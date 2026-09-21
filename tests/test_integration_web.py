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
   **跨两个自然日的每日汇总** → 页面完成）后，审计时间序列按升序渲染。

唯一允许的进程外替身是本机 ``http.server.ThreadingHTTPServer``（webhook 桩渠道）。

**``follow_redirects`` 的坑（M8 规格已冻结）**：``TestClient`` 默认跟随重定向，用默认客户端
根本观察不到 303，因此本文件一律用 ``follow_redirects=False`` 观察跳转。
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime
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
    "at": "21:00",
    "timezone": "Asia/Shanghai",
}

#: 需要**自己驱动** ``run_once()`` 的用例用这一组：扫描周期取上限 3600，使
#: ``ReminderScheduler`` 的后台线程不会在测试推进时钟后抢跑，从而让「第 N 天这一轮」由
#: 测试精确触发。调度器本身仍是真货（同一个 ``run_once()`` 代码路径）。
_DIGEST_REMINDERS = {
    "scan_interval_seconds": 3600,
    "at": "21:00",
    "timezone": "Asia/Shanghai",
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
def _make_settings(tmp_path: Path, webhook_url: str, *, reminders: dict | None = None):
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
        "reminders": dict(reminders or _SHORT_REMINDERS),
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

        # 默认列表的**主表**不再显示它；直接访问列表页的主表也看不到
        # （「最近完成」栏目渲染在主表下方，今天完成的待办按 §5.2 会出现在那里）
        after = client.get("/todos")
        assert after.status_code == 200
        main_region = after.text.split("最近完成")[0]
        assert "备份失败" not in main_region
        assert f"/todos/{todo_id}" not in main_region
        assert 'action="/todos/' not in main_region

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
# 3. 跨两个自然日各发一次每日汇总后经真实页面完成；详情页审计时间序列
#
#    **改写自 ``test_web_detail_audit_timeline_after_real_reminders``（场景替换，不是放宽）。**
#    旧场景依赖已被移除的语义：同一条待办在**同一自然日内**按 +2s / +3s 的单项间隔连发两次提醒。
#    ``add-daily-digest`` 之后「同一自然日绝不发第二次汇总」，旧场景在新模型下不可能发生。
#    新场景覆盖同样的接缝（真实 Web 页面 + 真实 SQLite + 真实
#    ``ReminderScheduler``/``DeliveryService`` → 真实 socket），但把「同日两次」换成新模型下
#    真实可发生的「**跨两个本地自然日各一次汇总**」，并补上旧约束的防回归断言。
# --------------------------------------------------------------------------- #
def test_web_detail_audit_timeline_after_real_digests(tmp_path, webhook_stub, manual_clock):
    # scan_interval=3600：后台扫描线程不在本轮抢跑，跨天由测试用 run_once() 精确触发。
    settings = _make_settings(
        tmp_path, webhook_stub.url, reminders=_DIGEST_REMINDERS
    )
    ctx = build_context(settings, clock=manual_clock)
    app = create_app(ctx=ctx)

    zone = settings.reminders.zone
    day1_trigger = datetime.combine(date(2024, 1, 1), settings.reminders.trigger_time, tzinfo=zone)
    day2_trigger = datetime.combine(date(2024, 1, 2), settings.reminders.trigger_time, tzinfo=zone)

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
        # 首次通知（由受理 worker 线程经真实 socket 投递）——不是汇总。
        assert webhook_stub.wait_for(1, timeout=5.0) is True
        assert webhook_stub.count() == 1
        assert webhook_stub.payloads()[0]["kind"] == "first_notice"

        events_before = ctx.todos.detail(todo_id).events
        assert [event.kind for event in events_before] == ["created"]
        created_event = events_before[0].occurred_at

        # ---- 第 1 个自然日（本地 2024-01-01 21:00）：发出第 1 次汇总 ----
        manual_clock.set(day1_trigger)
        assert ctx.scheduler.run_once() == 1
        assert webhook_stub.wait_for(2, timeout=5.0) is True
        assert webhook_stub.count() == 2, "第 1 天汇总必须恰好新增一条请求"
        first_reminder = ctx.todos.detail(todo_id).events[1].occurred_at
        assert ctx.todos.detail(todo_id).events[1].kind == "reminder"
        assert ctx.todos.detail(todo_id).todo.reminder_count == 1

        # 防回归（旧场景被替换掉的那条约束）：同一自然日稍后（本地 22:00）再跑一轮必须返回 0，
        # webhook 端不得新增请求。
        manual_clock.advance(3600)
        assert ctx.scheduler.run_once() == 0
        assert webhook_stub.count() == 2

        # ---- 第 2 个自然日（本地 2024-01-02 21:00）：发出第 2 次汇总 ----
        manual_clock.set(day2_trigger)
        assert ctx.scheduler.run_once() == 1
        assert webhook_stub.wait_for(3, timeout=5.0) is True
        assert webhook_stub.count() == 3, "第 2 天汇总必须恰好新增一条请求"
        events_after_day2 = ctx.todos.detail(todo_id).events
        assert [event.kind for event in events_after_day2] == [
            "created",
            "reminder",
            "reminder",
        ]
        assert events_after_day2[2].kind == "reminder"
        second_reminder = events_after_day2[2].occurred_at
        assert ctx.todos.detail(todo_id).todo.reminder_count == 2

        # 两次汇总各是一条独立请求，正文都含该待办；都不归属任何单条待办。
        digest_payloads = [
            payload for payload in webhook_stub.payloads() if payload["kind"] == "reminder"
        ]
        assert len(digest_payloads) == 2
        assert all("备份失败" in payload["body"] for payload in digest_payloads)
        assert all(payload["todo_id"] is None for payload in digest_payloads)

        # 两个提醒时刻分属**两个不同的本地自然日**（新模型的核心约束）。
        local_dates = [
            as_utc(moment).astimezone(zone).date()
            for moment in (first_reminder, second_reminder)
        ]
        assert local_dates == [date(2024, 1, 1), date(2024, 1, 2)]

        # 与持久化的每日状态对账：两个 local_date 各一行、均已投递、fired_at 即提醒时刻。
        for expected_date, moment in zip(local_dates, (first_reminder, second_reminder)):
            state = ctx.digest.state_for(expected_date)
            assert state is not None, expected_date
            assert state.delivered is True
            assert state.fired_at is not None
            assert as_utc(state.fired_at) == as_utc(moment)

        # 第 2 天同日再跑一轮同样返回 0（跨天后也不重复）。
        assert ctx.scheduler.run_once() == 0
        assert webhook_stub.count() == 3

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
        assert "提醒次数" in html

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
        assert occurred[1] == as_utc(day1_trigger)
        assert occurred[2] == as_utc(day2_trigger)

        # 只取「状态变更时间序列」表格内的文本（收窄到「待办」小节之前，避免
        # 「提醒次数」这类同名文本混入），避免其它区块的同名时间干扰
        start = html.index("状态变更时间序列")
        end = html.index("<h2>待办</h2>", start)
        timeline = html[start:end]
        rendered = [event.occurred_at.strftime("%Y-%m-%d %H:%M:%S") for event in events]
        positions = [timeline.find(text) for text in rendered]
        assert all(position >= 0 for position in positions), (rendered, timeline)
        assert positions == sorted(positions), (rendered, positions)
        assert positions[1] < positions[2], (rendered, positions)
        assert "创建" in timeline and "提醒" in timeline and "完成" in timeline
        # 两次提醒各占一行（强化：不只是「出现过提醒」）
        assert timeline.count("提醒") == 2, timeline
