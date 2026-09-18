"""M4 通知投递层 · ``DeliveryService`` 的渠道选择 / 降级 / 投递记录 模块测试。

作用域：**模块测试，实现尚不存在**（阶段 A）。依据
``openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md`` 第 6 节「模块 M4」第 2 段（候选渠道顺序、
降级原因文案、投递记录规则）与第 4 段第 9–14 条，以及
``specs/notification-delivery/spec.md`` 的「渠道注册与选择」「投递记录」「凭据管理」
「投递失败不阻塞后续处理」。

所有对实现的导入都在函数体内（architecture.md 3.2：阶段 A 不得在收集阶段失败）。
渠道一律使用**真实的** ``WebhookNotifier`` + ``httpx.MockTransport``，不访问外网，
也不使用记录型替身来测投递层自身。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from notify_hub.clock import as_utc

UTC = timezone.utc
MOMENT = datetime(2024, 5, 1, 12, 0, 0, tzinfo=UTC)
SECRET = "SECRETTOKEN"


# --------------------------------------------------------------------------- #
# 测试辅助
# --------------------------------------------------------------------------- #
def _new_message(**overrides: Any):
    from notify_hub.domain import Level
    from notify_hub.notifiers.base import NotificationMessage

    values: dict[str, Any] = {
        "title": "磁盘空间不足",
        "body": "根分区使用率 95%",
        "level": Level.ERROR,
        "source": "alert-svc",
        "occurred_at": MOMENT,
    }
    values.update(overrides)
    return NotificationMessage(**values)


class _WebhookSpy:
    def __init__(self, responder: Callable[[httpx.Request], httpx.Response]) -> None:
        self._responder = responder
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._responder(request)

        return httpx.MockTransport(handler)


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "msg": "ok"})


def _failing(channel_id: str):
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"code": 500, "msg": f"{channel_id} 故障"})

    return responder


def _webhook(
    channel_id: str,
    responder: Callable[[httpx.Request], httpx.Response],
    *,
    url: str = "https://hook.invalid/notify",
    secrets=(),
):
    from notify_hub.notifiers.webhook import WebhookNotifier

    spy = _WebhookSpy(responder)
    notifier = WebhookNotifier(channel_id, url, transport=spy.transport(), secrets=tuple(secrets))
    return notifier, spy


def _service(db, registry, *, default_channel, clock, secrets=(), logger=None):
    from notify_hub.delivery import DeliveryService

    return DeliveryService(
        db,
        registry,
        default_channel=default_channel,
        channel_order=registry.ids(),
        clock=clock,
        logger=logger or logging.getLogger("notify_hub.tests.delivery"),
        secrets=tuple(secrets),
    )


def _records(db, **filters: Any) -> list[Any]:
    """按条件读投递记录，按 ``attempted_at`` 升序（时间列需经 ``as_utc`` 归一化）。"""
    from sqlmodel import select

    from notify_hub.models import DeliveryRecord

    statement = select(DeliveryRecord)
    for key, value in filters.items():
        statement = statement.where(getattr(DeliveryRecord, key) == value)
    with db.session() as session:
        rows = list(session.execute(statement).scalars().all())
    return sorted(rows, key=lambda row: (as_utc(row.attempted_at), row.id or 0))


def _seed_message(db, message_id: int = 1) -> int:
    """投递记录对 messages 有外键（M2 开启 PRAGMA foreign_keys），先落一行消息。"""
    from notify_hub.models import Message

    row = Message(
        id=message_id,
        source="alert-svc",
        title="磁盘空间不足",
        body="根分区使用率 95%",
        level="error",
        need_ack_declared=False,
        dedup_key=None,
        occurred_at=MOMENT,
        received_at=MOMENT,
        meta={},
        rule_id=None,
        category=None,
        labels=[],
        needs_ack=None,
        ack_reason=None,
        preferred_channel=None,
    )
    with db.session() as session:
        session.add(row)
    return message_id


def _seed_todo(db, message_id: int, *, todo_id: int = 7) -> int:
    from notify_hub.models import Todo

    row = Todo(
        id=todo_id,
        message_id=message_id,
        source="alert-svc",
        dedup_key=None,
        category=None,
        title="磁盘空间不足",
        status="pending",
        ack_reason="caller_declared",
        preferred_channel=None,
        created_at=MOMENT,
        first_notified_at=MOMENT,
        last_notified_at=MOMENT,
        reminder_count=0,
        completed_at=None,
    )
    with db.session() as session:
        session.add(row)
    return todo_id


def _registry_with(*notifiers):
    from notify_hub.notifiers import NotifierRegistry

    registry = NotifierRegistry()
    for notifier in notifiers:
        registry.register(notifier)
    return registry


# --------------------------------------------------------------------------- #
# 第 9 条：成功投递与记录
# --------------------------------------------------------------------------- #
def test_successful_delivery_uses_preferred_channel_and_writes_one_record(db, manual_clock):
    from notify_hub.domain import DeliveryEvent

    message_id = _seed_message(db)
    notifier, spy = _webhook("primary", _ok)
    service = _service(db, _registry_with(notifier), default_channel="primary", clock=manual_clock)

    outcome = service.deliver(_new_message(), preferred_channel="primary", message_id=message_id)

    assert outcome.ok is True
    assert outcome.channel_id == "primary"
    assert outcome.is_preferred is True
    assert outcome.is_fallback is False
    assert outcome.fallback_reason is None
    assert outcome.error_reason is None
    assert outcome.receipt
    assert tuple(outcome.attempted_channels) == ("primary",)
    assert len(spy.requests) == 1

    records = _records(db, message_id=message_id)
    assert len(records) == 1
    record = records[0]
    assert bool(record.ok) is True
    assert record.channel_id == "primary"
    assert bool(record.is_preferred) is True
    assert bool(record.is_fallback) is False
    assert record.fallback_reason is None
    assert record.error_reason is None
    assert record.event == DeliveryEvent.FIRST_NOTICE.value


def test_reminder_delivery_records_todo_id_and_event(db, manual_clock):
    from notify_hub.domain import DeliveryEvent

    message_id = _seed_message(db)
    todo_id = _seed_todo(db, message_id)
    notifier, spy = _webhook("primary", _ok)
    service = _service(db, _registry_with(notifier), default_channel="primary", clock=manual_clock)

    msg = _new_message(kind=DeliveryEvent.REMINDER, todo_id=todo_id, overdue_seconds=1800.0)
    outcome = service.deliver(
        msg, preferred_channel="primary", message_id=message_id, todo_id=todo_id
    )

    assert outcome.ok is True
    assert len(spy.requests) == 1
    records = _records(db, todo_id=todo_id)
    assert len(records) == 1
    assert records[0].message_id == message_id
    assert records[0].event == DeliveryEvent.REMINDER.value


# --------------------------------------------------------------------------- #
# 第 10 条：首选不可用 → 降级（且不为它写记录）
# --------------------------------------------------------------------------- #
def test_unavailable_preferred_channel_falls_back_without_recording_it(db, manual_clock):
    from notify_hub.config import ChannelSpec

    message_id = _seed_message(db)
    registry = _registry_with()
    registry.build_from_specs([ChannelSpec(id="missing", type="webhook", enabled=False)])
    notifier, spy = _webhook("default-wh", _ok)
    registry.register(notifier)
    service = _service(db, registry, default_channel="default-wh", clock=manual_clock)

    outcome = service.deliver(_new_message(), preferred_channel="missing", message_id=message_id)

    assert outcome.ok is True
    assert outcome.channel_id == "default-wh"
    assert outcome.is_fallback is True
    assert "missing" in (outcome.fallback_reason or "")
    assert "default-wh" not in (outcome.fallback_reason or "")
    assert tuple(outcome.attempted_channels) == ("default-wh",)
    assert len(spy.requests) == 1

    records = _records(db, message_id=message_id)
    assert len(records) == 1
    assert records[0].channel_id == "default-wh"
    assert bool(records[0].is_fallback) is True
    assert all(record.channel_id != "missing" for record in records), "未尝试的首选渠道不得写记录"


# --------------------------------------------------------------------------- #
# 第 11 条：首选已尝试但失败 → 先记失败，再降级
# --------------------------------------------------------------------------- #
def test_failed_preferred_channel_is_recorded_before_fallback_succeeds(db, manual_clock):
    message_id = _seed_message(db)
    preferred, preferred_spy = _webhook("preferred-wh", _failing("preferred-wh"))
    fallback, fallback_spy = _webhook("default-wh", _ok)
    registry = _registry_with(preferred, fallback)
    service = _service(db, registry, default_channel="default-wh", clock=manual_clock)

    outcome = service.deliver(_new_message(), preferred_channel="preferred-wh", message_id=message_id)

    assert outcome.ok is True
    assert outcome.channel_id == "default-wh"
    assert outcome.is_fallback is True
    assert "preferred-wh" in (outcome.fallback_reason or "")
    assert tuple(outcome.attempted_channels) == ("preferred-wh", "default-wh")
    assert len(preferred_spy.requests) == 1
    assert len(fallback_spy.requests) == 1

    records = _records(db, message_id=message_id)
    assert len(records) == 2
    first, second = records
    assert first.channel_id == "preferred-wh"
    assert bool(first.ok) is False
    assert bool(first.is_preferred) is True
    assert bool(first.is_fallback) is False
    assert first.fallback_reason is None
    assert "500" in (first.error_reason or "")
    assert second.channel_id == "default-wh"
    assert bool(second.ok) is True
    assert bool(second.is_fallback) is True
    assert "preferred-wh" in (second.fallback_reason or "")


# --------------------------------------------------------------------------- #
# 候选渠道顺序：default → channel_order 其余（去重保序）
# --------------------------------------------------------------------------- #
def test_candidate_order_is_default_then_remaining_channel_order(db, manual_clock):
    message_id = _seed_message(db)
    spies: dict[str, _WebhookSpy] = {}
    notifiers = []
    for channel_id in ("alpha", "beta", "gamma"):
        notifier, spy = _webhook(channel_id, _failing(channel_id))
        notifiers.append(notifier)
        spies[channel_id] = spy
    service = _service(db, _registry_with(*notifiers), default_channel="beta", clock=manual_clock)

    outcome = service.deliver(_new_message(), message_id=message_id)

    assert outcome.ok is False
    assert outcome.error_reason
    assert tuple(outcome.attempted_channels) == ("beta", "alpha", "gamma")
    assert {cid: len(spy.requests) for cid, spy in spies.items()} == {
        "alpha": 1,
        "beta": 1,
        "gamma": 1,
    }
    records = _records(db, message_id=message_id)
    assert [record.channel_id for record in records] == ["beta", "alpha", "gamma"]
    assert all(bool(record.ok) is False for record in records)
    assert all(bool(record.is_fallback) is False for record in records), "preferred 为空时不属于降级"


# --------------------------------------------------------------------------- #
# 第 12 条：零候选渠道
# --------------------------------------------------------------------------- #
def test_no_candidate_channel_writes_exactly_one_null_record(db, manual_clock):
    from notify_hub.notifiers import NotifierRegistry

    message_id = _seed_message(db)
    service = _service(db, NotifierRegistry(), default_channel=None, clock=manual_clock)

    outcome = service.deliver(_new_message(), message_id=message_id)

    assert outcome.ok is False
    assert outcome.channel_id is None
    assert tuple(outcome.attempted_channels) == ()
    assert outcome.error_reason

    records = _records(db, message_id=message_id)
    assert len(records) == 1
    assert records[0].channel_id is None
    assert bool(records[0].ok) is False
    assert records[0].error_reason == "没有可用的通知渠道"


# --------------------------------------------------------------------------- #
# 第 13 条：脱敏（安全关键）
# --------------------------------------------------------------------------- #
def test_webhook_failure_is_redacted_in_records_and_logs(db, manual_clock, caplog):
    message_id = _seed_message(db)
    url = f"https://hook.invalid/notify?access_token={SECRET}"
    notifier, spy = _webhook("secure-wh", _failing("secure-wh"), url=url, secrets=(SECRET,))
    service = _service(
        db, _registry_with(notifier), default_channel="secure-wh", clock=manual_clock, secrets=(SECRET,)
    )

    caplog.set_level(logging.DEBUG)
    outcome = service.deliver(_new_message(), message_id=message_id)

    assert outcome.ok is False
    assert SECRET not in (outcome.error_reason or "")
    records = _records(db, message_id=message_id)
    assert records
    for record in records:
        assert SECRET not in (record.error_reason or "")
        assert SECRET not in (record.fallback_reason or "")
    assert SECRET not in caplog.text, "日志中的失败原因不得含 webhook token"


def test_transport_error_message_is_redacted_in_record_and_logs(db, manual_clock, caplog):
    message_id = _seed_message(db)
    url = f"https://hook.invalid/notify?access_token={SECRET}"

    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"连接失败: {request.url}")

    notifier, spy = _webhook("secure-wh", responder, url=url, secrets=(SECRET,))
    service = _service(
        db, _registry_with(notifier), default_channel="secure-wh", clock=manual_clock, secrets=(SECRET,)
    )

    caplog.set_level(logging.DEBUG)
    outcome = service.deliver(_new_message(), message_id=message_id)

    assert outcome.ok is False
    records = _records(db, message_id=message_id)
    assert len(records) == 1
    assert "ConnectError" in (records[0].error_reason or "")
    assert SECRET not in (records[0].error_reason or "")
    assert SECRET not in caplog.text


# --------------------------------------------------------------------------- #
# 第 14 条：首次通知失败不重试（可观察形式 = 每个候选渠道的尝试次数）
# --------------------------------------------------------------------------- #
def test_failed_first_notice_is_not_retried(db, manual_clock):
    message_id = _seed_message(db)
    preferred, preferred_spy = _webhook("preferred-wh", _failing("preferred-wh"))
    fallback, fallback_spy = _webhook("default-wh", _failing("default-wh"))
    service = _service(
        db, _registry_with(preferred, fallback), default_channel="default-wh", clock=manual_clock
    )

    outcome = service.deliver(_new_message(), preferred_channel="preferred-wh", message_id=message_id)

    candidates = ("preferred-wh", "default-wh")
    assert outcome.ok is False
    assert tuple(outcome.attempted_channels) == candidates
    assert len(preferred_spy.requests) == 1, "首选渠道不得被重复尝试"
    assert len(fallback_spy.requests) == 1, "默认渠道不得被重复尝试"

    records_after_first_call = _records(db, message_id=message_id)
    assert len(records_after_first_call) == len(candidates), "每次实际投递恰写一条记录，无额外尝试"

    # 只有显式再次调用才会产生新尝试；两次调用之间不存在隐式重试或重放
    service.deliver(_new_message(), preferred_channel="preferred-wh", message_id=message_id)
    assert len(_records(db, message_id=message_id)) == 2 * len(candidates)
    assert len(preferred_spy.requests) == 2
    assert len(fallback_spy.requests) == 2


def test_deliver_never_raises_when_adapter_raises(db, manual_clock):
    """适配器违反契约抛异常时，DeliveryService 也必须返回失败结果而不是把异常传给上层。"""
    from notify_hub.notifiers import NotifierRegistry

    class _Exploding:
        channel_id = "exploding"

        def capabilities(self):
            from notify_hub.notifiers.base import ChannelCapabilities

            return ChannelCapabilities()

        def send(self, msg):
            raise RuntimeError(f"适配器内部错误 {SECRET}")

    message_id = _seed_message(db)
    registry = NotifierRegistry()
    registry.register(_Exploding())
    service = _service(db, registry, default_channel="exploding", clock=manual_clock, secrets=(SECRET,))

    outcome = service.deliver(_new_message(), message_id=message_id)

    assert outcome.ok is False
    assert SECRET not in (outcome.error_reason or "")
    records = _records(db, message_id=message_id)
    assert len(records) == 1
    assert bool(records[0].ok) is False
    assert SECRET not in (records[0].error_reason or "")


# --------------------------------------------------------------------------- #
# 第 13 条 b：平台回显**裸 token**（已确认 P1 的真实形态，回归闸门）
# --------------------------------------------------------------------------- #
#: 与 ``config.example.yaml`` 同形状的最小配置：webhook 凭据值 = 整条 URL。
_PRODUCTION_CONFIG_YAML = """\
server:
  host: 127.0.0.1
  port: 8000
  log_level: INFO
