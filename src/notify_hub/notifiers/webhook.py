"""内置通用 Webhook 适配器（architecture.md 6-M4 第 2/4 段）。

HTTP POST JSON 载荷到配置地址，用于对接群机器人（飞书/企业微信/钉钉等）。
``transport`` 是唯一的测试接缝（``httpx.MockTransport``）。

判定失败：HTTP 非 2xx → 失败；HTTP 2xx 且响应体是 JSON 且 ``error_path`` 可取到值时，
该值不在 ``success_codes`` 中 → 失败。

本文件由 M4 模块负责。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Mapping, Sequence

import httpx

from notify_hub.config import ChannelSpec
from notify_hub.errors import ConfigurationError
from notify_hub.redact import redact_exception, redact_text

from .base import ChannelCapabilities, DeliveryResult, NotificationMessage

__all__ = ["WebhookNotifier", "build_webhook_notifier"]

_LOGGER = logging.getLogger("notify_hub.notifiers.webhook")

#: httpx 在 INFO 级别记录完整请求 URL——其中可能含 token/basic-auth。凭据 MUST NOT 进日志，
#: 因此这里把 httpx 的请求日志压到 WARNING 以上（错误仍会以脱敏后的失败原因记录）。
logging.getLogger("httpx").setLevel(logging.WARNING)

_DEFAULT_SUCCESS_CODES: tuple[Any, ...] = (0, 200)


def _lookup_path(data: Any, path: str) -> tuple[bool, Any]:
    """按 ``a.b.c`` 路径取值；返回 ``(是否取到, 值)``。"""
    current = data
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return False, None
    return True, current


def _first_non_empty_text(body: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return None


class WebhookNotifier:
    """把 ``NotificationMessage`` 以 JSON POST 发往 webhook 地址。"""

    def __init__(
        self,
        channel_id: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        field_map: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        error_path: str | None = "code",
        success_codes: Sequence[Any] = _DEFAULT_SUCCESS_CODES,
        transport: httpx.BaseTransport | None = None,
        secrets: Sequence[str] = (),
    ) -> None:
        self.channel_id = channel_id
        self._url = url
        self._headers = dict(headers or {})
        self._field_map = dict(field_map or {})
        self._timeout = timeout
        self._error_path = error_path
        self._success_codes = tuple(success_codes)
        self._transport = transport
        self._secrets = tuple(secrets)

    # ------------------------------------------------------------------ #
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(supports_rich_text=False, supports_headers=True)

    # ------------------------------------------------------------------ #
    def _payload(self, msg: NotificationMessage) -> dict[str, Any]:
        occurred_at: datetime = msg.occurred_at
        defaults: dict[str, Any] = {
            "title": msg.title,
            "body": msg.body,
            "level": msg.level.value,
            "source": msg.source,
            "occurred_at": occurred_at.isoformat(),
            "category": msg.category,
            "todo_id": msg.todo_id,
            "overdue_seconds": msg.overdue_seconds,
            "kind": msg.kind.value,
        }
        payload: dict[str, Any] = {}
        for key, value in defaults.items():
            target = self._field_map.get(key, key)
            payload[target] = value
        return payload

    def _redact(self, text: str) -> str:
        return redact_text(text, self._secrets)

    def _business_error(self, body: Any) -> str | None:
        if self._error_path is None or not isinstance(body, Mapping):
            return None
        found, code = _lookup_path(body, self._error_path)
        if not found or code is None:
            return None
        if code in self._success_codes:
            return None
        message = _first_non_empty_text(body, ("msg", "message", "error"))
        if message:
            return message
        return f"平台返回业务错误: {code}"

    def _receipt(self, body: Any, status_code: int) -> str:
        if isinstance(body, Mapping):
            value = _first_non_empty_text(body, ("msg", "message", "id"))
            if value:
                return value
            for key in ("msg", "message", "id"):
                candidate = body.get(key)
                if candidate is not None and str(candidate):
                    return str(candidate)
        return f"HTTP {status_code}"

    # ------------------------------------------------------------------ #
    def send(self, msg: NotificationMessage) -> DeliveryResult:
        try:
            headers = {"Content-Type": "application/json"}
            headers.update(self._headers)
            with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
                response = client.post(
                    self._url, json=self._payload(msg), headers=headers
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
                _LOGGER.warning("webhook 渠道 %s 投递失败: HTTP %s", self.channel_id, status)
                return DeliveryResult.failure(reason)

            business_error = self._business_error(body)
            if business_error is not None:
                reason = self._redact(business_error)
                _LOGGER.warning("webhook 渠道 %s 业务失败: %s", self.channel_id, reason)
                return DeliveryResult.failure(reason)

            _LOGGER.info("webhook 渠道 %s 投递成功 (HTTP %s)", self.channel_id, status)
            return DeliveryResult.success(self._receipt(body, status))
        except Exception as exc:  # noqa: BLE001 - send() MUST NOT 抛异常
            reason = redact_exception(exc, self._secrets)
            _LOGGER.warning("webhook 渠道 %s 投递异常: %s", self.channel_id, reason)
            return DeliveryResult.failure(reason)


def build_webhook_notifier(
    spec: ChannelSpec, secrets: Sequence[str] = ()
) -> WebhookNotifier:
    """按冻结的参数映射表从 :class:`ChannelSpec` 构造 webhook 适配器。

    必填项缺失时抛异常，由注册表按「适配器构造失败」记入 ``unavailable_reasons``。
    """
    params = spec.params or {}
    url = spec.credentials.get("url")
    if not url:
        raise ConfigurationError(f"webhook 渠道 {spec.id} 缺少必填凭据: url")

    success_codes = params.get("success_codes")
    if success_codes is None:
        success_codes = _DEFAULT_SUCCESS_CODES

    timeout = params.get("timeout", 10.0)

    return WebhookNotifier(
        spec.id,
        url,
        headers=params.get("headers") or {},
        field_map=params.get("field_map") or {},
        timeout=float(timeout),
        error_path=params.get("error_path", "code"),
        success_codes=success_codes,
        secrets=tuple(secrets),
    )
