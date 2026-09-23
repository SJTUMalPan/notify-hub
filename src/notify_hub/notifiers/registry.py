"""通知渠道注册表与 ``spec -> 适配器`` 构造（architecture.md 6-M4 第 2 段）。

``build_from_specs`` 逐个 ``ChannelSpec`` 构造适配器，**永不抛异常**：任何导致渠道不可用的
原因（未启用、未知类型、凭据缺失、工厂异常）都记入 :meth:`NotifierRegistry.unavailable_reasons`，
并跳过该渠道。凭据缺失 = 渠道不可用，不是启动错误。

本文件由 M4 模块负责。
"""

from __future__ import annotations

import logging
from typing import Callable, Mapping, Sequence

from notify_hub.config import ChannelSpec
from notify_hub.redact import redact_exception

from .base import Notifier

__all__ = ["NOTIFIER_FACTORIES", "NotifierRegistry"]

#: key = ``ChannelSpec.type`` -> 工厂 ``(spec, secrets) -> Notifier``。
#: 由 ``webhook.py`` / ``email.py`` 在 ``notifiers/__init__.py`` 中注册。
NOTIFIER_FACTORIES: dict[str, Callable[[ChannelSpec, Sequence[str]], Notifier]] = {}

_LOGGER = logging.getLogger("notify_hub.notifiers.registry")


class NotifierRegistry:
    """已注册渠道的容器：按注册顺序保存，支持按 id 查询与按 spec 批量构造。"""

    def __init__(self) -> None:
        self._notifiers: dict[str, Notifier] = {}
        self._unavailable: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # 注册与查询
    # ------------------------------------------------------------------ #
    def register(self, notifier: Notifier) -> None:
        """按 ``notifier.channel_id`` 注册；重复 id 覆盖并记录一条 warning 日志。"""
        channel_id = notifier.channel_id
        if channel_id in self._notifiers:
            _LOGGER.warning("渠道 %s 重复注册，后注册的适配器生效", channel_id)
        self._notifiers[channel_id] = notifier

    def get(self, channel_id: str) -> Notifier | None:
        return self._notifiers.get(channel_id)

    def ids(self) -> tuple[str, ...]:
        """已注册渠道 id，按注册顺序。"""
        return tuple(self._notifiers)

    def __contains__(self, channel_id: object) -> bool:
        return channel_id in self._notifiers

    def available_ids(self) -> tuple[str, ...]:
        return tuple(self._notifiers)

    def unavailable_reasons(self) -> Mapping[str, str]:
        """只包含**配置里声明过**的渠道的不可用原因。"""
        return dict(self._unavailable)

    # ------------------------------------------------------------------ #
    # 按 spec 构造
    # ------------------------------------------------------------------ #
    def build_from_specs(
        self, specs: Sequence[ChannelSpec], *, secrets: Sequence[str] = ()
    ) -> None:
        """逐个 spec 构造适配器；不可用者记原因并跳过，永不抛异常。"""
        self._unavailable = {}
        for spec in specs:
            if not spec.enabled:
                self._unavailable[spec.id] = "渠道未启用"
                continue

            factory = NOTIFIER_FACTORIES.get(spec.type)
            if factory is None:
                self._unavailable[spec.id] = f"未知的适配器类型: {spec.type}"
                continue

            if not spec.credentials_complete:
                missing = [
                    str(name) for name, value in spec.credentials.items() if not value
                ]
                self._unavailable[spec.id] = f"凭据缺失: {', '.join(missing)}"
                continue

            try:
                notifier = factory(spec, secrets)
            except Exception as exc:  # noqa: BLE001 - 工厂异常不得让启动失败
                reason = redact_exception(exc, tuple(secrets))
                self._unavailable[spec.id] = f"适配器构造失败: {reason}"
                _LOGGER.warning("渠道 %s 适配器构造失败: %s", spec.id, reason)
                continue

            self.register(notifier)
