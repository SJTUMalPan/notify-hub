"""阶段 D 集成测试：``Web 页面 → service → DB``（architecture.md 第 7 节接缝表）。

作用域 2。本文件补上一轮明确披露的缺口：当时 M8 尚未实现，「在页面上点完成」在
``tests/test_e2e.py`` 里以等价的生产入口 ``POST /api/v1/todos/{id}/done`` 替代；现在换回真货。

覆盖的接缝
----------
1. ``create_app`` → api + web + classifier + scheduler：**同一个 app** 上 ``/healthz``、
   ``/api/v1/todos``、``/todos`` 三者同时可达；lifespan 进入后三个后台线程在场、退出后消失。
2. Web 列表页的真实 ``<form method="post" action="/todos/{id}/done">`` → ``ctx.todos.complete``
   → SQLite：303 → ``Location: /todos``，``ctx.todos.get()`` 状态为 done，列表页与 API 同步变化。
3. Web 详情页 → ``TodoService.detail`` → ``todo_events``/``deliveries``：真实链路（受理 →
   提醒 → 页面完成）后，审计时间序列按升序渲染。

唯一允许的进程外替身是本机 ``http.server.ThreadingHTTPServer``（webhook 桩渠道）。

**``follow_redirects`` 的坑（M8 规格已冻结）**：``TestClient`` 默认跟随重定向，用默认客户端
根本观察不到 303，因此本文件一律用 ``follow_redirects=False`` 观察跳转。
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from notify_hub.app import create_app
from notify_hub.clock import as_utc
from notify_hub.config import load_settings
from notify_hub.context import build_context

#: 三个后台组件（M3 规则轮询 / M7 受理 worker / M6 提醒调度）的线程名前缀。
_HUB_THREAD_PREFIX = "notify-hub-"

_SHORT_REMINDERS = {
    "scan_interval_seconds": 1,
    "first_reminder_after_seconds": 2,
    "reminder_interval_seconds": 3,
}


def _hub_thread_names() -> set[str]:
    return {
        thread.name
        for thread in threading.enumerate()
        if thread.is_alive() and thread.name.startswith(_HUB_THREAD_PREFIX)
    }


# --------------------------------------------------------------------------- #
# 进程外对端：本机 webhook 桩（真实 socket）
# --------------------------------------------------------------------------- #
class WebhookStub:
    """本机 HTTP 桩：真实 socket 上扮演 webhook 接收方。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests: list[dict] = []
        stub = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    payload = None
                with stub._lock:
                    stub.requests.append({"path": self.path, "json": payload})
                body = b'{"code": 0, "msg": "ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:  # 静音
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="webhook-stub-web", daemon=True
        )
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/notify"

    def count(self) -> int:
        with self._lock:
            return len(self.requests)

    def payloads(self) -> list[dict]:
        with self._lock:
            return [dict(item["json"]) for item in self.requests if item["json"] is not None]

    def wait_for(self, expected: int, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.count() >= expected:
                return True
            time.sleep(0.02)
        return self.count() >= expected

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)


@pytest.fixture
def webhook_stub():
    stub = WebhookStub()
    try:
        yield stub
    finally:
        stub.close()


