"""飞书（Feishu / Lark）自定义机器人适配器（architecture.md 6-M10）。

通用 ``webhook`` 适配器发的是**平铺 JSON**，而飞书要求**嵌套**结构
``{"msg_type": "text", "content": {"text": "..."}}``；``field_map`` 只做顶层键改名，
表达不了嵌套，故按适配器契约新增本专属适配器。协议要点（全部来自飞书官方文档）：

1. 请求体是嵌套结构 ``{"msg_type": "text", "content": {"text": "<纯文本>"}}``。
2. 加签时 ``timestamp`` 与 ``sign`` 放在 **JSON body 顶层**，不是 URL query。
3. ``timestamp`` 单位是**秒**，且在 body 里是**字符串**。
4. 签名：``string_to_sign = f"{timestamp}\\n{secret}"``，``hmac.new`` 的 key 是
   ``string_to_sign``、**消息体为空**（与常规 ``hmac.new(secret, data)`` 正好相反）。
5. 成功判定：HTTP 2xx **且** 响应体 JSON 的 ``code == 0``。

硬不变量：``send()`` MUST NOT 抛异常；``error_reason`` 与日志 MUST NOT 出现 webhook_url
或 secret，异常一律经 :func:`redact_exception` 处理。

本文件由 M10 模块负责。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from datetime import datetime
from typing import Any, Mapping, Sequence

import httpx

from notify_hub.clock import Clock, SystemClock, as_utc
from notify_hub.config import ChannelSpec
from notify_hub.domain import DeliveryEvent
from notify_hub.errors import ConfigurationError
from notify_hub.redact import extract_url_secrets, redact_exception, redact_text

from .base import ChannelCapabilities, DeliveryResult, NotificationMessage

__all__ = ["FeishuNotifier", "build_feishu_notifier", "sign_feishu"]

_LOGGER = logging.getLogger("notify_hub.notifiers.feishu")

#: 时间文本格式（冻结）：UTC、去微秒，与 M8 页面同一格式。
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def sign_feishu(timestamp: int, secret: str) -> str:
    """按飞书官方算法计算签名（纯函数，便于独立测试）。

    ``key = f"{timestamp}\\n{secret}"``、**消息体为空**、HMAC-SHA256、base64。
    """
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _format_time(moment: datetime) -> str:
    return as_utc(moment).strftime(_TIME_FORMAT)


def _render_text(msg: NotificationMessage) -> str:
    """冻结的多行文本（第 3 段第 8 条）。"""
    lines = [
        f"[{msg.level.value.upper()}] {msg.title}",
        f"来源: {msg.source}",
        f"时间: {_format_time(msg.occurred_at)}",
    ]
    if msg.category is not None:
        lines.append(f"分类: {msg.category}")
    if msg.kind is DeliveryEvent.REMINDER and msg.overdue_seconds is not None:
        # 惰性导入以避开 ``notifiers`` <-> ``services`` 的包级循环；
        # 复用 M6 的唯一一份 ``format_duration`` 实现，不在此重复实现。
        from notify_hub.services.notifications import format_duration

        lines.append(f"已超时: {format_duration(msg.overdue_seconds)}")
    if msg.body:
        lines.append("")
        lines.append(msg.body)
    return "\n".join(lines)


class FeishuNotifier:
    """把 ``NotificationMessage`` 以飞书自定义机器人协议 POST 出去。"""

    def __init__(
        self,
        channel_id: str,
        webhook_url: str,
        *,
        secret: str | None = None,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        clock: Clock | None = None,
        secrets: Sequence[str] = (),
    ) -> None:
        self.channel_id = channel_id
        self._webhook_url = webhook_url
        self._secret = secret or None
        self._timeout = timeout
        self._transport = transport
        self._clock = clock or SystemClock()
        # 4.6 节不变量（第二次 P1 后新增）：适配器自身持有 URL 形态的凭据时，
        # MUST 从该 URL 派生密钥并与注入的 secrets 合并；保序去重，使脱敏不变量
        # 由构造保证，不依赖调用方记得传 secrets。
        self._secrets = tuple(
            dict.fromkeys((*secrets, *extract_url_secrets(webhook_url)))
        )

    # ------------------------------------------------------------------ #
    def capabilities(self) -> ChannelCapabilities:
        """首版只做 ``text``；飞书限制请求体 ≤ 20 KB。"""
        return ChannelCapabilities(
            supports_rich_text=False,
            max_body_length=20000,
            supports_headers=False,
        )

    # ------------------------------------------------------------------ #
    def _payload(self, msg: NotificationMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": _render_text(msg)},
        }
        if self._secret is not None:
            timestamp = int(self._clock.now().timestamp())
            payload["timestamp"] = str(timestamp)
            payload["sign"] = sign_feishu(timestamp, self._secret)
        return payload

    def _redact(self, text: str) -> str:
        return redact_text(text, self._secrets)

    def _business_error(self, body: Any) -> str | None:
        if not isinstance(body, Mapping):
            return None
        code = body.get("code")
        if code is None or code == 0:
            return None
        message = body.get("msg")
        if isinstance(message, str) and message:
            return message
        return f"平台返回业务错误: {code}"

    def _receipt(self, body: Any, status_code: int) -> str:
        if isinstance(body, Mapping):
            message = body.get("msg")
            if isinstance(message, str) and message:
                return message
        return f"HTTP {status_code}"

    # ------------------------------------------------------------------ #
    def send(self, msg: NotificationMessage) -> DeliveryResult:
        try:
            headers = {"Content-Type": "application/json"}
            with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
                response = client.post(
                    self._webhook_url, json=self._payload(msg), headers=headers
                )
            status = response.status_code

            body: Any = None
            try:
                body = response.json()
            except Exception:  # noqa: BLE001 - 响应体非 JSON：只看 HTTP 状态
                body = None

            if not 200 <= status < 300:
                snippet = self._redact(response.text.strip())[:200]
                reason = f"HTTP {status}"
                if snippet:
                    reason = f"{reason}: {snippet}"
                _LOGGER.warning("feishu 渠道 %s 投递失败: HTTP %s", self.channel_id, status)
                return DeliveryResult.failure(reason)

            business_error = self._business_error(body)
            if business_error is not None:
                reason = self._redact(business_error)
                _LOGGER.warning("feishu 渠道 %s 业务失败: %s", self.channel_id, reason)
                return DeliveryResult.failure(reason)

            _LOGGER.info("feishu 渠道 %s 投递成功 (HTTP %s)", self.channel_id, status)
            return DeliveryResult.success(self._receipt(body, status))
        except Exception as exc:  # noqa: BLE001 - send() MUST NOT 抛异常
            reason = redact_exception(exc, self._secrets)
            _LOGGER.warning("feishu 渠道 %s 投递异常: %s", self.channel_id, reason)
            return DeliveryResult.failure(reason)


def build_feishu_notifier(
    spec: ChannelSpec, secrets: Sequence[str] = ()
) -> FeishuNotifier:
    """按冻结的参数映射表从 :class:`ChannelSpec` 构造飞书适配器。

    必填项缺失时抛异常，由注册表按「适配器构造失败」记入 ``unavailable_reasons``。
    """
    params = spec.params or {}
    url = spec.credentials.get("url")
    if not url:
        raise ConfigurationError(f"feishu 渠道 {spec.id} 缺少必填凭据: url")

    secret = spec.credentials.get("secret")
    timeout = float(params.get("timeout", 10.0))

    return FeishuNotifier(
        spec.id,
        url,
        secret=secret or None,
        timeout=timeout,
        secrets=tuple(secrets),
    )
