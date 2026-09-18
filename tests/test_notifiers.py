"""M4 通知投递层 · 契约 / 注册表 / webhook / email 的模块测试。

作用域：**模块测试，实现尚不存在**（阶段 A）。测试完全依据
``openspec/changes/add-notify-hub/architecture.md`` 第 4.6 与第 6 节「模块 M4」第 4 段，
以及 ``specs/notification-delivery/spec.md`` 编写。

两条硬约束：

* 所有对 ``notify_hub.notifiers`` / ``notify_hub.config`` 的导入都写在**测试函数体内**。
  阶段 A 时这些模块不存在，顶部导入会让 pytest 在**收集阶段**失败（architecture.md 3.2）。
* Webhook 只经 ``httpx.MockTransport``（绝不访问外网）；Email 只连本机 ``aiosmtpd``。
  ``Controller(port=0)`` 不绑定临时端口（architecture.md 1.3），因此端口必须先自行选取。
"""

from __future__ import annotations

import json
import logging
import smtplib
import socket
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from typing import Any, Callable

import httpx
import pytest

UTC = timezone.utc
MESSAGE_TIME = datetime(2024, 5, 1, 12, 0, 0, tzinfo=UTC)
SMTP_PASSWORD = "S3CRET-SMTP-PASSWORD"

#: 与 M6 ``notification_for_message`` **实际产出**同形的正文（``来源``/``分类``/``时间``/空行/原始正文）。
#: 渠道渲染规格必须按「拿到的就是完整正文」来写：拿简短的 ``"根分区使用率 95%"`` 当 body，
#: 适配器自己补一层表头也能通过，真实用户看到的 ``来源``/``时间`` 各出现两次就抓不到。
#: 该 ISO 形态由 M6 的 ``as_utc(...).isoformat()`` 产出。
FIRST_NOTICE_BODY = (
    "来源: alert-svc\n"
    "分类: disk\n"
    "时间: 2024-05-01T12:00:00+00:00\n"
    "\n"
    "根分区使用率 95%"
)

#: 提醒类的正文形如 M6 产出：``来源``/``分类``/``已超时``/``待办 id``/空行/原始正文。
REMINDER_BODY = (
    "来源: alert-svc\n"
    "分类: disk\n"
    "已超时: 1 小时 5 分钟\n"
    "待办 id: 7\n"
    "\n"
    "根分区使用率 95%"
)


# --------------------------------------------------------------------------- #
# 测试辅助（不在模块顶部导入实现）
# --------------------------------------------------------------------------- #
def _new_message(**overrides: Any):
    """按 4.6 的冻结契约构造一条 ``NotificationMessage``。"""
    from notify_hub.domain import Level
    from notify_hub.notifiers.base import NotificationMessage

    values: dict[str, Any] = {
        "title": "磁盘空间不足",
        "body": "根分区使用率 95%",
        "level": Level.ERROR,
        "source": "alert-svc",
        "occurred_at": MESSAGE_TIME,
    }
    values.update(overrides)
    return NotificationMessage(**values)


class _WebhookSpy:
    """记录 ``WebhookNotifier`` 实际发出的请求（MockTransport，无网络）。"""

    def __init__(self, responder: Callable[[httpx.Request], httpx.Response]) -> None:
        self._responder = responder
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._responder(request)

        return httpx.MockTransport(handler)

    @property
    def payloads(self) -> list[dict[str, Any]]:
        return [json.loads(r.content.decode("utf-8")) for r in self.requests]

    def payload(self) -> dict[str, Any]:
        assert self.requests, "WebhookNotifier 未发出任何请求"
        return self.payloads[0]


def _respond_json(status: int, payload: Any):
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return responder


def _respond_text(status: int, text: str):
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=text)

    return responder


def _new_webhook(spy: _WebhookSpy, *, channel_id: str = "wh", url: str = "https://hook.invalid/notify", **kwargs: Any):
    from notify_hub.notifiers.webhook import WebhookNotifier

    return WebhookNotifier(channel_id, url, transport=spy.transport(), **kwargs)


