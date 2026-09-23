"""阶段 D 集成测试：真实跨越模块间接缝（architecture.md 第 7 节）。

作用域 2。**不修改任何实现文件**；需要额外 fixture 时只在本文件内定义。

打桩边界声明
------------
本文件只对**进程外的对端**打桩：一个本机 ``http.server.ThreadingHTTPServer``
（``("127.0.0.1", 0)`` 临时端口），它扮演真实 webhook 接收方。
M1–M7 之间的内部边界全部走真实实现：
``load_settings`` → ``build_context`` / ``build_test_context`` → ``Database``(真实 SQLite 文件)
→ ``RuleClassifier``(真实文件 + 真实轮询线程) → ``MessageService``/``TodoService``
→ ``DeliveryService`` → **真实 ``WebhookNotifier``（真实 socket，不使用 ``MockTransport``）**
→ ``IngestPipeline``（真实队列 + 守护线程）→ FastAPI 路由（真实 ``TestClient`` HTTP）。

M5（CLI）与 M8（Web）的接缝已由本轮新增的 ``tests/test_integration_cli.py``
（CLI → 真实 uvicorn → app）与 ``tests/test_integration_web.py``
（Web 页面 → service → DB）覆盖；``tests/test_e2e.py::test_full_lifecycle`` 的「完成」
已从 API 端点换回列表页上的真实 Web 表单。

**add-daily-digest 变更（提醒模型整体替换）**：本文件原先的「单项超时间隔提醒」用例已被
改写为「每日汇总」语义，并在每处改写点注明它测的是哪个**已被移除**的行为。
被移除：``reminders.first_reminder_after_seconds`` / ``reminders.reminder_interval_seconds``、
待办服务上按超时门槛筛选待办的公开方法，以及「为每条待办分别发提醒、按各自间隔重复提醒、
提醒文案带该待办超时时长」这些行为。
新增：越过每日触发时刻（``reminders.at`` / ``reminders.timezone``）后发**一条**汇总，
正文含全部未完成待办的标题，逐条记账，单条投递记录不归属任何待办。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from notify_hub.api import create_api_app
from notify_hub.app import create_app
from notify_hub.clock import as_utc
from notify_hub.config import load_settings
from notify_hub.context import build_context, build_test_context
from notify_hub.domain import Level

# --------------------------------------------------------------------------- #
# 汇总参数三档（时钟起点 2024-01-01T00:00:00Z == 08:00 Asia/Shanghai）
#   * 未到：触发时刻 23:59，后台扫描线程在测试期间绝不发出汇总
#   * 当天稍后：触发时刻 09:00，可用 run_once() 精确跨过
#   * 已过 + 极短扫描：由真实后台线程自己发出汇总
# --------------------------------------------------------------------------- #
_DIGEST_NOT_YET = {
    "at": "23:59",
    "timezone": "Asia/Shanghai",
    "scan_interval_seconds": 3600,
}
_DIGEST_AT_0900 = {
    "at": "09:00",
    "timezone": "Asia/Shanghai",
    "scan_interval_seconds": 3600,
}
_DIGEST_PAST_RAPID = {
    "at": "08:00",
    "timezone": "Asia/Shanghai",
    "scan_interval_seconds": 0.05,
}

#: 三个后台组件（M3 规则轮询 / M7 受理worker / M6 提醒调度）的线程名前缀。
_HUB_THREAD_PREFIX = "notify-hub-"


def _from_iso(value: str) -> datetime:
    """解析 ISO 时间串（pydantic 会把 UTC 序列化成 ``...Z``，py3.10 的 fromisoformat 不认）。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _hub_thread_names() -> set[str]:
    return {
        thread.name
        for thread in threading.enumerate()
        if thread.is_alive() and thread.name.startswith(_HUB_THREAD_PREFIX)
    }


