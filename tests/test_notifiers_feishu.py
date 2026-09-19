"""M10 飞书（Feishu / Lark）自定义机器人适配器 · 模块测试。

作用域：**模块测试，实现尚不存在**（``src/notify_hub/notifiers/feishu.py`` 尚未创建）。
测试完全依据 ``openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md`` 第 6 节「模块 M10」
（第 3 段协议要点 + 第 4 段 13 条验证规格）与第 4.3/4.6 节冻结契约编写。

硬约束：

* 所有对 ``notify_hub.notifiers.feishu`` 的导入都写在**测试函数/辅助函数体内**，
  否则 pytest 会在**收集阶段**失败（architecture.md 3.2）。
* 全部经 ``httpx.MockTransport``，**不发起任何真实网络请求**。

第 4 段 13 条 → 用例映射：

1  → test_sign_feishu_matches_official_algorithm / ..._differs_from_conventional_hmac_usage
2  → test_payload_is_nested_feishu_text_shape
3  → test_unsigned_payload_has_no_timestamp_or_sign
4  → test_signed_payload_puts_timestamp_and_sign_in_body_not_query
5  → test_success_returns_receipt_from_msg / test_success_without_msg_falls_back_to_http_status
6  → test_http_200_business_error_is_failure_with_platform_msg
7  → test_sign_verification_failure_is_reported
8  → test_http_non_2xx_is_failure_with_status_code
9  → test_non_json_body_relies_on_http_status_and_receipt_is_non_empty
10 → test_connect_error_returns_failure_instead_of_raising
11 → test_credentials_do_not_leak_on_business_failure /
     test_full_url_in_exception_message_is_redacted
11a → test_platform_echoed_bare_path_token_is_redacted_end_to_end
     （平台只回显**裸路径 token**；secrets 经 ``load_settings`` + ``credential_values``
     生产装配，落库到真实 ``DeliveryRecord`` —— 第二次 P1 的回归闸门）
12 → test_feishu_factory_is_registered_and_buildable_from_spec /
     test_feishu_spec_with_unresolved_url_is_unavailable /
     test_feishu_spec_without_url_is_constructed_failed
13 → test_feishu_notifier_satisfies_notifier_protocol

文案（第 3 段第 8 条）→ test_payload_text_includes_title_level_source_and_time /
test_reminder_text_includes_overdue_duration。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import parse_qs

import httpx

UTC = timezone.utc
MESSAGE_TIME = datetime(2024, 5, 1, 12, 0, 0, tzinfo=UTC)

#: 冻结时刻（与 ``tests/conftest.py`` 的 ``START`` 一致），epoch 秒 = 1704067200。
FROZEN = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
FROZEN_EPOCH_SECONDS = 1704067200

FEISHU_SECRET = "FEISHU-SIGN-SECRET"

#: 与 M6 ``notification_for_message`` **实际产出**同形的正文（``来源``/``分类``/``时间``/空行/原始正文）。
#: 渠道文案规格必须按「拿到的就是完整正文」来写：拿简短的 ``"backup job failed at 02:00"`` 当 body，
#: 适配器自己补一层表头也能通过，而真实用户看到的 ``来源``/``时间`` 各两次就抓不到。
#: 该 ISO 形态由 M6 的 ``as_utc(...).isoformat()`` 产出。
FIRST_NOTICE_BODY = (
    "来源: backup-svc\n"
    "分类: backup-failure\n"
    "时间: 2024-05-01T12:00:00+00:00\n"
    "\n"
    "backup job failed at 02:00"
)

#: 提醒类的正文形如 M6 产出：``来源``/``分类``/``已超时``/``待办 id``/空行/原始正文。
REMINDER_BODY = (
    "来源: backup-svc\n"
    "分类: backup-failure\n"
    "已超时: 1 小时 5 分钟\n"
    "待办 id: 7\n"
    "\n"
    "backup job failed at 02:00"
)

#: 飞书 token 在 **URL 路径末段**（不是 query）——第 4 段第 11 条刻意选这个形态。
TOKEN = "FSECRETTOKEN123456"
WEBHOOK_URL = f"https://open.feishu.cn/open-apis/bot/v2/hook/{TOKEN}"

#: 第 4 段第 1 条：官方文档样例题 (timestamp, secret) 与用文档算法独立复算出的签名。
SIGN_VECTOR_TS = 1599360473
SIGN_VECTOR_SECRET = "demo"
#: 由**飞书官方文档的算法**（``key = f"{timestamp}\n{secret}"``、**消息体为空**、
#: HMAC-SHA256、base64）在本地独立复算所得：timestamp=1599360473、secret=demo。
#: 硬编码它是为了在实现被改坏时仍能发现——现算的期望值会跟着实现一起错。
SIGN_VECTOR_EXPECTED = "l1N0gAcBjdwBvGm1xMjOF0XSyaLRpR7tuO5dHfhAYc8="

#: 通用 webhook 适配器的平铺键——飞书请求体顶层**不得**出现它们（第 4 段第 2 条）。
FLAT_KEYS = (
    "title",
    "body",
    "level",
    "source",
    "occurred_at",
    "category",
    "todo_id",
    "overdue_seconds",
    "kind",
)


# --------------------------------------------------------------------------- #
# 测试辅助（不在模块顶部导入实现）
# --------------------------------------------------------------------------- #
def _new_message(**overrides: Any):
    """按 4.6 冻结契约构造一条 ``NotificationMessage``。"""
    from notify_hub.domain import Level
    from notify_hub.notifiers.base import NotificationMessage

    values: dict[str, Any] = {
        "title": "备份失败",
        "body": "backup job failed at 02:00",
        "level": Level.ERROR,
        "source": "backup-svc",
        "occurred_at": MESSAGE_TIME,
    }
    values.update(overrides)
    return NotificationMessage(**values)


def _official_sign(timestamp: int, secret: str) -> str:
    """用**飞书官方文档的算法**现算期望签名（key 是 ``f"{ts}\\n{secret}"``，消息体为空）。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _conventional_sign(timestamp: int, secret: str) -> str:
    """常规（但**错误**）写法：``hmac.new(secret, data)``——用于反向锁定「写反了」。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class _FeishuSpy:
    """记录 ``FeishuNotifier`` 实际发出的请求（MockTransport，无网络）。"""

    def __init__(self, responder: Callable[[httpx.Request], httpx.Response]) -> None:
        self._responder = responder
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._responder(request)

        return httpx.MockTransport(handler)

    def payload(self) -> dict[str, Any]:
        assert self.requests, "FeishuNotifier 未发出任何请求"
        return json.loads(self.requests[0].content.decode("utf-8"))

    def request(self) -> httpx.Request:
        assert self.requests, "FeishuNotifier 未发出任何请求"
        return self.requests[0]


def _respond_json(status: int, payload: Any):
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return responder


def _respond_text(status: int, text: str):
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=text)

    return responder


def _new_feishu(spy: _FeishuSpy, *, channel_id: str = "fs", url: str = WEBHOOK_URL, **kwargs: Any):
    from notify_hub.notifiers.feishu import FeishuNotifier

    return FeishuNotifier(channel_id, url, transport=spy.transport(), **kwargs)


def _clock_at(moment: datetime = FROZEN):
    from notify_hub.clock import ManualClock

    return ManualClock(moment)


def _content_text(spy: _FeishuSpy) -> str:
    payload = spy.payload()
    content = payload.get("content")
    assert isinstance(content, dict), f"content 必须是嵌套 dict，实际 {content!r}"
    text = content.get("text")
    assert isinstance(text, str) and text, f"content['text'] 必须是非空字符串，实际 {text!r}"
    return text


def _query_of(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.url.query.decode("utf-8"))


# --------------------------------------------------------------------------- #
# 第 4 段第 1 条：纯函数签名正确性
# --------------------------------------------------------------------------- #
def test_sign_feishu_matches_official_algorithm():
    """官方算法现算 + 硬编码已知结果，双重锁定。"""
    from notify_hub.notifiers.feishu import sign_feishu

    recomputed = _official_sign(SIGN_VECTOR_TS, SIGN_VECTOR_SECRET)

    assert _official_sign(SIGN_VECTOR_TS, SIGN_VECTOR_SECRET) == SIGN_VECTOR_EXPECTED, (
        "测试自身写坏的算法不该与硬编码向量一致——先修测试"
    )
    sign = sign_feishu(SIGN_VECTOR_TS, SIGN_VECTOR_SECRET)

    assert isinstance(sign, str)
    assert sign == recomputed, "sign_feishu 必须等于官方算法现算值"
    assert sign == SIGN_VECTOR_EXPECTED, "sign_feishu 必须等于硬编码的已知结果"


def test_sign_feishu_differs_from_conventional_hmac_usage():
    """第 3 段第 4 条的坑：key 是 ``f"{ts}\\n{secret}"``、消息体为空，与常规写法相反。"""
    from notify_hub.notifiers.feishu import sign_feishu

    expected = sign_feishu(SIGN_VECTOR_TS, SIGN_VECTOR_SECRET)

    assert expected != _conventional_sign(SIGN_VECTOR_TS, SIGN_VECTOR_SECRET), (
        "把 hmac.new 的 key/data 写反会得到通用写法的结果——这必须是另一种签名"
    )
    assert sign_feishu(SIGN_VECTOR_TS, "other-secret") != expected
    assert sign_feishu(SIGN_VECTOR_TS + 1, SIGN_VECTOR_SECRET) != expected
    assert len(expected) == 44, "HMAC-SHA256 的 base64 签名长度为 44"


# --------------------------------------------------------------------------- #
# 第 4 段第 2/8 条：请求体形状与文案
# --------------------------------------------------------------------------- #
def test_payload_is_nested_feishu_text_shape():
    spy = _FeishuSpy(_respond_json(200, {"code": 0, "msg": "success", "data": {}}))
    notifier = _new_feishu(spy)

    message = _new_message(body=FIRST_NOTICE_BODY)
    assert notifier.send(message).ok is True

    payload = spy.payload()
    assert payload["msg_type"] == "text"
    assert isinstance(payload["content"], dict), "飞书要求嵌套结构，不是平铺 JSON"
    text = payload["content"]["text"]
    assert isinstance(text, str)
    assert "备份失败" in text, "content.text 必须含标题"
    assert "backup-svc" in text, "content.text 必须含来源"

    # 第 3 段第 8 条（已修正重复）：正文部分**就是 msg.body 原样**，不得再拼 来源/时间/分类/已超时 表头。
    assert text == f"[ERROR] 备份失败\n\n{FIRST_NOTICE_BODY}", (
        f"content.text 必须是 '[LEVEL] title' + 空行 + msg.body 原样，实际 {text!r}"
    )
    assert text.count("来源:") == 1, f"来源 只应出现一次（来自 msg.body），实际 {text.count('来源:')} 次"
    assert text.count("时间:") == 1, f"时间 只应出现一次（来自 msg.body），实际 {text.count('时间:')} 次"
    assert text.count("分类:") == 1, f"分类 只应出现一次（来自 msg.body），实际 {text.count('分类:')} 次"

    for flat_key in FLAT_KEYS:
        assert flat_key not in payload, f"顶层不得出现通用 webhook 的平铺键 {flat_key}"

    request = spy.request()
    assert request.method == "POST"
    assert request.headers["content-type"].startswith("application/json")


def test_payload_text_includes_title_level_source_and_time():
    """第 3 段第 8 条：文本必须是 ``[LEVEL] title`` + 空行 + ``msg.body`` 原样。

    标题、级别、来源、时间都必须出现——但**各恰好一次**（来源/时间来自 ``msg.body``）。
    """
    spy = _FeishuSpy(_respond_json(200, {"code": 0, "msg": "success"}))
    notifier = _new_feishu(spy)

    message = _new_message(body=FIRST_NOTICE_BODY, category="backup-failure")
    assert notifier.send(message).ok is True

    text = _content_text(spy)
    assert "备份失败" in text
    assert "backup-svc" in text
    assert "error" in text.lower(), "content.text 必须含级别（Level 的值）"
    assert "2024-05-01" in text, "content.text 必须含时间"
    assert "12:00" in text, "content.text 必须含时间"
    # 前提检查：注入的 body 确实自带这些行；否则下面的「恰好一次」会退化成空断言。
    assert "来源: backup-svc" in FIRST_NOTICE_BODY
    assert "时间: 2024-05-01T12:00:00+00:00" in FIRST_NOTICE_BODY
    assert "分类: backup-failure" in FIRST_NOTICE_BODY
    assert text.count("来源:") == 1, f"来源 重复渲染了：{text!r}"
    assert text.count("时间:") == 1, f"时间 重复渲染了：{text!r}"
    assert text.count("分类:") == 1, f"分类 重复渲染了：{text!r}"
    # 逐字符等价：任何多拼的表头都会让这条失败。
    assert text == f"[ERROR] 备份失败\n\n{FIRST_NOTICE_BODY}", (
        f"content.text 必须是 '[LEVEL] title' + 空行 + msg.body 原样，实际 {text!r}"
    )


def test_reminder_text_includes_overdue_duration():
    """第 3 段第 8 条：提醒类必须含已超时时长，且复用 M6 的 ``format_duration``。

    标题刻意**不含**时长，否则下面的「时长在文本中」会被标题本身假通过。
    时限**只允许出现一次**（在 ``msg.body`` 里），适配器不得再拼 ``已超时:`` 表头。
    """
    from notify_hub.domain import DeliveryEvent
    from notify_hub.services.notifications import format_duration

    spy = _FeishuSpy(_respond_json(200, {"code": 0, "msg": "success"}))
    notifier = _new_feishu(spy)

    msg = _new_message(
        title="备份失败（提醒）",
        body=REMINDER_BODY,
        kind=DeliveryEvent.REMINDER,
        todo_id=7,
        overdue_seconds=3900.0,
        category="backup-failure",
    )
    assert notifier.send(msg).ok is True

    text = _content_text(spy)
    duration = format_duration(3900.0)
    assert duration == "1 小时 5 分钟", "前提：M6 的 format_duration(3900) 是 '1 小时 5 分钟'"
    assert duration not in msg.title, "前提：标题不含时长，断言才非空过"
    assert duration in text, "提醒文案必须复用 M6 的 format_duration 输出"
    assert "备份失败" in text
    assert text.count("已超时:") == 1, f"已超时 重复渲染了：{text!r}"
    assert text.count("待办 id:") == 1, f"待办 id 重复渲染了：{text!r}"
    assert text.count(duration) == 1, f"时长在文本中应恰好一次，实际 {text.count(duration)} 次"
    assert text == f"[ERROR] 备份失败（提醒）\n\n{REMINDER_BODY}", (
        f"content.text 必须是 '[LEVEL] title' + 空行 + msg.body 原样，实际 {text!r}"
    )


# --------------------------------------------------------------------------- #
# 第 4 段第 3/4 条：加签字段位置（本模块存在的头号坑）
# --------------------------------------------------------------------------- #
def test_unsigned_payload_has_no_timestamp_or_sign():
    spy = _FeishuSpy(_respond_json(200, {"code": 0, "msg": "success"}))
    notifier = _new_feishu(spy)  # 未提供 secret -> 不加签

    assert notifier.send(_new_message()).ok is True

    payload = spy.payload()
    assert "timestamp" not in payload
    assert "sign" not in payload
    assert "timestamp" not in _query_of(spy.request())
    assert "sign" not in _query_of(spy.request())


def test_signed_payload_puts_timestamp_and_sign_in_body_not_query():
    """第 4 段第 4 条：加签字段在 body 顶层；URL query 里**没有**；timestamp 单位是秒。"""
    from notify_hub.notifiers.feishu import sign_feishu

    spy = _FeishuSpy(_respond_json(200, {"code": 0, "msg": "success"}))
    notifier = _new_feishu(spy, secret=FEISHU_SECRET, clock=_clock_at(FROZEN))

    assert notifier.send(_new_message()).ok is True

    payload = spy.payload()
    assert "timestamp" in payload, "加签的 timestamp 必须在 JSON body 顶层（放 query 验签必失败）"
    assert "sign" in payload, "加签的 sign 必须在 JSON body 顶层（放 query 验签必失败）"

    timestamp = payload["timestamp"]
    assert timestamp == str(FROZEN_EPOCH_SECONDS), "timestamp 必须是冻结时刻的**秒**值"
    assert len(str(timestamp)) == 10, "10 位 = 秒；13 位 = 毫秒（规格明确要求秒）"

    recomputed = _official_sign(FROZEN_EPOCH_SECONDS, FEISHU_SECRET)
    assert payload["sign"] == recomputed, "sign 必须按官方算法由冻结 timestamp + secret 算出"
    assert payload["sign"] == sign_feishu(int(timestamp), FEISHU_SECRET)

    request = spy.request()
    assert _query_of(request) == {}, f"加签字段不得出现在 URL query 中，实际 {request.url!r}"
    assert TOKEN in request.url.path, "前提：token 仍在 URL 路径末段（query 未被占用）"


# --------------------------------------------------------------------------- #
# 第 4 段第 5/6/7/8/9 条：成功与失败判定
# --------------------------------------------------------------------------- #
def test_success_returns_receipt_from_msg():
    spy = _FeishuSpy(_respond_json(200, {"code": 0, "msg": "success", "data": {}}))
    notifier = _new_feishu(spy)

    result = notifier.send(_new_message())

    assert result.ok is True, result.error_reason
    assert result.receipt == "success"
    assert result.error_reason is None


def test_success_without_msg_falls_back_to_http_status():
    """第 3 段第 7 条：receipt 取 ``msg``（非空时），否则 ``f"HTTP {status_code}"``。"""
    spy = _FeishuSpy(_respond_json(200, {"code": 0}))
    notifier = _new_feishu(spy)

    result = notifier.send(_new_message())

    assert result.ok is True, result.error_reason
    assert result.receipt == "HTTP 200", "无 msg 时 receipt 回退为 HTTP 状态码，且永不为空"


def test_http_200_business_error_is_failure_with_platform_msg():
    """第 4 段第 6 条：HTTP 200 但 ``code != 0`` 是失败。"""
    spy = _FeishuSpy(_respond_json(200, {"code": 19024, "msg": "Key Words Not Found"}))
    notifier = _new_feishu(spy)

    result = notifier.send(_new_message())

    assert result.ok is False, "HTTP 2xx 且 code != 0 必须判定为失败"
    assert "Key Words Not Found" in (result.error_reason or "")


def test_sign_verification_failure_is_reported():
    """第 4 段第 7 条（异常场景 a）：19021 验签失败。"""
    message = "sign match fail or timestamp is not within one hour from current time"
    spy = _FeishuSpy(_respond_json(200, {"code": 19021, "msg": message}))
    notifier = _new_feishu(spy, secret=FEISHU_SECRET, clock=_clock_at(FROZEN))

    result = notifier.send(_new_message())

    assert result.ok is False
    assert "sign match fail" in (result.error_reason or "")


def test_http_non_2xx_is_failure_with_status_code():
    spy = _FeishuSpy(_respond_json(500, {"code": 500, "msg": "server exploded"}))
    notifier = _new_feishu(spy)

    result = notifier.send(_new_message())

    assert result.ok is False
    assert "500" in (result.error_reason or "")


def test_non_json_body_relies_on_http_status_and_receipt_is_non_empty():
    """第 4 段第 9 条：响应体非 JSON → 只看 HTTP 状态。"""
    spy = _FeishuSpy(_respond_text(200, "<html>gateway</html>"))
    notifier = _new_feishu(spy)

    result = notifier.send(_new_message())

    assert result.ok is True, result.error_reason
    assert result.receipt, "receipt 永不为空"
    assert "200" in result.receipt


# --------------------------------------------------------------------------- #
# 第 4 段第 10 条：网络异常不外抛
# --------------------------------------------------------------------------- #
def test_connect_error_returns_failure_instead_of_raising():
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("连接被拒绝")

    spy = _FeishuSpy(responder)
    notifier = _new_feishu(spy)

    result = notifier.send(_new_message())

    assert result.ok is False
    assert "ConnectError" in (result.error_reason or "")
    assert "连接被拒绝" in (result.error_reason or "")


def test_unexpected_exception_returns_failure_instead_of_raising():
    def responder(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("内部爆炸")

    spy = _FeishuSpy(responder)
    notifier = _new_feishu(spy)

    result = notifier.send(_new_message())

    assert result.ok is False
    assert "RuntimeError" in (result.error_reason or "")


# --------------------------------------------------------------------------- #
# 第 4 段第 11 条：凭据不泄漏（安全关键）
# --------------------------------------------------------------------------- #
#: 与 ``config.example.yaml`` 同形状的最小配置；飞书凭据值 = **完整 URL**（token 在路径末段）。
_FEISHU_CONFIG_YAML = """\
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
  at: "21:00"
  timezone: "Asia/Shanghai"