def _free_port() -> int:
    """先取空闲端口再交给 ``Controller``——``port=0`` 不绑定临时端口。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _SmtpSink:
    """本机 aiosmtpd 的收信夹具。"""

    def __init__(self) -> None:
        self.envelopes: list[Any] = []

    async def handle_DATA(self, server, session, envelope):  # noqa: N802 - aiosmtpd 约定的方法名
        self.envelopes.append(envelope)
        return "250 Message accepted for delivery"


def _header(parsed, name: str) -> str:
    raw = parsed.get(name)
    if raw is None:
        return ""
    return str(make_header(decode_header(raw)))


def _body_text(parsed) -> str:
    if parsed.is_multipart():
        chunks = [
            part.get_payload(decode=True) or b""
            for part in parsed.walk()
            if part.get_content_maintype() != "multipart"
        ]
        raw = b"".join(chunks)
    else:
        raw = parsed.get_payload(decode=True) or b""
    charset = parsed.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


# --------------------------------------------------------------------------- #
# 1. 冻结契约（architecture.md 4.6）
# --------------------------------------------------------------------------- #
def test_channel_capabilities_defaults():
    from notify_hub.notifiers.base import ChannelCapabilities

    caps = ChannelCapabilities()
    assert caps.supports_rich_text is False
    assert caps.max_body_length is None
    assert caps.supports_headers is False


def test_delivery_result_success_and_failure_factories():
    from notify_hub.notifiers.base import DeliveryResult

    ok = DeliveryResult.success()
    assert ok.ok is True
    assert ok.receipt is None
    assert ok.error_reason is None
    assert DeliveryResult.success("receipt-1").receipt == "receipt-1"

    bad = DeliveryResult.failure("渠道故障")
    assert bad.ok is False
    assert bad.receipt is None
    assert bad.error_reason == "渠道故障"


def test_notification_message_defaults_and_required_fields():
    from notify_hub.domain import DeliveryEvent, Level

    msg = _new_message()
    assert msg.title == "磁盘空间不足"
    assert msg.body == "根分区使用率 95%"
    assert msg.level is Level.ERROR
    assert msg.source == "alert-svc"
    assert msg.occurred_at == MESSAGE_TIME
    assert msg.kind is DeliveryEvent.FIRST_NOTICE
    assert msg.todo_id is None
    assert msg.overdue_seconds is None
    assert msg.category is None
    assert dict(msg.meta) == {}


def test_contract_dataclasses_are_frozen():
    import dataclasses

    from notify_hub.notifiers.base import ChannelCapabilities, DeliveryResult

    msg = _new_message()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ChannelCapabilities().supports_rich_text = True  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        DeliveryResult.success().ok = False  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        msg.title = "改标题"  # type: ignore[misc]


def test_notifier_protocol_is_satisfied_by_real_adapters():
    from notify_hub.notifiers.base import ChannelCapabilities, Notifier
    from notify_hub.notifiers.email import EmailNotifier
    from notify_hub.notifiers.webhook import WebhookNotifier

    webhook = WebhookNotifier("webhook", "https://hook.invalid/notify")
    email = EmailNotifier(
        "email", host="smtp.invalid", sender="notify@example.com", recipients=["me@example.com"]
    )

    for notifier in (webhook, email):
        assert isinstance(notifier, Notifier)
    assert webhook.channel_id == "webhook"
    assert isinstance(webhook.capabilities(), ChannelCapabilities)
    assert email.channel_id == "email"
    assert isinstance(email.capabilities(), ChannelCapabilities)


# --------------------------------------------------------------------------- #
# 2. 注册表（架构 6-M4 第 2/4 段，规格「渠道注册与选择」）
# --------------------------------------------------------------------------- #
def test_registry_register_get_ids_and_contains():
    from notify_hub.notifiers import NotifierRegistry
    from notify_hub.notifiers.webhook import WebhookNotifier

    registry = NotifierRegistry()
    first = WebhookNotifier("a", "https://hook.invalid/a")
    second = WebhookNotifier("b", "https://hook.invalid/b")
    registry.register(first)
    registry.register(second)

    assert registry.ids() == ("a", "b")
    assert registry.get("a") is first
    assert registry.get("b") is second
    assert registry.get("nope") is None
    assert "a" in registry
    assert "nope" not in registry
    assert registry.available_ids() == ("a", "b")


def test_registry_duplicate_id_overwrites_and_warns(caplog):
    from notify_hub.notifiers import NotifierRegistry
    from notify_hub.notifiers.webhook import WebhookNotifier

    registry = NotifierRegistry()
    first = WebhookNotifier("dup", "https://hook.invalid/1")
    second = WebhookNotifier("dup", "https://hook.invalid/2")
    registry.register(first)
    with caplog.at_level(logging.WARNING):
        registry.register(second)

    assert registry.get("dup") is second, "重复注册同一 channel_id 时后注册的生效"
    assert registry.ids().count("dup") == 1, "重复注册不得让 ids() 出现同一个渠道两次"
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "重复注册同一 channel_id 必须记录一条 warning 日志"


def test_build_from_specs_skips_disabled_unknown_type_and_missing_credentials():
    from notify_hub.config import ChannelSpec
    from notify_hub.notifiers import NotifierRegistry

    specs = [
        ChannelSpec(id="off", type="webhook", enabled=False),
        ChannelSpec(id="weird", type="carrier-pigeon"),
        ChannelSpec(id="nopw", type="email", credentials={"password": None}),
    ]
    registry = NotifierRegistry()
    registry.build_from_specs(specs)

    assert registry.available_ids() == ()
    reasons = dict(registry.unavailable_reasons())
    assert set(reasons) == {"off", "weird", "nopw"}
    assert "未启用" in reasons["off"]
    assert "未知的适配器类型" in reasons["weird"]
    assert "carrier-pigeon" in reasons["weird"]
    assert "凭据缺失" in reasons["nopw"]


def test_build_from_specs_calls_factory_with_spec_and_secrets(monkeypatch):
    from notify_hub.config import ChannelSpec
    from notify_hub.notifiers import NotifierRegistry
    from notify_hub.notifiers.registry import NOTIFIER_FACTORIES
    from notify_hub.notifiers.webhook import WebhookNotifier

    calls: list[tuple[Any, tuple[str, ...]]] = []

    def factory(spec, secrets):
        calls.append((spec, tuple(secrets)))
        return WebhookNotifier(spec.id, "https://hook.invalid/from-spec")

    monkeypatch.setitem(NOTIFIER_FACTORIES, "fake-type", factory)
    spec = ChannelSpec(id="fake-1", type="fake-type")
    registry = NotifierRegistry()
    registry.build_from_specs([spec], secrets=("S3CRET-VALUE",))

    assert registry.available_ids() == ("fake-1",)
    assert registry.get("fake-1") is not None
    assert len(calls) == 1
    assert calls[0][0] is spec
    assert calls[0][1] == ("S3CRET-VALUE",)
    assert registry.unavailable_reasons() == {}


def test_build_from_specs_builds_real_adapters_from_frozen_config_shape():
    """spec 形状取自 M1 的 ``config.example.yaml``（M4 未重申键名）：URL/密码在 credentials。"""
    from notify_hub.config import ChannelSpec
    from notify_hub.notifiers import NotifierRegistry
    from notify_hub.notifiers.email import EmailNotifier
    from notify_hub.notifiers.webhook import WebhookNotifier

    specs = [
        ChannelSpec(
            id="wh",
            type="webhook",
            params={
                "url_env": "NOTIFY_WEBHOOK_URL",
                "field_map": {"body": "text"},
                "headers": {"X-From": "config"},
            },
            credentials={"url": "https://hook.invalid/from-spec"},
        ),
        ChannelSpec(
            id="mail",
            type="email",
            params={
                "host": "smtp.invalid",
                "port": 587,
                "use_tls": True,
                "sender": "notify@example.com",
                "recipients": ["me@example.com"],
            },
            credentials={"password": "smtp-secret"},
        ),
    ]
    registry = NotifierRegistry()
    registry.build_from_specs(specs, secrets=("https://hook.invalid/from-spec", "smtp-secret"))

    assert registry.available_ids() == ("wh", "mail")
    assert registry.unavailable_reasons() == {}
    assert isinstance(registry.get("wh"), WebhookNotifier)
    assert isinstance(registry.get("mail"), EmailNotifier)


def test_build_from_specs_redacts_factory_failure(monkeypatch):
    from notify_hub.config import ChannelSpec
    from notify_hub.notifiers import NotifierRegistry
    from notify_hub.notifiers.registry import NOTIFIER_FACTORIES

    secret = "S3CRET-FACTORY-VALUE"

    def factory(spec, secrets):
        raise RuntimeError(f"构造失败，凭据 {secret} 无法使用")

    monkeypatch.setitem(NOTIFIER_FACTORIES, "exploding", factory)
    spec = ChannelSpec(id="boom", type="exploding")
    registry = NotifierRegistry()
    registry.build_from_specs([spec], secrets=(secret,))

    assert registry.available_ids() == ()
    reason = registry.unavailable_reasons()["boom"]
    assert "适配器构造失败" in reason
    assert secret not in reason, "工厂异常原因必须已脱敏"


# --------------------------------------------------------------------------- #
# 3. Webhook 适配器（architecture.md 6-M4 第 2/4 段）
# --------------------------------------------------------------------------- #
def test_webhook_success_returns_receipt_and_posts_json():
    spy = _WebhookSpy(_respond_json(200, {"code": 0, "msg": "ok"}))
    notifier = _new_webhook(spy, channel_id="wh")

    result = notifier.send(_new_message())

    assert result.ok is True, result.error_reason
    assert result.receipt
    assert result.error_reason is None
    request = spy.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://hook.invalid/notify"
    assert request.headers["content-type"].startswith("application/json")


def test_webhook_default_payload_keys_and_values():
    spy = _WebhookSpy(_respond_json(200, {"code": 0}))
    notifier = _new_webhook(spy, channel_id="wh")

    assert notifier.send(_new_message()).ok is True

    payload = spy.payload()
    assert payload["title"] == "磁盘空间不足"
    assert payload["body"] == "根分区使用率 95%"
    assert payload["level"] == "error"
    assert payload["source"] == "alert-svc"
    assert payload["occurred_at"].startswith("2024-05-01T12:00:00")
    assert payload["category"] is None
    assert payload["todo_id"] is None
    assert payload["overdue_seconds"] is None
    assert payload["kind"] == "first_notice"


def test_webhook_reminder_payload_carries_todo_and_overdue():
    from notify_hub.domain import DeliveryEvent

    spy = _WebhookSpy(_respond_json(200, {"code": 0}))
    notifier = _new_webhook(spy, channel_id="wh")

    msg = _new_message(
        kind=DeliveryEvent.REMINDER, todo_id=7, overdue_seconds=1800.0, category="backup-failure"
    )
    assert notifier.send(msg).ok is True

    payload = spy.payload()
    assert payload["kind"] == "reminder"
    assert payload["todo_id"] == 7
    assert payload["overdue_seconds"] == 1800.0
    assert payload["category"] == "backup-failure"


def test_webhook_field_map_renames_keys_and_keeps_the_rest():
    spy = _WebhookSpy(_respond_json(200, {"code": 0}))
    notifier = _new_webhook(spy, channel_id="wh", field_map={"body": "text", "title": "head"})

    assert notifier.send(_new_message()).ok is True

    payload = spy.payload()
    assert payload["head"] == "磁盘空间不足"
    assert payload["text"] == "根分区使用率 95%"
    assert "title" not in payload and "body" not in payload
    for default_key in ("level", "source", "occurred_at", "category", "todo_id", "overdue_seconds", "kind"):
        assert default_key in payload, f"未映射的默认键 {default_key} 必须保留"


def test_webhook_custom_headers_are_sent():
    spy = _WebhookSpy(_respond_json(200, {"code": 0}))
    notifier = _new_webhook(spy, channel_id="wh", headers={"X-Sign": "abc"})

    assert notifier.send(_new_message()).ok is True
    assert spy.requests[0].headers["X-Sign"] == "abc"


def test_webhook_business_error_uses_platform_message():
    spy = _WebhookSpy(_respond_json(200, {"code": 40001, "msg": "invalid sign"}))
    notifier = _new_webhook(spy, channel_id="wh")

    result = notifier.send(_new_message())

    assert result.ok is False
    assert "invalid sign" in (result.error_reason or "")


def test_webhook_business_error_without_message_mentions_code():
    spy = _WebhookSpy(_respond_json(200, {"code": 40001}))
    notifier = _new_webhook(spy, channel_id="wh")

    result = notifier.send(_new_message())

    assert result.ok is False
    assert "40001" in (result.error_reason or "")


def test_webhook_http_500_is_failure_with_status_code():
    spy = _WebhookSpy(_respond_json(500, {"code": 500, "msg": "server exploded"}))
    notifier = _new_webhook(spy, channel_id="wh")

    result = notifier.send(_new_message())

    assert result.ok is False
    assert "500" in (result.error_reason or "")


def test_webhook_non_json_body_only_http_status_counts():
    spy = _WebhookSpy(_respond_text(200, "<html>ok</html>"))
    notifier = _new_webhook(spy, channel_id="wh")

    result = notifier.send(_new_message())

    assert result.ok is True, result.error_reason


def test_webhook_connect_error_returns_failure_instead_of_raising():
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("连接被拒绝")

    spy = _WebhookSpy(responder)
    notifier = _new_webhook(spy, channel_id="wh")

    result = notifier.send(_new_message())

    assert result.ok is False
    assert result.error_reason
    assert "ConnectError" in result.error_reason


def test_webhook_unexpected_exception_returns_failure_instead_of_raising():
    def responder(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("内部爆炸")

    spy = _WebhookSpy(responder)
    notifier = _new_webhook(spy, channel_id="wh")

    result = notifier.send(_new_message())

    assert result.ok is False
    assert "RuntimeError" in (result.error_reason or "")


# --------------------------------------------------------------------------- #
# 4. Email 适配器（architecture.md 6-M4 第 2/4 段；本机 aiosmtpd）
# --------------------------------------------------------------------------- #
def test_email_send_success_via_local_smtp():
    from aiosmtpd.controller import Controller

    from notify_hub.notifiers.email import EmailNotifier

    recipients = ["oncall@example.com", "ops@example.com"]
    sink = _SmtpSink()
    port = _free_port()
    controller = Controller(sink, hostname="127.0.0.1", port=port)
    controller.start()
    try:
        notifier = EmailNotifier(
            "email",
            host="127.0.0.1",
            port=port,
            sender="notify@example.com",
            recipients=recipients,
        )
        result = notifier.send(_new_message(title="磁盘空间不足", body=FIRST_NOTICE_BODY))
    finally:
        controller.stop()

    assert result.ok is True, result.error_reason
    assert sink.envelopes, "本机 SMTP 未收到任何邮件（端口未绑定？）"
    envelope = sink.envelopes[0]
    assert set(envelope.rcpt_tos) == set(recipients)

    parsed = message_from_bytes(envelope.content)
    assert _header(parsed, "Subject") == "[ERROR] 磁盘空间不足"
    to_header = _header(parsed, "To")
    for recipient in recipients:
        assert recipient in to_header

    body = _body_text(parsed)
    # 规格 6-M4 第 3 段「Email 主题与正文（冻结，已修正重复）」+ 4.6「正文渲染归属」：
    # 正文**就是 msg.body 原样**。适配器若再拼一层表头，下列计数会变成 2。
    # 前提检查：注入的 body 确实自带这些行（否则「恰好一次」会退化成空断言）。
    assert "来源: alert-svc" in FIRST_NOTICE_BODY
    assert "时间: 2024-05-01T12:00:00+00:00" in FIRST_NOTICE_BODY
    assert body.count("来源:") == 1, f"来源 只应出现一次（来自 msg.body），实际 body={body!r}"
    assert body.count("时间:") == 1, f"时间 只应出现一次（来自 msg.body），实际 body={body!r}"
    assert "来源: alert-svc" in body, "msg.body 里的来源行必须原样送达"
    assert "分类: disk" in body, "msg.body 里的分类行必须原样送达（适配器无需也无法补）"
    assert "2024-05-01T12:00:00+00:00" in body, "msg.body 里的 ISO 时间必须原样送达"
    assert "根分区使用率 95%" in body
    assert "\n\n" in body


def test_email_body_is_msg_body_verbatim_without_duplicate_headers():
    """回归闸门：email 正文 MUST 是 ``msg.body`` 原样，不得重复渲染 ``来源``/``时间``/``已超时``。

    真实用户曾在飞书群看到 ``来源``/``分类``/``时间`` 各两次——根因是 M6 的 ``msg.body``
    已含这些行，而渠道适配器又加了一层表头（``email`` 与 ``feishu`` 同型）。
    本用例同时钉住「计数为 1」与「正文逐字符等于 msg.body」，任一形态的重复都会被抓住。
    """
    from aiosmtpd.controller import Controller

    from notify_hub.domain import DeliveryEvent
    from notify_hub.notifiers.email import EmailNotifier

    sink = _SmtpSink()
    port = _free_port()
    controller = Controller(sink, hostname="127.0.0.1", port=port)
    controller.start()
    try:
        notifier = EmailNotifier(
            "email",
            host="127.0.0.1",
            port=port,
            sender="notify@example.com",
            recipients=["oncall@example.com"],
        )
        assert notifier.send(
            _new_message(title="磁盘空间不足", body=FIRST_NOTICE_BODY, category="disk")
        ).ok is True
        assert notifier.send(
            _new_message(
                title="[待办超时 1 小时 5 分钟] 磁盘空间不足",
                body=REMINDER_BODY,
                category="disk",
                kind=DeliveryEvent.REMINDER,
                todo_id=7,
                overdue_seconds=3900.0,
            )
        ).ok is True
    finally:
        controller.stop()

    assert len(sink.envelopes) == 2, "两封邮件都必须真的发出去"
    bodies = [_body_text(message_from_bytes(envelope.content)) for envelope in sink.envelopes]

    first = bodies[0]
    for header in ("来源:", "分类:", "时间:"):
        assert first.count(header) == 1, f"首报邮件里 {header} 只应出现一次，实际 {first.count(header)} 次"
    # 「原样」：去掉邮件编码层引入的行尾差异后必须逐字符相等（多一行表头就会不等）。
    assert first.rstrip("\r\n") == FIRST_NOTICE_BODY, f"email 正文必须逐字符等于 msg.body，实际 {first!r}"
    assert FIRST_NOTICE_BODY in first.replace("\r\n", "\n"), "msg.body 必须以原文形态送达"

    reminder = bodies[1]
    for header in ("来源:", "分类:", "已超时:", "待办 id:", "时间:"):
        expected = 0 if header == "时间:" else 1
        assert reminder.count(header) == expected, (
            f"提醒邮件里 {header} 出现 {reminder.count(header)} 次，应为 {expected} 次"
        )
    assert reminder.rstrip("\r\n") == REMINDER_BODY, f"提醒邮件正文必须逐字符等于 msg.body，实际 {reminder!r}"


def test_email_authentication_failure_is_readable_and_redacted():
    from notify_hub.notifiers.email import EmailNotifier

    class _FakeSMTP:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def starttls(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
            return (220, b"ready")

        def login(self, user: str, password: str):
            raise smtplib.SMTPAuthenticationError(535, f"auth failed for {user}/{password}".encode())

        def sendmail(self, *args: Any, **kwargs: Any):
            raise AssertionError("认证失败后不得继续发信")

        def quit(self) -> None:
            pass

        def close(self) -> None:
            pass

    notifier = EmailNotifier(
        "email",
        host="smtp.invalid",
        username="oncall@example.com",
        password=SMTP_PASSWORD,
        sender="notify@example.com",
        recipients=["me@example.com"],
        smtp_factory=_FakeSMTP,
        secrets=(SMTP_PASSWORD,),
    )

    result = notifier.send(_new_message())

    assert result.ok is False
    assert result.error_reason
    assert SMTP_PASSWORD not in result.error_reason


# --------------------------------------------------------------------------- #
# 第 4 段第 13 条 b：平台回显**裸 token** 的脱敏（已确认 P1 的回归闸门）
# --------------------------------------------------------------------------- #
#: webhook 渠道凭据的**生产形态**：凭据值是内嵌 access_token 的完整 URL。
BARE_TOKEN = "SECRETTOKEN"
WEBHOOK_URL = f"https://h/p?access_token={BARE_TOKEN}"

#: 与 ``config.example.yaml`` 同形状的最小配置（webhook 凭据值 = 整条 URL）。
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

    刻意**不**手写 ``secrets=("SECRETTOKEN",)``：生产里 webhook 凭据值只有整条 URL，
    裸 token 必须由 ``credential_values`` 的 URL 展开提供；测试只有走同一条路径，
    才守得住「裸 token 匹配不上 → 明文入库」这个 P1。
    """
    from notify_hub.config import credential_values, load_settings

    config_path = tmp_path / "config.yaml"
    config_path.write_text(_PRODUCTION_CONFIG_YAML, encoding="utf-8")
    settings = load_settings(config_path, env={"NOTIFY_WEBHOOK_URL": WEBHOOK_URL})
    return settings.channels[0].credentials["url"], credential_values(settings)