def _delivery_records(ctx, *, event: str | None = None) -> list:
    """从**真实 SQLite** 读出投递记录（汇总记录不挂在任何 message/todo 上，只能全表查）。

    惰性导入：``notify_hub.models`` 在阶段 A 已存在，但这里避免把任何实现细节提到收集期。
    """
    from sqlmodel import select  # noqa: PLC0415 - 惰性，保证收集阶段不依赖实现

    from notify_hub.models import DeliveryRecord  # noqa: PLC0415

    statement = select(DeliveryRecord).order_by(DeliveryRecord.id.asc())
    if event is not None:
        statement = statement.where(DeliveryRecord.event == event)
    with ctx.db.session() as session:
        return list(session.exec(statement).all())


# --------------------------------------------------------------------------- #
# 进程外对端：本机 webhook 桩（真实 socket）
# --------------------------------------------------------------------------- #
class WebhookStub:
    """本机 HTTP 桩：真实 socket 上扮演 webhook 接收方。

    ``wait_for`` 只做**有界轮询**（带明确超时），绝不依赖「等待一个扫描周期」。
    """

    def __init__(
        self,
        *,
        status: int = 200,
        response_body: object | None = None,
        delay: float = 0.0,
    ) -> None:
        self._lock = threading.Lock()
        self.requests: list[dict] = []
        self._status = status
        self._response_body = (
            {"code": 0, "msg": "ok"} if response_body is None else response_body
        )
        self._delay = delay
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
                    stub.requests.append(
                        {
                            "path": self.path,
                            "headers": {
                                key.lower(): value for key, value in self.headers.items()
                            },
                            "raw": raw,
                            "json": payload,
                        }
                    )
                if stub._delay:
                    time.sleep(stub._delay)
                body = json.dumps(stub._response_body).encode("utf-8")
                self.send_response(stub._status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:  # 静音，避免污染测试输出
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="webhook-stub", daemon=True
        )
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/notify"

    # ------------------------------------------------------------------ #
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

    def __enter__(self) -> "WebhookStub":
        return self

    def __exit__(self, *exc_info) -> bool:
        self.close()
        return False


@pytest.fixture
def webhook_stub():
    with WebhookStub() as stub:
        yield stub


# --------------------------------------------------------------------------- #
# 真实配置装配（走 M1 的 load_settings，凭据经环境变量解析）
# --------------------------------------------------------------------------- #
def _write_rules(path: Path, rules, *, defaults_channel: str | None = None) -> None:
    document = {
        "case_sensitive": False,
        "defaults": {
            "category": "uncategorized",
            "labels": [],
            "need_ack": False,
            "channel": defaults_channel,
        },
        "rules": list(rules),
    }
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _webhook_channel(
    channel_id: str = "hook", env_name: str = "STUB_URL", *, enabled: bool = True
) -> dict:
    return {
        "id": channel_id,
        "type": "webhook",
        "enabled": enabled,
        "params": {},
        "credentials": {"url": env_name},
    }


