"""通知适配器契约（architecture.md 4.6 节冻结签名）。

本模块只放**共享类型**，不含任何渠道实现：

- :class:`ChannelCapabilities`：渠道能力声明。
- :class:`DeliveryResult`：明确区分成功与失败的结果。
- :class:`NotificationMessage`：与渠道无关的统一消息结构。
- :class:`Notifier`：所有适配器必须满足的 ``runtime_checkable`` Protocol。

硬不变量：``Notifier.send()`` **MUST NOT 抛异常**——任何渠道侧/网络侧错误都必须
转成 :meth:`DeliveryResult.failure`。

本文件由 M4 模块负责，见 ``openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md``
第 4.6 与第 6 节「模块 M4」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol, runtime_checkable

from notify_hub.domain import DeliveryEvent, Level

__all__ = [
    "ChannelCapabilities",
    "DeliveryResult",
    "NotificationMessage",
    "Notifier",
]


@dataclass(frozen=True)
class ChannelCapabilities:
    """渠道能力声明（供上层在构建消息前查询）。"""

    supports_rich_text: bool = False
    max_body_length: int | None = None
    supports_headers: bool = False


@dataclass(frozen=True)
class DeliveryResult:
    """一次渠道投递的结果。"""

    ok: bool
    receipt: str | None = None
    error_reason: str | None = None

    @classmethod
    def success(cls, receipt: str | None = None) -> "DeliveryResult":
        """构造成功结果；``receipt`` 为渠道侧回执标识（若渠道提供）。"""
        return cls(ok=True, receipt=receipt, error_reason=None)

    @classmethod
    def failure(cls, reason: str) -> "DeliveryResult":
        """构造失败结果；``reason`` 必须可读且**已脱敏**。"""
        return cls(ok=False, receipt=None, error_reason=reason)


@dataclass(frozen=True)
class NotificationMessage:
    """与渠道无关的统一消息结构。"""

    title: str
    body: str
    level: Level
    source: str
    occurred_at: datetime  # tz-aware UTC
    kind: DeliveryEvent = DeliveryEvent.FIRST_NOTICE
    todo_id: int | None = None
    overdue_seconds: float | None = None
    category: str | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class Notifier(Protocol):
    """通知适配器契约。

    ``send()`` 的实现 MUST NOT 向上层抛出异常；渠道侧的失败以失败结果返回。
    """

    channel_id: str

    def capabilities(self) -> ChannelCapabilities: ...

    def send(self, msg: NotificationMessage) -> DeliveryResult: ...