def test_webhook_business_error_echoing_bare_token_is_redacted(tmp_path, caplog):
    """13b：HTTP 200 + 业务失败，平台在 ``msg`` 里回显裸 token → 不得出现在 error_reason/日志。"""
    url, secrets = _production_url_and_secrets(tmp_path)
    assert url == WEBHOOK_URL and BARE_TOKEN in url, "前提：凭据值是内嵌裸 token 的完整 URL"

    spy = _WebhookSpy(
        _respond_json(200, {"code": 40001, "msg": f"invalid access_token {BARE_TOKEN}"})
    )
    notifier = _new_webhook(spy, channel_id="secure-wh", url=url, secrets=secrets)

    caplog.set_level(logging.DEBUG)
    result = notifier.send(_new_message())

    assert result.ok is False
    # 先证明平台 msg 确实进入了 error_reason，否则下面的「不含」会假通过
    assert "invalid access_token" in (result.error_reason or "")
    assert BARE_TOKEN not in (result.error_reason or "")
    assert BARE_TOKEN not in caplog.text


#: 路径末段形态的凭据（第三次 P1 的向量）：调用方**没传** secrets 时也必须被脱敏。
PATH_TOKEN = "whk_9f3Aq7ZmXpL2VtRb"  # 24 字符，仅 [A-Za-z0-9_-]
PATH_TOKEN_URL = f"https://hooks.example.com/robot/send/{PATH_TOKEN}"