# --------------------------------------------------------------------------- #
# 真实配置装配（M1 load_settings，凭据经环境变量解析）
# --------------------------------------------------------------------------- #
def _make_settings(tmp_path: Path, webhook_url: str):
    (tmp_path / "rules.yaml").write_text(
        yaml.safe_dump(
            {
                "case_sensitive": False,
                "defaults": {
                    "category": "uncategorized",
                    "labels": [],
                    "need_ack": False,
                    "channel": "hook",
                },
                "rules": [],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    config = {
        "server": {"host": "127.0.0.1", "port": 8000, "log_level": "INFO"},
        "storage": {"db_path": "./data/notify.db"},
        "rules": {"path": "./rules.yaml", "poll_interval_seconds": 5},
        "reminders": dict(_SHORT_REMINDERS),
        "default_channel": "hook",
        "channels": [
            {
                "id": "hook",
                "type": "webhook",
                "enabled": True,
                "params": {},
                "credentials": {"url": "STUB_WEBHOOK_URL"},
            }
        ],
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return load_settings(config_path, env={"STUB_WEBHOOK_URL": webhook_url})


def _complete_form_action(html: str, todo_id: int) -> str:
    """从列表页 HTML 里取出真实的「完成」表单 action（不硬编码路径）。"""
    pattern = re.compile(
        r'<form[^>]*method="post"[^>]*action="(?P<action>[^"]*)"[^>]*>', re.IGNORECASE
    )
    for match in pattern.finditer(html):
        action = match.group("action")
        if f"/todos/{todo_id}/done" in action:
            return action
    raise AssertionError(f"列表页未找到待办 {todo_id} 的完成表单：{html[:2000]}")


# --------------------------------------------------------------------------- #
# 1. 完整 create_app：api + web 同时挂载 + lifespan 起停三个后台线程
# --------------------------------------------------------------------------- #
def test_create_app_serves_healthz_api_and_web_pages_with_lifespan(tmp_path, webhook_stub):
    settings = _make_settings(tmp_path, webhook_stub.url)
    ctx = build_context(settings)
    app = create_app(ctx=ctx)

    assert _hub_thread_names() == set()
    # 不跟随重定向：这样才能观察 M8 的 303（默认客户端会跟随成 200）
    with TestClient(app, follow_redirects=False) as client:
        # 组合根把 M7 与 M8 两套路由挂到同一个 app 上
        health = client.get("/healthz")
        assert health.status_code == 200 and health.json()["status"] == "ok"

        api_todos = client.get("/api/v1/todos")
        assert api_todos.status_code == 200
        assert api_todos.json() == {"todos": [], "total": 0}

        page = client.get("/todos")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert "没有匹配的待办" in page.text

        # 根路径：303 → /todos（web 路由确实挂上了）
        root = client.get("/")
        assert root.status_code == 303
        assert root.headers["location"].endswith("/todos")

        # 消息页也由 web 路由提供
        assert client.get("/messages").status_code == 200

        assert ctx.scheduler.running is True
        assert _hub_thread_names() == {
            "notify-hub-rule-reloader",
            "notify-hub-ingest-worker",
            "notify-hub-reminder-scheduler",
        }

    assert ctx.scheduler.running is False
    assert _hub_thread_names() == set()


# --------------------------------------------------------------------------- #
# 2. 真实「完成」表单：Web 页面 → TodoService → DB（不经过 API 完成端点）
# --------------------------------------------------------------------------- #
def test_web_done_form_completes_todo_through_service_and_db(tmp_path, webhook_stub, manual_clock):
    settings = _make_settings(tmp_path, webhook_stub.url)
    ctx = build_context(settings, clock=manual_clock)
    app = create_app(ctx=ctx)

    with TestClient(app, follow_redirects=False) as client:
        accepted = client.post(
            "/api/v1/messages",
            json={
                "source": "cron",
                "title": "备份失败",
                "body": "exit code 1",
                "level": "error",
                "need_ack": True,
                "dedup_key": "web-form-1",
            },
        )
        assert accepted.status_code == 202
        todo_id = accepted.json()["todo_id"]
        assert webhook_stub.wait_for(1, timeout=5.0) is True

        listing = client.get("/todos")
        assert listing.status_code == 200
        assert "备份失败" in listing.text
        assert f"/todos/{todo_id}" in listing.text

        action = _complete_form_action(listing.text, todo_id)
        assert action == f"/todos/{todo_id}/done"

        # 点「完成」= 提交列表页那个真实表单；303 必须能被观察到（不跟随重定向）。
        done = client.post(action, data={})
        assert done.status_code == 303, done.text[:500]
        assert done.headers["location"].endswith("/todos")

        # 页面 → service → DB：状态真的落到了数据库，而不是仅重定向成功。
        todo = ctx.todos.get(todo_id)
        assert todo.status is not None and todo.status == "done"
        assert todo.completed_at is not None

        # 默认列表不再显示它；直接访问列表页也看不到
        after = client.get("/todos")
        assert after.status_code == 200
        assert "备份失败" not in after.text
        assert f"/todos/{todo_id}" not in after.text
        assert 'action="/todos/' not in after.text

        # 详情页仍可访问，并显示「已完成」
        detail = client.get(f"/todos/{todo_id}")
        assert detail.status_code == 200
        assert "已完成" in detail.text

        # API 与页面看到同一份状态（同一 ctx / 同一 DB）
        assert client.get("/api/v1/todos").json()["total"] == 0
        assert client.get("/api/v1/todos?status=done").json()["todos"][0]["id"] == todo_id

        # 完成后不再提醒
        manual_clock.advance(100_000)
        assert ctx.scheduler.run_once() == 0
        assert webhook_stub.count() == 1


# --------------------------------------------------------------------------- #
# 3. 超时提醒后经真实页面完成；详情页审计时间序列（真实 service 链路）
# --------------------------------------------------------------------------- #
def test_web_detail_audit_timeline_after_real_reminders(tmp_path, webhook_stub, manual_clock):
    settings = _make_settings(tmp_path, webhook_stub.url)
    ctx = build_context(settings, clock=manual_clock)
    app = create_app(ctx=ctx)

    with TestClient(app, follow_redirects=False) as client:
        accepted = client.post(
            "/api/v1/messages",
            json={
                "source": "db-backup",
                "title": "备份失败",
                "body": "exit code 1",
                "level": "error",
                "need_ack": True,
                "dedup_key": "web-detail-1",
            },
        )
        assert accepted.status_code == 202
        todo_id = accepted.json()["todo_id"]
        assert webhook_stub.wait_for(1, timeout=5.0) is True

        created_event = ctx.todos.detail(todo_id).events[0].occurred_at

        # 两次提醒（真实 DeliveryService → webhook 桩）
        manual_clock.advance(2)
        assert ctx.scheduler.run_once() == 1
        assert webhook_stub.wait_for(2, timeout=5.0) is True
        first_reminder = ctx.todos.detail(todo_id).events[1].occurred_at

        manual_clock.advance(3)
        assert ctx.scheduler.run_once() == 1
        assert webhook_stub.wait_for(3, timeout=5.0) is True
        second_reminder = ctx.todos.detail(todo_id).events[2].occurred_at

        # 经真实页面表单完成
        listing = client.get("/todos")
        action = _complete_form_action(listing.text, todo_id)
        done = client.post(action, data={})
        assert done.status_code == 303, done.text[:500]
        assert done.headers["location"].endswith("/todos")

        detail = client.get(f"/todos/{todo_id}")
        assert detail.status_code == 200
        html = detail.text

        # 关联消息的四要素
        assert "备份失败" in html
        assert "exit code 1" in html
        assert "db-backup" in html
        assert "error" in html

        # 审计时间序列必须按时间升序渲染（创建 < 第 1 次提醒 < 第 2 次提醒 < 完成）
        events = ctx.todos.detail(todo_id).events
        assert [event.kind for event in events] == [
            "created",
            "reminder",
            "reminder",
            "completed",
        ]
        occurred = [as_utc(event.occurred_at) for event in events]
        assert occurred == sorted(occurred)
        assert occurred[0] == as_utc(created_event)
        assert occurred[1] == as_utc(first_reminder)
        assert occurred[2] == as_utc(second_reminder)

        # 只取「状态变更时间序列」表格内的文本，避免其它区块的同名时间干扰
        start = html.index("状态变更时间序列")
        end = html.index("原始消息", start)
        timeline = html[start:end]
        rendered = [event.occurred_at.strftime("%Y-%m-%d %H:%M:%S") for event in events]
        positions = [timeline.find(text) for text in rendered]
        assert all(position >= 0 for position in positions), (rendered, timeline)
        assert positions == sorted(positions), (rendered, positions)
        assert "创建" in timeline and "提醒" in timeline and "完成" in timeline