def _make_settings(
    tmp_path: Path,
    *,
    channels=(),
    default_channel: str | None = None,
    reminders=None,
    env=None,
    rules=(),
    defaults_channel: str | None = None,
    rules_poll: float = 5.0,
):
    _write_rules(tmp_path / "rules.yaml", rules, defaults_channel=defaults_channel)
    config = {
        "server": {"host": "127.0.0.1", "port": 8000, "log_level": "INFO"},
        "storage": {"db_path": "./data/notify.db"},
        "rules": {"path": "./rules.yaml", "poll_interval_seconds": rules_poll},
        "reminders": dict(reminders or _DIGEST_NOT_YET),
        "default_channel": default_channel,
        "channels": list(channels),
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return load_settings(config_path, env={} if env is None else env)


# --------------------------------------------------------------------------- #
# 1. 组合根真实装配 + lifespan 起停
# --------------------------------------------------------------------------- #
def test_create_app_assembles_real_graph_and_lifespan_starts_stops_workers(
    tmp_path, webhook_stub
):
    """``create_app(settings)`` 真装配；两个入口同 app 可达；lifespan 真起停后台线程。"""
    settings = _make_settings(
        tmp_path,
        channels=[_webhook_channel()],
        default_channel="hook",
        defaults_channel="hook",
        env={"STUB_URL": webhook_stub.url},
    )

    app = create_app(settings)
    ctx = app.state.ctx
    assert ctx.settings.db_path == settings.db_path
    assert ctx.settings.rules_path == settings.rules_path
    assert ctx.registry.available_ids() == ("hook",)
    # 组合根必须把汇总配置与 DigestService 一起装配进 AppContext（context.py 阶段 B 改动）。
    # 旧行为（已移除）：AppContext 只按 first_reminder_after_seconds/reminder_interval_seconds
    # 装配单项提醒调度器。
    assert ctx.settings.reminders.at == "23:59"
    assert ctx.settings.reminders.timezone == "Asia/Shanghai"
    assert ctx.digest is not None
    # 生产装配：run_inline 为假（受理不阻塞响应，见 D-1）
    assert ctx.pipeline._run_inline is False  # noqa: SLF001 - 集成测试确证装配分支

    assert _hub_thread_names() == set()
    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        health_body = health.json()
        assert health_body["status"] == "ok"
        assert isinstance(_from_iso(health_body["time"]), datetime)

        todos = client.get("/api/v1/todos")
        assert todos.status_code == 200
        assert todos.json() == {"todos": [], "total": 0}

        assert ctx.scheduler.running is True
        assert _hub_thread_names() == {
            "notify-hub-rule-reloader",
            "notify-hub-ingest-worker",
            "notify-hub-reminder-scheduler",
        }

    assert ctx.scheduler.running is False
    assert _hub_thread_names() == set()


# --------------------------------------------------------------------------- #
# 2. HTTP 接入 → 分类 → 落库 → 待办 → 后台派发 → 真实 webhook（真实 socket）
# --------------------------------------------------------------------------- #
def test_message_reaches_real_webhook_over_socket(tmp_path, webhook_stub):
    settings = _make_settings(
        tmp_path,
        channels=[_webhook_channel()],
        default_channel="hook",
        defaults_channel="hook",
        env={"STUB_URL": webhook_stub.url},
    )
    app = create_app(settings)
    ctx = app.state.ctx

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/messages",
            json={
                "source": "disk-check",
                "title": "磁盘使用率 92%",
                "body": "请清理 /var/log",
                "level": "error",
                "need_ack": True,
                "dedup_key": "disk-1",
            },
        )
        assert response.status_code == 202
        body = response.json()
        message_id, todo_id = body["message_id"], body["todo_id"]
        assert isinstance(message_id, int) and isinstance(todo_id, int)

        # 首次通知由受理队列的守护线程投递（D-1）：排空队列即完成一次真实 socket 投递。
        assert ctx.pipeline.drain(timeout=5.0) is True
        assert webhook_stub.wait_for(1, timeout=5.0) is True
        assert webhook_stub.count() == 1

        payload = webhook_stub.payloads()[0]
        assert payload["title"] == "磁盘使用率 92%"
        assert payload["level"] == "error"
        assert payload["source"] == "disk-check"
        assert "请清理 /var/log" in payload["body"]
        assert payload["kind"] == "first_notice"
        # 冻结文案：首次通知的 NotificationMessage.todo_id 为 None（待办 id 只挂在投递记录上）
        assert payload["todo_id"] is None
        assert payload["overdue_seconds"] is None
        assert payload["category"] == "uncategorized"

        message = ctx.messages.get(message_id)
        assert message is not None
        sent_at = _from_iso(payload["occurred_at"])
        assert sent_at == as_utc(message.received_at)

        request_item = webhook_stub.requests[0]
        assert request_item["path"] == "/notify"
        assert request_item["headers"]["content-type"] == "application/json"

        todo = ctx.todos.get(todo_id)
        assert todo is not None and todo.status == "pending"

        records = ctx.messages.deliveries(message_id)
        assert len(records) == 1
        assert records[0].ok is True
        assert records[0].channel_id == "hook"
        assert records[0].todo_id == todo_id
        assert records[0].is_preferred is True  # 分类的 channel 参与者是"首选"
        assert records[0].is_fallback is False
        assert records[0].event == "first_notice"
        assert records[0].receipt  # 桩返回 {"code": 0, "msg": "ok"}