storage:
  db_path: ./data/notify.db
rules:
  path: ./rules.yaml
  poll_interval_seconds: 5
reminders:
  scan_interval_seconds: 60
  first_reminder_after_seconds: 1800
  reminder_interval_seconds: 3600
default_channel: secure-wh
channels:
  - id: secure-wh
    type: webhook
    enabled: true
    params:
      url_env: NOTIFY_WEBHOOK_URL
      field_map: {}
      headers: {}
    credentials:
      url: NOTIFY_WEBHOOK_URL
"""


def _production_url_and_secrets(tmp_path):
    """按**生产装配**的形态取 url 与 secrets：``load_settings`` → ``credential_values``。

    刻意**不**手写 ``secrets=(SECRET,)``：原测试作者正是用了这种生产中不存在的形态，
    才让「裸 token 匹配不上 → 明文落进 DeliveryRecord.error_reason」的 P1 逃过 166 个绿灯。
    """
    from notify_hub.config import credential_values, load_settings

    url = f"https://h/p?access_token={SECRET}"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(_PRODUCTION_CONFIG_YAML, encoding="utf-8")
    settings = load_settings(config_path, env={"NOTIFY_WEBHOOK_URL": url})
    return settings.channels[0].credentials["url"], credential_values(settings)


def test_business_error_echoing_bare_token_never_reaches_record_or_logs(
    db, manual_clock, caplog, tmp_path
):
    """13b 全链路：业务失败回显裸 token → outcome / 落库记录 / 日志都不得含它。

    与 13a 的区别即缺陷本身：13a 用 HTTP 500，``error_reason`` 只有 ``HTTP 500``，
    响应体根本不进记录；这里用 HTTP 200 + 业务错误，裸 token 随 ``msg`` 进入 error_reason。
    """
    message_id = _seed_message(db)
    url, secrets = _production_url_and_secrets(tmp_path)
    assert SECRET in url, "前提：凭据值是内嵌裸 token 的完整 URL（生产形态）"

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 40001, "msg": f"invalid access_token {SECRET}"})

    notifier, spy = _webhook("secure-wh", responder, url=url, secrets=secrets)
    service = _service(
        db,
        _registry_with(notifier),
        default_channel="secure-wh",
        clock=manual_clock,
        secrets=secrets,
    )

    caplog.set_level(logging.DEBUG)
    outcome = service.deliver(_new_message(), message_id=message_id)

    assert len(spy.requests) == 1
    assert outcome.ok is False
    # 先证明平台 msg 确实流入了 error_reason，否则下面的「不含」会假通过
    assert "invalid access_token" in (outcome.error_reason or "")
    assert SECRET not in (outcome.error_reason or "")

    records = _records(db, message_id=message_id)
    assert len(records) == 1
    assert bool(records[0].ok) is False
    assert SECRET not in (records[0].error_reason or ""), "裸 token 不得落进 DeliveryRecord"
    assert SECRET not in (records[0].fallback_reason or "")
    assert SECRET not in caplog.text