default_channel: feishu-ops
channels:
  - id: feishu-ops
    type: feishu
    enabled: true
    params: {}
    credentials:
      url: NOTIFY_FEISHU_WEBHOOK_URL
"""


def _production_url_and_secrets(tmp_path):
    """按**生产装配**的形态取 url 与 secrets：``load_settings`` → ``credential_values``。

    刻意不手写 ``secrets=(TOKEN,)`` 或 ``secrets=(URL,)``：生产里飞书凭据值只有整条 URL，
    测试只有走同一条路径，才守得住「本模块不得打印 URL」这个不变量。
    """
    from notify_hub.config import credential_values, load_settings

    config_path = tmp_path / "config.yaml"
    config_path.write_text(_FEISHU_CONFIG_YAML, encoding="utf-8")
    settings = load_settings(config_path, env={"NOTIFY_FEISHU_WEBHOOK_URL": WEBHOOK_URL})
    return settings.channels[0].credentials["url"], credential_values(settings)


def test_credentials_do_not_leak_on_business_failure(tmp_path, caplog):
    """第 4 段第 11 条前半：平台业务失败时，裸 token 与完整 URL 都不得进 error_reason/日志。"""
    url, secrets = _production_url_and_secrets(tmp_path)
    assert url == WEBHOOK_URL
    assert TOKEN in url, "前提：token 在 URL 路径末段（extract_url_secrets 覆盖不到）"
    assert url in secrets, "前提：credential_values 把完整 URL 作为密钥"

    spy = _FeishuSpy(_respond_json(200, {"code": 19024, "msg": "Key Words Not Found"}))
    notifier = _new_feishu(spy, channel_id="feishu-ops", url=url, secrets=secrets)

    caplog.set_level(logging.DEBUG)
    result = notifier.send(_new_message())

    assert result.ok is False
    assert "Key Words Not Found" in (result.error_reason or ""), "先证明失败原因确实被写入，避免空过"
    assert caplog.records, "失败路径必须记录日志，否则下面的 caplog 脱敏断言空过"
    assert TOKEN not in (result.error_reason or ""), "裸 token 泄漏进 error_reason"
    assert url not in (result.error_reason or ""), "完整 webhook URL 泄漏进 error_reason"
    assert TOKEN not in caplog.text, "裸 token 泄漏进日志"
    assert url not in caplog.text, "完整 webhook URL 泄漏进日志"


def test_platform_echoed_full_url_is_redacted(tmp_path, caplog):
    """第 4 段第 11 条：平台在 msg 里回显完整 URL → 必须被 ``redact_text`` 掩码。"""
    url, secrets = _production_url_and_secrets(tmp_path)

    spy = _FeishuSpy(_respond_json(200, {"code": 9499, "msg": f"Bad Request: {url}"}))
    notifier = _new_feishu(spy, channel_id="feishu-ops", url=url, secrets=secrets)

    caplog.set_level(logging.DEBUG)
    result = notifier.send(_new_message())

    assert result.ok is False
    assert "Bad Request" in (result.error_reason or ""), "先证明平台 msg 确实进入了 error_reason"
    assert url not in (result.error_reason or "")
    assert TOKEN not in (result.error_reason or "")
    assert url not in caplog.text
    assert TOKEN not in caplog.text


#: 第 4 段第 11a 条专用：**自造**的路径 token（≥16 字符），刻意不复用 ``TOKEN``——
#: 复用旧常量会让「旧测试碰巧通过」与「新形态确实被覆盖」分不清。
PATH_ECHO_TOKEN = "PATHONLYTOKEN9f4c2ab77d1e"


def _production_url_and_secrets_for(tmp_path, url: str):
    """同 :func:`_production_url_and_secrets`，但可指定 URL（仍走生产装配路径）。"""
    from notify_hub.config import credential_values, load_settings

    config_path = tmp_path / "config.yaml"
    config_path.write_text(_FEISHU_CONFIG_YAML, encoding="utf-8")
    settings = load_settings(config_path, env={"NOTIFY_FEISHU_WEBHOOK_URL": url})
    return settings.channels[0].credentials["url"], credential_values(settings)


def test_platform_echoed_bare_path_token_is_redacted_end_to_end(tmp_path, caplog):
    """第 4 段第 11a 条：平台只回显**裸路径 token**（不含完整 URL）——第二次 P1 的回归闸门。

    §3 第 11 条原写的「裸 token 只会在有代码主动打印它时泄漏」已被证伪：平台在业务错误
    ``msg`` 里回显裸 token 就足以让明文进入 ``error_reason`` → ``DeliveryRecord`` → API/日志。
    因此本用例刻意让 ``msg`` **只含裸 token**（不含完整 URL），走**生产装配**的 secrets
    （``load_settings`` → ``credential_values``，即 11a 强制的形态），并落到**真实 Database**
    的 ``DeliveryRecord`` 上：手工 ``secrets=(TOKEN,)`` 或只看内存结果都抓不到这次泄漏。
    """
    import notify_hub.notifiers  # noqa: F401 - 导入即完成内置工厂注册
    from notify_hub.clock import ManualClock
    from notify_hub.config import ChannelSpec
    from notify_hub.db import Database
    from notify_hub.delivery import DeliveryService
    from notify_hub.models import DeliveryRecord, Message
    from notify_hub.notifiers import NotifierRegistry
    from notify_hub.notifiers.feishu import FeishuNotifier
    from sqlmodel import select

    url, secrets = _production_url_and_secrets_for(
        tmp_path, f"https://open.feishu.cn/open-apis/bot/v2/hook/{PATH_ECHO_TOKEN}"
    )
    # 前置前提：token 确实是 URL 的**路径末段**（这正是绕过 query 提取的形态）。
    # 刻意**不**断言裸 token 不在 secrets 里：修复后它本就应该在，那不是测试错误。
    assert url.endswith(PATH_ECHO_TOKEN), "前提：token 必须在 URL 路径末段"
    assert url in secrets, "前提：credential_values 必须把完整 URL 作为密钥"

    spy = _FeishuSpy(
        _respond_json(
            200,
            {"code": 9499, "msg": f"invalid webhook token: {PATH_ECHO_TOKEN}"},
        )
    )

    # 挂真实 Database 的父行（M2 开了 PRAGMA foreign_keys，deliveries.message_id 需要父行）。
    database = Database(tmp_path / "m10-path-token.sqlite3")
    database.init_schema()
    with database.session() as session:
        parent = Message(
            source="backup-svc",
            title="备份失败",
            occurred_at=MESSAGE_TIME,
            received_at=MESSAGE_TIME,
        )
        session.add(parent)
    with database.session() as session:
        message_id = session.exec(select(Message.id)).one()
    assert isinstance(message_id, int)

    class _SpyFeishuNotifier(FeishuNotifier):
        """只注入 MockTransport/时钟，脱敏仍走适配器自己的生产代码路径。"""

        def __init__(self, channel_id: str, webhook_url: str, **kwargs: Any) -> None:
            super().__init__(channel_id, webhook_url, transport=spy.transport(), **kwargs)

    registry = NotifierRegistry()
    registry.build_from_specs(
        [ChannelSpec(id="feishu-ops", type="feishu", params={}, credentials={"url": url})]
    )
    assert registry.available_ids() == ("feishu-ops",), registry.unavailable_reasons()
    assert isinstance(registry.get("feishu-ops"), FeishuNotifier), "前提：工厂走真实飞书适配器"
    registry.register(_SpyFeishuNotifier("feishu-ops", url))

    service = DeliveryService(
        database,
        registry,
        default_channel="feishu-ops",
        channel_order=["feishu-ops"],
        clock=ManualClock(MESSAGE_TIME),
        logger=logging.getLogger("notify_hub.test.feishu.path_token"),
        secrets=secrets,
    )

    caplog.set_level(logging.DEBUG)
    outcome = service.deliver(_new_message(), message_id=message_id)

    # ---- 正控：先证明前置条件真的触发了，否则「不含 token」纯属空过 ----
    assert outcome.ok is False
    reason = outcome.error_reason or ""
    assert "invalid webhook token" in reason, "正控：平台 msg 必须真的进入 error_reason"
    assert caplog.records, "正控：失败路径必须记录日志，否则 caplog 脱敏断言空过"

    # ---- 落库的 DeliveryRecord（真实表、真实外键、真实 redact 路径）----
    with database.session() as session:
        records = list(session.exec(select(DeliveryRecord)))
    assert len(records) == 1, f"应恰好写一条投递记录，实际 {len(records)}"
    assert records[0].error_reason, "正控：记录里的 error_reason 不得为空，否则下面无意义"
    assert "invalid webhook token" in records[0].error_reason, (
        "正控：平台 msg 必须真的落到 DeliveryRecord.error_reason"
    )

    # ---- 断言：裸 token 三处都不得出现 ----
    assert PATH_ECHO_TOKEN not in reason, "裸路径 token 泄漏进 error_reason（P1 回归）"
    assert PATH_ECHO_TOKEN not in caplog.text, "裸路径 token 泄漏进日志（P1 回归）"
    assert PATH_ECHO_TOKEN not in records[0].error_reason, (
        "裸路径 token 明文落进 DeliveryRecord.error_reason（P1 回归，会经 API 外泄）"
    )
    database.dispose()


def test_full_url_in_exception_message_is_redacted(tmp_path, caplog):
    """第 4 段第 11 条后半：异常消息含完整 URL → error_reason 与 caplog 均不得含它。"""
    url, secrets = _production_url_and_secrets(tmp_path)

    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"无法连接到 {url}")

    spy = _FeishuSpy(responder)
    notifier = _new_feishu(spy, channel_id="feishu-ops", url=url, secrets=secrets)

    caplog.set_level(logging.DEBUG)
    result = notifier.send(_new_message())

    assert result.ok is False
    assert "ConnectError" in (result.error_reason or ""), "先证明异常原因被写入，避免空过"
    assert caplog.records, "异常路径必须记录日志，否则下面的 caplog 脱敏断言空过"
    assert url not in (result.error_reason or ""), "完整 webhook URL 泄漏进 error_reason"
    assert TOKEN not in (result.error_reason or ""), "URL 中的裸 token 泄漏进 error_reason"
    assert url not in caplog.text, "完整 webhook URL 泄漏进日志"
    assert TOKEN not in caplog.text, "URL 中的裸 token 泄漏进日志"


# --------------------------------------------------------------------------- #
# 第 4 段第 12 条：可被注册表按配置构造
# --------------------------------------------------------------------------- #
def test_feishu_factory_is_registered_and_buildable_from_spec():
    import notify_hub.notifiers  # noqa: F401 - 导入即完成内置工厂注册
    from notify_hub.config import ChannelSpec
    from notify_hub.notifiers import NotifierRegistry
    from notify_hub.notifiers.feishu import FeishuNotifier
    from notify_hub.notifiers.registry import NOTIFIER_FACTORIES

    assert "feishu" in NOTIFIER_FACTORIES, "notifiers/__init__.py 必须注册 feishu 工厂"
    assert "webhook" in NOTIFIER_FACTORIES and "email" in NOTIFIER_FACTORIES, "既有注册项不得丢失"

    registry = NotifierRegistry()
    registry.build_from_specs(
        [
            ChannelSpec(
                id="fs",
                type="feishu",
                params={},
                credentials={"url": WEBHOOK_URL},
            ),
            ChannelSpec(
                id="fs-signed",
                type="feishu",
                params={},
                credentials={"url": WEBHOOK_URL, "secret": FEISHU_SECRET},
            ),
        ]
    )

    assert "fs" in registry.ids()
    assert registry.available_ids() == ("fs", "fs-signed")
    assert registry.unavailable_reasons() == {}
    notifier = registry.get("fs")
    assert notifier is not None
    assert notifier.channel_id == "fs"
    assert isinstance(notifier, FeishuNotifier)


def test_feishu_spec_with_unresolved_url_is_unavailable():
    """第 4 段第 12 条：url 未解析 → 进 ``unavailable_reasons``，原因为「凭据缺失」。"""
    import notify_hub.notifiers  # noqa: F401
    from notify_hub.config import ChannelSpec
    from notify_hub.notifiers import NotifierRegistry

    registry = NotifierRegistry()
    registry.build_from_specs(
        [ChannelSpec(id="fs", type="feishu", params={}, credentials={"url": None})]
    )

    assert registry.available_ids() == ()
    assert "fs" not in registry.ids()
    reason = registry.unavailable_reasons()["fs"]
    assert "凭据缺失" in reason
    assert "url" in reason, "原因里应是逻辑凭据名，不是环境变量名"


def test_feishu_spec_without_url_is_constructed_failed():
    """config 映射表：``credentials["url"]`` **必填** → 工厂抛异常 → 记入不可用原因。"""
    import notify_hub.notifiers  # noqa: F401
    from notify_hub.config import ChannelSpec
    from notify_hub.notifiers import NotifierRegistry

    registry = NotifierRegistry()
    registry.build_from_specs([ChannelSpec(id="fs", type="feishu", params={}, credentials={})])

    assert registry.available_ids() == ()
    reason = registry.unavailable_reasons()["fs"]
    assert reason, "缺少必填 url 的渠道必须记录不可用原因"
    assert "适配器构造失败" in reason


# --------------------------------------------------------------------------- #
# 第 4 段第 13 条：协议自检
# --------------------------------------------------------------------------- #
def test_feishu_notifier_satisfies_notifier_protocol():
    from notify_hub.notifiers.base import ChannelCapabilities, Notifier
    from notify_hub.notifiers.feishu import FeishuNotifier

    notifier = FeishuNotifier("fs", WEBHOOK_URL)

    assert isinstance(notifier, Notifier)
    assert notifier.channel_id == "fs"
    assert isinstance(notifier.capabilities(), ChannelCapabilities)


def test_send_never_raises_for_broken_transport():
    """4.6 硬不变量：``send()`` MUST NOT 抛异常——异常一律转成失败结果。"""
    def responder(request: httpx.Request) -> httpx.Response:
        raise ValueError("坏了")

    spy = _FeishuSpy(responder)
    notifier = _new_feishu(spy, secret=FEISHU_SECRET, clock=_clock_at(FROZEN))

    result = notifier.send(_new_message())

    assert result.ok is False
    assert "ValueError" in (result.error_reason or "")