# --------------------------------------------------------------------------- #
# 3. 慢渠道不阻塞受理响应（D-1：进程内队列 + 守护线程）
# --------------------------------------------------------------------------- #
def test_accept_response_is_not_blocked_by_slow_real_channel(tmp_path):
    with WebhookStub(delay=1.0) as slow_stub:
        settings = _make_settings(
            tmp_path,
            channels=[_webhook_channel()],
            default_channel="hook",
            defaults_channel="hook",
            env={"STUB_URL": slow_stub.url},
        )
        app = create_app(settings)
        ctx = app.state.ctx
        with TestClient(app) as client:
            started = time.monotonic()
            response = client.post(
                "/api/v1/messages",
                json={"source": "s", "title": "慢渠道", "level": "warning"},
            )
            elapsed = time.monotonic() - started
            assert response.status_code == 202
            assert elapsed < 0.5, f"受理响应被投递阻塞：{elapsed:.3f}s"

            assert ctx.pipeline.drain(timeout=5.0) is True
            assert slow_stub.wait_for(1, timeout=5.0) is True


# --------------------------------------------------------------------------- #
# 4. 汇总链路：ReminderScheduler → DigestService → TodoService → DeliveryService
#    → 真实 webhook（真实 socket）
#
#    **改写自已被移除的「单项超时间隔提醒」用例。** 旧用例测的是：
#    (a) 每条待办各自经过 first_reminder_after_seconds 后分别收到提醒；
#    (b) 提醒文案的标题/正文含该待办自己的超时时长；
#    (c) 同一轮内不重复发送该待办；
#    (d) 再过 reminder_interval_seconds 收到第二次提醒。
#    以上行为随 feature 一并移除。新语义断言：越过每日触发时刻后**恰好一条**汇总，
#    正文含全部未完成待办的标题，逐条记账，汇总记录不归属任何待办。
# --------------------------------------------------------------------------- #
def test_digest_chain_reaches_real_webhook_once_with_all_titles(
    tmp_path, webhook_stub, manual_clock
):
    settings = _make_settings(
        tmp_path,
        channels=[_webhook_channel()],
        default_channel="hook",
        defaults_channel="hook",
        reminders=_DIGEST_AT_0900,
        env={"STUB_URL": webhook_stub.url},
    )
    # 真实 create_app + 真实 lifespan；scan_interval=3600 保证后台扫描线程本轮不抢跑，
    # 由测试用 run_once() 精确跨过触发时刻。
    # **必须把测试时钟注入组合根**：``create_app(settings)`` 内部会走
    # ``build_context(settings, clock=None)`` → ``SystemClock()``，那样调度器看的是真实
    # 系统时间，本用例的 ``manual_clock.advance(...)`` 全部无效、结果按当天钟点飘。
    # 不注入 registry：渠道仍从配置真实构造。
    ctx = build_context(settings, clock=manual_clock)
    app = create_app(ctx=ctx)

    with TestClient(app) as client:
        accepted = []
        for index, title in enumerate(("备份失败", "磁盘将满"), start=1):
            response = client.post(
                "/api/v1/messages",
                json={
                    "source": "db-backup",
                    "title": title,
                    "body": f"detail-{index}",
                    "level": "error",
                    "need_ack": True,
                    "dedup_key": f"backup-{index}",
                },
            )
            assert response.status_code == 202
            accepted.append(response.json())

        # 首次通知由生产 worker 线程经真实 socket 投递（每条一条，共 2 条）
        assert ctx.pipeline.drain(timeout=5.0) is True
        assert webhook_stub.wait_for(2, timeout=5.0) is True
        assert webhook_stub.count() == 2

        # 未到触发时刻（本地 08:59:59 < 09:00）：不发送
        # （取代旧的「未到 first_reminder_after_seconds 门槛不打扰」）
        manual_clock.advance(3599)
        assert ctx.scheduler.run_once() == 0
        assert webhook_stub.count() == 2

        # 越过触发时刻：一条汇总覆盖两条待办
        manual_clock.advance(1)
        assert ctx.scheduler.run_once() == 1
        assert webhook_stub.wait_for(3, timeout=5.0) is True
        assert webhook_stub.count() == 3, "汇总必须恰好一条请求"
        digest = webhook_stub.payloads()[2]
        assert digest["kind"] == "reminder"
        assert digest["title"] == "[待办汇总] 2 项未完成"
        assert "备份失败" in digest["body"]
        assert "磁盘将满" in digest["body"]
        assert "已超时" in digest["body"]
        # 汇总不归属任何单条待办/消息（旧行为：提醒的 todo_id 是该待办 id）
        assert digest["todo_id"] is None
        assert digest["overdue_seconds"] is None

        # 同一自然日不重复发送（取代旧的「同一轮内不重复发送该待办」）
        assert ctx.scheduler.run_once() == 0
        assert webhook_stub.count() == 3

        # 逐条记账：每条待办提醒次数 +1、last_notified_at 更新、各一条 REMINDER 事件
        for body in accepted:
            todo_id = body["todo_id"]
            todo = ctx.todos.get(todo_id)
            assert todo.reminder_count == 1
            assert as_utc(todo.last_notified_at) == manual_clock.now()
            events = ctx.todos.detail(todo_id).events
            assert [event.kind for event in events] == ["created", "reminder"]
            assert events[1].channel_id == "hook"
            assert events[1].delivery_ok is True
            # 汇总投递记录没有被记到这条消息/待办名下
            assert [record.event for record in ctx.messages.deliveries(body["message_id"])] == [
                "first_notice"
            ]

        # 汇总本身恰好一条投递记录，且不归属于任何待办/消息
        digest_records = _delivery_records(ctx, event="reminder")
        assert len(digest_records) == 1
        assert digest_records[0].todo_id is None
        assert digest_records[0].message_id is None
        assert digest_records[0].channel_id == "hook"
        assert digest_records[0].ok is True