def test_webhook_derives_secrets_from_own_url_when_caller_passes_none(caplog):
    """回归闸门（第三次 P1 的镜像）：适配器 MUST 自行从 URL 派生密钥。

    形态刻意钉死「调用方纪律缺失」：凭据在 **URL 路径末段**，且构造时
    ``secrets=()``（调用方没传、也拿不到额外密钥）。平台在 ``msg`` 里只回显
    **裸 token**（不含完整 URL），因此只有适配器在 ``__init__`` 中经
    ``extract_url_secrets(url)`` 自行派生，才可能匹配上并脱敏。
    """
    spy = _WebhookSpy(
        _respond_json(200, {"code": 40001, "msg": f"invalid token: {PATH_TOKEN}"})
    )
    notifier = _new_webhook(
        spy, channel_id="derived-wh", url=PATH_TOKEN_URL, secrets=()
    )

    caplog.set_level(logging.DEBUG)
    result = notifier.send(_new_message())

    assert result.ok is False
    # 正控：先证明前置条件真的触发了，否则下面两条「不含」会毫无意义地空过
    assert PATH_TOKEN in str(spy.requests[0].url), "前提：凭据确实在请求 URL 路径末段"
    assert "invalid token" in (result.error_reason or ""), (
        f"前置条件未触发：平台回显未进入 error_reason（{result.error_reason!r}）"
    )
    assert caplog.records, "前置条件未触发：适配器未产生任何日志"
    # 断言本体：调用方没传 secrets，裸 token 仍不得出现在失败原因与日志中
    assert PATH_TOKEN not in (result.error_reason or "")
    assert PATH_TOKEN not in caplog.text
