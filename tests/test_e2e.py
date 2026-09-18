"""阶段 D 端到端测试（tasks 9.5）：``tests/test_e2e.py::test_full_lifecycle``。

场景（规格 architecture.md 第 7 节）::

    投递 need_ack=true 消息
      → 首次通知经真实 webhook（真实 socket）送达
      → 时钟推进 + scheduler.run_once() 收到提醒
      → 在真实 Web 页面（M8）上提交列表页那个完成表单
      → 再 run_once() 不再提醒

「完成」这一跳走的是**真货**：``GET /todos`` 拿到的列表页 HTML 里那个
``<form method="post" action="/todos/{id}/done">``，直接 POST 它，并用
``follow_redirects=False`` 观察 303（``TestClient`` 默认跟随重定向，看不到 303——见 M8 规格）。

进程外打桩仅限本机 ``ThreadingHTTPServer``（webhook 接收方）；M1–M8 内部边界无替身。
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

#: 提醒间隔已按 tasks 9.5 缩短（默认 1800/3600），以便用时钟推进而非真实等待触发提醒。
_SHORTENED_REMINDERS = {
    "scan_interval_seconds": 1,
    "first_reminder_after_seconds": 2,
    "reminder_interval_seconds": 3,
}


class _WebhookStub:
    """本机 webhook 桩（真实 socket），记录收到的 JSON 载荷。"""

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
                body = json.dumps({"code": 0, "msg": "ok"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="webhook-stub-e2e", daemon=True
        )
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/notify"

    def count(self) -> int:
        with self._lock:
            return len(self.requests)

    def payloads(self) -> list[dict]:
        with self._lock:
            return [dict(item["json"]) for item in self.requests if item["json"] is not None]

    def wait_for(self, expected: int, timeout: float = 5.0) -> bool:
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
    stub = _WebhookStub()
    try:
        yield stub
    finally:
        stub.close()


def _settings(tmp_path: Path, *, webhook_url: str):
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
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
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "server": {"host": "127.0.0.1", "port": 8000, "log_level": "INFO"},
                "storage": {"db_path": "./data/notify.db"},
                "rules": {"path": "./rules.yaml", "poll_interval_seconds": 5},
                "reminders": dict(_SHORTENED_REMINDERS),
                "default_channel": "hook",
                "channels": [
                    {
                        "id": "hook",
                        "type": "webhook",
                        "enabled": True,
                        "params": {},
                        "credentials": {"url": "NOTIFY_E2E_WEBHOOK_URL"},
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return load_settings(config_path, env={"NOTIFY_E2E_WEBHOOK_URL": webhook_url})


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


def test_full_lifecycle(tmp_path, webhook_stub, manual_clock):
    """tasks 9.5：受理 → 首次通知 → 超时提醒 → 页面上点完成 → 不再提醒。"""
    settings = _settings(tmp_path, webhook_url=webhook_stub.url)
    ctx = build_context(settings, clock=manual_clock)
    app = create_app(ctx=ctx)  # 生产装配 + lifespan 真实起停后台线程

    # 必须显式关闭重定向跟随，才能观察到 Web「完成」表单的 303。
    with TestClient(app, follow_redirects=False) as client:
        # --- 1. 受理一条 need_ack 消息（真实 HTTP → 分类 → 落库 → 待办 → 后台派发） ---
        accepted = client.post(
            "/api/v1/messages",
            json={
                "source": "cron",
                "title": "备份失败",
                "body": "exit code 1",
                "level": "error",
                "need_ack": True,
                "dedup_key": "backup-2024-01-01",
            },
        )
        assert accepted.status_code == 202
        body = accepted.json()
        message_id, todo_id = body["message_id"], body["todo_id"]
        assert isinstance(todo_id, int)

        assert ctx.pipeline.drain(timeout=5.0) is True
        assert webhook_stub.wait_for(1, timeout=5.0) is True, "首次通知未送达桩渠道"
        assert webhook_stub.count() == 1
        first_notice = webhook_stub.payloads()[0]
        assert first_notice["kind"] == "first_notice"
        assert first_notice["title"] == "备份失败"
        assert first_notice["overdue_seconds"] is None

        # 为确定性地驱动提醒轮次，停掉后台扫描线程（lifespan 退出时会再次 stop，幂等）。
        ctx.scheduler.stop()

        todo = ctx.todos.get(todo_id)
        assert todo is not None
        assert todo.status == "pending"
        first_notified_at = as_utc(todo.first_notified_at)
        assert as_utc(todo.first_notified_at) == as_utc(todo.last_notified_at)
        assert [event.kind for event in ctx.todos.detail(todo_id).events] == ["created"]

        # --- 2. 缩短后的提醒间隔到期：run_once() 发出提醒 ---
        manual_clock.advance(2)
        assert ctx.scheduler.run_once() == 1
        assert webhook_stub.wait_for(2, timeout=5.0) is True, "超时提醒未送达桩渠道"
        assert webhook_stub.count() == 2
        reminder = webhook_stub.payloads()[1]
        assert reminder["kind"] == "reminder"
        assert "备份失败" in reminder["title"]
        assert "2 秒" in reminder["title"]
        assert reminder["overdue_seconds"] == 2.0
        assert reminder["todo_id"] == todo_id

        todo = ctx.todos.get(todo_id)
        assert todo.reminder_count == 1
        assert as_utc(todo.last_notified_at) == manual_clock.now()
        assert [event.kind for event in ctx.todos.detail(todo_id).events] == [
            "created",
            "reminder",
        ]

        # --- 3. 在真实 Web 页面上点「完成」（M8 列表页那个表单） ---
        page = client.get("/todos")
        assert page.status_code == 200
        assert "备份失败" in page.text
        action = _complete_form_action(page.text, todo_id)
        assert action == f"/todos/{todo_id}/done"

        done = client.post(action, data={})
        assert done.status_code == 303, done.text[:500]
        assert done.headers["location"].endswith("/todos")

        assert ctx.todos.get(todo_id).status == "done"
        assert client.get("/api/v1/todos").json()["total"] == 0  # 默认仅待完成
        done_list = client.get("/api/v1/todos?status=done").json()
        assert done_list["total"] == 1
        assert done_list["todos"][0]["id"] == todo_id
        # 默认列表页不再显示已完成的那条
        assert "备份失败" not in client.get("/todos").text
        assert "已完成" in client.get(f"/todos/{todo_id}").text

        # --- 4. 完成后不再提醒（时钟继续前进也没有新通知） ---
        manual_clock.advance(100_000)
        assert ctx.scheduler.run_once() == 0
        assert webhook_stub.count() == 2

        detail = ctx.todos.detail(todo_id)
        assert [event.kind for event in detail.events] == [
            "created",
            "reminder",
            "completed",
        ]
        occurred = [as_utc(event.occurred_at) for event in detail.events]
        assert occurred == sorted(occurred), "状态变更时间序列必须按时间升序"
        assert occurred[0] == first_notified_at

        records = ctx.messages.deliveries(message_id)
        assert [record.event for record in records] == ["first_notice", "reminder"]
        assert all(record.ok for record in records)

    assert ctx.scheduler.running is False