# --------------------------------------------------------------------------- #
# 5. 调度器线程生命周期 + 真实后台扫描发出汇总（M6 明确移交阶段 D）
#
#    **改写自旧的单项提醒线程用例。** 旧行为（已移除）：真实扫描线程按
#    first_reminder_after_seconds 为**单条**待办发出提醒。新语义：线程在越过每日触发
#    时刻后发出**一条**汇总。
# --------------------------------------------------------------------------- #
def test_scheduler_thread_lifecycle_sends_digest_from_background_thread(
    tmp_path, webhook_stub, manual_clock
):
    settings = _make_settings(
        tmp_path,
        channels=[_webhook_channel()],
        default_channel="hook",
        defaults_channel="hook",
        reminders=_DIGEST_PAST_RAPID,
        env={"STUB_URL": webhook_stub.url},
    )
    # 刻意不走 lifespan：必须先受理并落库待办，再手动 start 扫描线程，否则 0.05s 的首轮
    # 扫描会在「当天无待办」时把该自然日定案（该定案行为由 tests/test_digest.py 覆盖），
    # 反而掩盖了「线程能发出汇总」这条集成结论。
    ctx = build_test_context(settings, clock=manual_clock)
    scheduler = ctx.scheduler

    with TestClient(create_api_app(ctx)) as client:
        accepted = client.post(
            "/api/v1/messages",
            json={"source": "s", "title": "到期待办", "need_ack": True, "dedup_key": "k1"},
        )
        assert accepted.status_code == 202
        assert webhook_stub.count() == 1  # run_inline=True：首次通知同步完成

        # 让本地时间确定越过触发时刻（08:00），再交给真实扫描线程
        manual_clock.advance(600)

        assert scheduler.running is False
        scheduler.start()
        assert scheduler.running is True
        assert "notify-hub-reminder-scheduler" in {
            thread.name for thread in threading.enumerate()
        }

        # 真实后台线程的有界轮询：汇总必须由线程自己发出
        assert webhook_stub.wait_for(2, timeout=5.0) is True, "后台扫描线程未发出汇总"
        digest = webhook_stub.payloads()[1]
        assert digest["kind"] == "reminder"
        assert digest["title"] == "[待办汇总] 1 项未完成"
        assert "到期待办" in digest["body"]
        assert digest["todo_id"] is None

        # stop 在数秒内返回，线程真正消失
        started = time.monotonic()
        scheduler.stop()
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, f"stop() 耗时 {elapsed:.3f}s"
        assert scheduler.running is False
        assert "notify-hub-reminder-scheduler" not in {
            thread.name for thread in threading.enumerate()
        }

        # 重复 stop 不抛异常、状态不变
        scheduler.stop()
        scheduler.stop()
        assert scheduler.running is False

        # 停止后即便时钟继续前进也不再产生请求
        # （取代旧的「停止后不再按间隔提醒」；这里同时含当日去重）
        manual_clock.advance(600)
        time.sleep(0.3)
        assert webhook_stub.count() == 2


