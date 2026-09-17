"""渠道选择、降级与投递记录（architecture.md 6-M4 第 2 段）。

候选渠道顺序（冻结）：``[preferred_channel]``（若非空）→ ``[default_channel]``（若非空）
→ ``channel_order`` 中其余渠道；去重保序，只保留 ``registry.available_ids()`` 中的项。

``is_preferred`` / ``is_fallback`` 的唯一判定规则（冻结）::

    is_preferred = preferred_channel is not None and used_channel == preferred_channel
    is_fallback  = preferred_channel is not None and used_channel != preferred_channel

每一次**实际调用适配器**都写一条 :class:`DeliveryRecord`（成功或失败）；首选的失败会先
为它写一条记录，再降级。零候选渠道时写且只写一条 ``channel_id=None`` 的失败记录。

硬不变量：:meth:`DeliveryService.deliver` **MUST NOT 抛异常**；写入记录的
``error_reason`` / ``fallback_reason`` **MUST** 已经过脱敏。本模块不含任何重试逻辑。

本文件由 M4 模块负责。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from notify_hub.clock import Clock
from notify_hub.db import Database
from notify_hub.models import DeliveryRecord
from notify_hub.notifiers.base import DeliveryResult, NotificationMessage
from notify_hub.notifiers.registry import NotifierRegistry
from notify_hub.redact import redact_exception, redact_text

__all__ = ["DeliveryOutcome", "DeliveryService"]

_NO_CHANNEL_REASON = "没有可用的通知渠道"
_UNKNOWN_CHANNEL_REASON = "未在配置中声明的渠道"


@dataclass(frozen=True)
class DeliveryOutcome:
    """一次投递（含降级尝试）的最终结果。"""

    ok: bool
    channel_id: str | None
    is_preferred: bool
    is_fallback: bool
    fallback_reason: str | None
    error_reason: str | None
    receipt: str | None
    attempted_channels: tuple[str, ...]


class DeliveryService:
    """按候选顺序投递，记录每一次实际尝试，并在首选不可用时降级。"""

    def __init__(
        self,
        db: Database,
        registry: NotifierRegistry,
        *,
        default_channel: str | None,
        channel_order: Sequence[str],
        clock: Clock,
        logger: logging.Logger,
        secrets: Sequence[str] = (),
    ) -> None:
        self._db = db
        self._registry = registry
        self._default_channel = default_channel
        self._channel_order = tuple(channel_order)
        self._clock = clock
        self._logger = logger
        self._secrets = tuple(secrets)

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #
    def _redact(self, text: str | None) -> str | None:
        if text is None:
            return None
        return redact_text(text, self._secrets)

    def _candidates(self, preferred_channel: str | None) -> list[str]:
        available = set(self._registry.available_ids())
        ordered: list[str] = []
        seen: set[str] = set()
        for candidate in (preferred_channel, self._default_channel, *self._channel_order):
            if candidate is None or candidate in seen:
                continue
            seen.add(candidate)
            if candidate in available:
                ordered.append(candidate)
        return ordered

    def _attempt(self, channel_id: str, msg: NotificationMessage) -> DeliveryResult:
        """调用适配器；适配器违反契约抛异常时也转成失败结果。"""
        notifier = self._registry.get(channel_id)
        if notifier is None:
            return DeliveryResult.failure(f"渠道未注册: {channel_id}")
        try:
            result = notifier.send(msg)
        except Exception as exc:  # noqa: BLE001 - deliver() MUST NOT 抛异常
            reason = redact_exception(exc, self._secrets)
            self._logger.warning("渠道 %s 适配器异常: %s", channel_id, reason)
            return DeliveryResult.failure(reason)
        if not isinstance(result, DeliveryResult):
            return DeliveryResult.failure(f"渠道 {channel_id} 返回了非法结果")
        return result

    def _write_record(
        self,
        *,
        channel_id: str | None,
        msg: NotificationMessage,
        message_id: int | None,
        todo_id: int | None,
        ok: bool,
        error_reason: str | None,
        receipt: str | None,
        is_preferred: bool,
        is_fallback: bool,
        fallback_reason: str | None,
    ) -> None:
        record = DeliveryRecord(
            message_id=message_id,
            todo_id=todo_id,
            channel_id=channel_id,
            attempted_at=self._clock.now(),
            ok=ok,
            error_reason=error_reason,
            receipt=receipt,
            is_preferred=is_preferred,
            is_fallback=is_fallback,
            fallback_reason=fallback_reason,
            event=msg.kind.value,
        )
        try:
            with self._db.session() as session:
                session.add(record)
        except Exception as exc:  # noqa: BLE001 - 记录失败不得让投递抛异常
            reason = redact_exception(exc, self._secrets)
            self._logger.warning("投递记录写入失败: %s", reason)

    # ------------------------------------------------------------------ #
    # 公开接口
    # ------------------------------------------------------------------ #
    def deliver(
        self,
        msg: NotificationMessage,
        *,
        preferred_channel: str | None = None,
        message_id: int | None = None,
        todo_id: int | None = None,
    ) -> DeliveryOutcome:
        """按候选顺序投递；返回最终结果，且**永不抛异常**。"""
        preferred = preferred_channel
        available = set(self._registry.available_ids())
        candidates = self._candidates(preferred)

        # 首选未注册/未启用/凭据缺失 —— 未尝试，不写投递记录。
        preferred_unavailable: str | None = None
        if preferred is not None and preferred not in available:
            detail = self._registry.unavailable_reasons().get(preferred, _UNKNOWN_CHANNEL_REASON)
            preferred_unavailable = self._redact(
                f"首选渠道 {preferred} 不可用: {detail}"
            )

        attempted: list[str] = []
        last_channel: str | None = None
        last_error: str | None = None
        preferred_error: str | None = None

        for channel_id in candidates:
            is_preferred = preferred is not None and channel_id == preferred
            is_fallback = preferred is not None and channel_id != preferred
            fallback_reason: str | None = None
            if is_fallback:
                if preferred_error is not None:
                    fallback_reason = self._redact(
                        f"首选渠道 {preferred} 投递失败: {preferred_error}"
                    )
                else:
                    fallback_reason = preferred_unavailable

            result = self._attempt(channel_id, msg)
            attempted.append(channel_id)
            last_channel = channel_id
            error_reason = self._redact(result.error_reason)
            if not result.ok and is_preferred:
                preferred_error = error_reason or "投递失败"

            self._write_record(
                channel_id=channel_id,
                msg=msg,
                message_id=message_id,
                todo_id=todo_id,
                ok=result.ok,
                error_reason=None if result.ok else (error_reason or "投递失败"),
                receipt=result.receipt,
                is_preferred=is_preferred,
                is_fallback=is_fallback,
                fallback_reason=fallback_reason,
            )

            if result.ok:
                self._logger.info("渠道 %s 投递成功", channel_id)
                return DeliveryOutcome(
                    ok=True,
                    channel_id=channel_id,
                    is_preferred=is_preferred,
                    is_fallback=is_fallback,
                    fallback_reason=fallback_reason,
                    error_reason=None,
                    receipt=result.receipt,
                    attempted_channels=tuple(attempted),
                )

            last_error = error_reason or "投递失败"
            self._logger.warning("渠道 %s 投递失败: %s", channel_id, last_error)

        if not candidates:
            self._logger.warning(_NO_CHANNEL_REASON)
            self._write_record(
                channel_id=None,
                msg=msg,
                message_id=message_id,
                todo_id=todo_id,
                ok=False,
                error_reason=_NO_CHANNEL_REASON,
                receipt=None,
                is_preferred=False,
                is_fallback=False,
                fallback_reason=None,
            )
            return DeliveryOutcome(
                ok=False,
                channel_id=None,
                is_preferred=False,
                is_fallback=False,
                fallback_reason=None,
                error_reason=_NO_CHANNEL_REASON,
                receipt=None,
                attempted_channels=(),
            )

        # 有候选但全部失败：channel_id = 最后一个尝试过的渠道。
        is_preferred = preferred is not None and last_channel == preferred
        is_fallback = preferred is not None and last_channel != preferred
        outcome_fallback_reason: str | None = None
        if is_fallback:
            if preferred_error is not None:
                outcome_fallback_reason = self._redact(
                    f"首选渠道 {preferred} 投递失败: {preferred_error}"
                )
            else:
                outcome_fallback_reason = preferred_unavailable

        return DeliveryOutcome(
            ok=False,
            channel_id=last_channel,
            is_preferred=is_preferred,
            is_fallback=is_fallback,
            fallback_reason=outcome_fallback_reason,
            error_reason=last_error,
            receipt=None,
            attempted_channels=tuple(attempted),
        )
