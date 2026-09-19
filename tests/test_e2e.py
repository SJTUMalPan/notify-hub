"""阶段 D 端到端测试：``test_full_lifecycle`` 与 add-daily-digest 第 3.4 节第 28 条。

场景（architecture.md 第 3.4 节第 28 条 + 第 7 节）::

    投递 need_ack=true 消息
      → 首次通知经真实 webhook（真实 socket）送达
      → 时钟推进越过每日触发时刻 → scheduler.run_once() 收到**一条**汇总
      → 在真实 Web 页面（M8）上提交列表页那个完成表单
      → 次日汇总只含仍未完成的那条

**本文件已随 add-daily-digest 改写**：原先的 ``test_full_lifecycle`` 测的是**已被移除**的
「单项超时间隔提醒」（每条待办按 ``first_reminder_after_seconds`` / ``reminder_interval_seconds``
分别提醒、提醒文案含该待办的超时时长、完成后按间隔不再提醒）。现在改测每日汇总语义。

「完成」这一跳走的是**真货**：``GET /todos`` 拿到的列表页 HTML 里那个
``<form method="post" action="/todos/{id}/done">``，直接 POST 它，并用
``follow_redirects=False`` 观察 303（``TestClient`` 默认跟随重定向，看不到 303——见 M8 规格）。

进程外打桩仅限本机 ``ThreadingHTTPServer``（webhook 接收方）；M1–M8 内部边界无替身，
**不使用** ``MockTransport``。
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

#: 汇总触发时刻：时钟起点 2024-01-01T00:00:00Z == 08:00 Asia/Shanghai，
#: 因此 advance(3600) 恰好跨过当天 09:00。scan_interval=3600 让真实后台扫描线程在
#: 测试期间不抢跑，由测试用 run_once() 精确驱动（真实 lifespan 仍然起停该线程）。
_DIGEST_AT_0900 = {
    "at": "09:00",
    "timezone": "Asia/Shanghai",
    "scan_interval_seconds": 3600,
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
                "reminders": dict(_DIGEST_AT_0900),
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


def _reminder_deliveries(ctx) -> list:
    """从真实 SQLite 读出 ``event=reminder`` 的投递记录（汇总记录不归属任何待办/消息）。"""
    from sqlmodel import select  # noqa: PLC0415 - 惰性，保证收集阶段不依赖实现

    from notify_hub.models import DeliveryRecord  # noqa: PLC0415

    statement = (
        select(DeliveryRecord)
        .where(DeliveryRecord.event == "reminder")
        .order_by(DeliveryRecord.id.asc())
    )
    with ctx.db.session() as session:
        return list(session.exec(statement).all())


def test_full_lifecycle(tmp_path, webhook_stub, manual_clock):
    """受理 → 首次通知 → 越过每日触发时刻收到一条汇总 → 页面上点完成 → 不再有新汇总。

    **改写自旧的单项提醒生命周期用例。** 旧用例断言（均已移除）：advance(2) 后
    ``run_once() == 1`` 发出**单条待办**提醒、标题含「2 秒」、``overdue_seconds == 2.0``、
    ``reminder["todo_id"] == todo_id``、完成后按提醒间隔不再重复。
    新语义：advance 越过本地 09:00 后收到一条汇总（``todo_id is None``），当日不再重复。
    """
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

        todo = ctx.todos.get(todo_id)
        assert todo is not None
        assert todo.status == "pending"
        first_notified_at = as_utc(todo.first_notified_at)
        assert as_utc(todo.first_notified_at) == as_utc(todo.last_notified_at)
        assert [event.kind for event in ctx.todos.detail(todo_id).events] == ["created"]

        # --- 2. 本地 08:59:59：未到触发时刻，不发送（取代旧的「未到提醒门槛不打扰」） ---
        manual_clock.advance(3599)
        assert ctx.scheduler.run_once() == 0
        assert webhook_stub.count() == 1

        # --- 3. 跨过本地 09:00：一条汇总，正文含待办标题 ---
        manual_clock.advance(1)
        assert ctx.scheduler.run_once() == 1
        assert webhook_stub.wait_for(2, timeout=5.0) is True, "汇总未送达桩渠道"
        assert webhook_stub.count() == 2
        digest = webhook_stub.payloads()[1]
        assert digest["kind"] == "reminder"
        assert digest["title"] == "[待办汇总] 1 项未完成"
        assert "备份失败" in digest["body"]
        assert "已超时" in digest["body"]
        assert digest["overdue_seconds"] is None
        assert digest["todo_id"] is None

        todo = ctx.todos.get(todo_id)
        assert todo.reminder_count == 1
        assert as_utc(todo.last_notified_at) == manual_clock.now()
        assert [event.kind for event in ctx.todos.detail(todo_id).events] == [
            "created",
            "reminder",
        ]
        # 同一自然日不重复发送（取代旧的「间隔未到不重复提醒」）
        assert ctx.scheduler.run_once() == 0
        assert webhook_stub.count() == 2

        # --- 4. 在真实 Web 页面上点「完成」（M8 列表页那个表单） ---
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

        # --- 5. 完成后时钟继续前进也没有新请求（旧行为：完成后按间隔不再提醒） ---
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

        # 汇总投递记录不归属这条消息/待办；消息名下只有首次通知
        assert [record.event for record in ctx.messages.deliveries(message_id)] == [
            "first_notice"
        ]
        digest_records = _reminder_deliveries(ctx)
        assert len(digest_records) == 1
        assert digest_records[0].todo_id is None
        assert digest_records[0].message_id is None
        assert digest_records[0].ok is True

        assert ctx.scheduler.running is True

    assert ctx.scheduler.running is False


def test_daily_digest_end_to_end_across_two_days(tmp_path, webhook_stub, manual_clock):
    """architecture.md 第 3.4 节第 28 条（真实 ``create_app`` + 真实 SQLite + 真实 webhook 桩）。

    两条 ``need_ack=true`` 消息 → 越过触发时刻 → ``run_once() == 1`` 且桩只多收到**一条**
    请求（正文含两条标题）→ 在真实 Web 表单上完成其中一条 → 次日汇总只含剩下那条。

    注：两条消息各自的**首次通知**也会到达同一个桩，因此「恰好 1 条」按**越过触发时刻之后
    新增的请求数**断言，并额外断言全局只有一条 ``kind=reminder`` 的载荷（不为每条待办各发一条）。
    """
    settings = _settings(tmp_path, webhook_url=webhook_stub.url)
    # 真实组合根装配 + 真实 lifespan。**必须把测试时钟注入组合根**：``create_app(settings)``
    # 会走 ``build_context(settings, clock=None)`` → ``SystemClock()``，调度器便看真实系统时间，
    # 本用例的 ``manual_clock.advance(...)`` 全部无效、结果按当天钟点飘。渠道仍从配置真实构造
    # （不注入 registry）。
    ctx = build_context(settings, clock=manual_clock)
    app = create_app(ctx=ctx)

    with TestClient(app, follow_redirects=False) as client:
        titles = ["磁盘占用 91%", "证书 3 天后过期"]
        accepted = []
        for index, title in enumerate(titles, start=1):
            response = client.post(
                "/api/v1/messages",
                json={
                    "source": "monitor",
                    "title": title,
                    "level": "error",
                    "need_ack": True,
                    "dedup_key": f"digest-{index}",
                },
            )
            assert response.status_code == 202
            accepted.append(response.json())

        assert ctx.pipeline.drain(timeout=5.0) is True
        assert webhook_stub.wait_for(2, timeout=5.0) is True, "首次通知未送达桩渠道"
        baseline = webhook_stub.count()
        assert baseline == 2

        # --- 越过当天本地 09:00：恰好一条汇总，正文含两条标题 ---
        manual_clock.advance(3600)
        assert ctx.scheduler.run_once() == 1
        assert webhook_stub.wait_for(baseline + 1, timeout=5.0) is True
        assert webhook_stub.count() == baseline + 1, "汇总必须恰好一条请求"
        first_digest = webhook_stub.payloads()[-1]
        assert first_digest["kind"] == "reminder"
        assert first_digest["title"] == "[待办汇总] 2 项未完成"
        assert all(title in first_digest["body"] for title in titles)
        assert first_digest["todo_id"] is None
        reminder_payloads = [
            payload for payload in webhook_stub.payloads() if payload["kind"] == "reminder"
        ]
        assert len(reminder_payloads) == 1, "不得为每条待办各发一条"

        # --- 在真实 Web 表单上完成其中一条 ---
        done_id, kept_id = accepted[0]["todo_id"], accepted[1]["todo_id"]
        page = client.get("/todos")
        assert page.status_code == 200
        action = _complete_form_action(page.text, done_id)
        done = client.post(action, data={})
        assert done.status_code == 303, done.text[:500]
        assert ctx.todos.get(done_id).status == "done"
        assert ctx.todos.get(kept_id).status == "pending"

        # --- 次日同一时刻：汇总只含剩下那条 ---
        manual_clock.advance(86400)
        assert ctx.scheduler.run_once() == 1
        assert webhook_stub.wait_for(baseline + 2, timeout=5.0) is True
        assert webhook_stub.count() == baseline + 2, "次日汇总也必须恰好一条请求"
        second_digest = webhook_stub.payloads()[-1]
        assert second_digest["kind"] == "reminder"
        assert second_digest["title"] == "[待办汇总] 1 项未完成"
        assert titles[1] in second_digest["body"]
        assert titles[0] not in second_digest["body"]
        assert second_digest["todo_id"] is None

        # 每天一条汇总投递记录，均不归属于任何待办/消息
        records = _reminder_deliveries(ctx)
        assert len(records) == 2
        assert all(record.todo_id is None and record.message_id is None for record in records)
        assert all(record.channel_id == "hook" for record in records)
        assert all(record.ok is True for record in records)

        assert ctx.scheduler.running is True

    assert ctx.scheduler.running is False