# --------------------------------------------------------------------------- #
# 6. 规则热加载（生产轮询线程）→ 新规则生效 → 新消息按新规则分类
# --------------------------------------------------------------------------- #
def test_rule_hot_reload_changes_category_of_later_messages(
    tmp_path, webhook_stub, manual_clock
):
    rules_path = tmp_path / "rules.yaml"
    v1 = [
        {
            "id": "v1-alert",
            "match": {"title_contains": ["初始"]},
            "category": "initial-cat",
            "labels": ["v1"],
            "need_ack": False,
            "channel": "hook",
        }
    ]
    v2 = [
        {
            "id": "v2-alert",
            "match": {"title_contains": ["初始"]},
            "category": "hot-cat",
            "labels": ["v2"],
            "need_ack": False,
            "channel": "hook",
        }
    ]
    settings = _make_settings(
        tmp_path,
        channels=[_webhook_channel()],
        default_channel="hook",
        reminders=_DIGEST_NOT_YET,
        env={"STUB_URL": webhook_stub.url},
        rules=v1,
        rules_poll=0.05,
    )
    assert settings.rules_path == rules_path

    ctx = build_context(settings, clock=manual_clock)
    app = create_app(ctx=ctx)
    assert ctx.classifier.ruleset.rules[0].id == "v1-alert"
    assert ctx.classifier.last_error is None

    with TestClient(app) as client:
        first = client.post(
            "/api/v1/messages", json={"source": "mon", "title": "初始告警"}
        )
        assert first.status_code == 202
        first_id = first.json()["message_id"]
        assert client.get(f"/api/v1/messages/{first_id}").json()["category"] == "initial-cat"

        # 服务运行中改写规则文件，并显式前移 mtime（不依赖真实等待文件系统时间戳变化）
        _write_rules(rules_path, v2)
        bumped = rules_path.stat().st_mtime_ns + 5_000_000_000
        os.utime(rules_path, ns=(bumped, bumped))

        # 只观察，不主动 poll_once：唯一可能替规则集的只有生产轮询线程
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if (
                ctx.classifier.classify(
                    source="mon", level=Level.INFO, title="初始告警"
                ).category
                == "hot-cat"
            ):
                break
            time.sleep(0.02)

        assert (
            ctx.classifier.classify(
                source="mon", level=Level.INFO, title="初始告警"
            ).category
            == "hot-cat"
        ), "规则热加载轮询线程未在 5 秒内替换规则集"
        assert [rule.id for rule in ctx.classifier.ruleset.rules] == ["v2-alert"]

        second = client.post(
            "/api/v1/messages", json={"source": "mon", "title": "初始告警"}
        )
        assert second.status_code == 202
        second_id = second.json()["message_id"]
        second_out = client.get(f"/api/v1/messages/{second_id}").json()
        assert second_out["category"] == "hot-cat"
        assert second_out["rule_id"] == "v2-alert"
        assert second_out["labels"] == ["v2"]

        # 已受理消息的分类结论是快照，不被热加载改写
        assert (
            client.get(f"/api/v1/messages/{first_id}").json()["category"] == "initial-cat"
        )

        assert webhook_stub.wait_for(2, timeout=5.0) is True


# --------------------------------------------------------------------------- #
# 7. 跨模块错误传播：全部渠道不可用
# --------------------------------------------------------------------------- #
def test_all_channels_unavailable_still_persists_and_records_failure(
    tmp_path, manual_clock
):
    settings = _make_settings(
        tmp_path,
        channels=[_webhook_channel(env_name="MISSING_URL_ENV")],
        default_channel="hook",
        reminders=_DIGEST_NOT_YET,
        env={},  # 凭据缺失 → 渠道不可用，但不是启动错误
    )
    ctx = build_context(settings, clock=manual_clock)
    assert ctx.registry.available_ids() == ()
    assert "凭据缺失" in ctx.registry.unavailable_reasons()["hook"]

    app = create_app(ctx=ctx)
    with TestClient(app) as client:
        accepted = []
        for index in (1, 2):
            response = client.post(
                "/api/v1/messages",
                json={
                    "source": "net",
                    "title": f"链路中断 {index}",
                    "level": "error",
                    "need_ack": True,
                    "dedup_key": f"net-{index}",
                },
            )
            assert response.status_code == 202
            accepted.append(response.json())

        assert ctx.pipeline.drain(timeout=5.0) is True

        # 渠道故障不影响探活与查询
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/v1/todos").json()["total"] == 2

        for body in accepted:
            message_id, todo_id = body["message_id"], body["todo_id"]
            assert isinstance(todo_id, int)

            message = ctx.messages.get(message_id)
            assert message is not None
            todo = ctx.todos.get(todo_id)
            assert todo is not None and todo.status == "pending"

            detail = client.get(f"/api/v1/messages/{message_id}").json()
            assert len(detail["deliveries"]) == 1
            record = detail["deliveries"][0]
            assert record["ok"] is False
            assert record["channel_id"] is None
            assert record["error_reason"] == "没有可用的通知渠道"

        # 前一条失败不阻断后续消息（两条都被受理、分类、落库并尝试投递）
        assert ctx.messages.count() == 2
        assert len(ctx.todos.list()) == 2


# --------------------------------------------------------------------------- #
# 8. 首选渠道不可用 → 降级到默认渠道（分类 → 待办 → 投递三层的真实传递）
# --------------------------------------------------------------------------- #
def test_preferred_channel_unavailable_falls_back_to_default_over_socket(
    tmp_path, webhook_stub, manual_clock
):
    channels = [
        _webhook_channel("hook", "STUB_URL"),
        _webhook_channel("pager", "PAGER_URL"),
    ]
    rules = [
        {
            "id": "pager-rule",
            "match": {"source": ["fallback-src"]},
            "category": "pager-cat",
            "labels": ["pager"],
            "need_ack": True,
            "channel": "pager",
        }
    ]
    settings = _make_settings(
        tmp_path,
        channels=channels,
        default_channel="hook",
        reminders=_DIGEST_NOT_YET,
        env={"STUB_URL": webhook_stub.url},  # PAGER_URL 未设置 → pager 不可用
        rules=rules,
    )
    ctx = build_context(settings, clock=manual_clock)
    assert ctx.registry.available_ids() == ("hook",)
    app = create_app(ctx=ctx)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/messages",
            json={"source": "fallback-src", "title": "支付网关 5xx", "level": "error"},
        )
        assert response.status_code == 202
        message_id, todo_id = response.json()["message_id"], response.json()["todo_id"]
        assert isinstance(todo_id, int)

        assert ctx.pipeline.drain(timeout=5.0) is True
        assert webhook_stub.wait_for(1, timeout=5.0) is True
        payload = webhook_stub.payloads()[0]
        assert payload["category"] == "pager-cat"  # 规则分类跨模块到达投递层
        assert payload["title"] == "支付网关 5xx"

        detail = client.get(f"/api/v1/messages/{message_id}").json()
        assert detail["preferred_channel"] == "pager"
        assert detail["ack_reason"] == "rule"  # 规则（而非调用方）决定入待办
        assert len(detail["deliveries"]) == 1
        record = detail["deliveries"][0]
        assert record["ok"] is True
        assert record["channel_id"] == "hook"
        assert record["is_preferred"] is False
        assert record["is_fallback"] is True
        assert "pager" in record["fallback_reason"]

        todo = ctx.todos.get(todo_id)
        assert todo is not None
        assert todo.preferred_channel == "pager"  # 待办自带首选渠道（D-5）
