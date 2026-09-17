"""通知文案构造与时长格式化。

冻结文案见 ``openspec/changes/add-notify-hub/architecture.md`` 第 6 节「模块 M6」：

- **首次通知**：``overdue_seconds=None``、``todo_id=None``、``title`` 不加前缀；
  正文依次含来源、分类、时间、空行、原始正文。
- **提醒**：``overdue_seconds = now - first_notified_at``；标题形如
  ``[待办超时 <时长>] <标题>``；正文依次含来源、分类、已超时、待办 id、空行、原始正文；
  ``meta`` 含 ``todo_id`` / ``message_id`` / ``reminder_count``。

时间列一律先经 :func:`notify_hub.clock.as_utc` 归一化（架构 1.2 节硬要求）。
"""

from __future__ import annotations

from datetime import datetime

from notify_hub.clock import as_utc
from notify_hub.domain import DeliveryEvent, Level
from notify_hub.models import Message, Todo
from notify_hub.notifiers.base import NotificationMessage

__all__ = ["format_duration", "notification_for_message"]

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400


def format_duration(seconds: float) -> str:
    """人类可读时长，如 ``'2 小时 5 分钟'``、``'45 秒'``、``'1 天 3 小时'``。

    只展开非零单位；全为 0 时返回 ``'0 秒'``。负数按 0 处理。
    """
    total = int(seconds) if seconds > 0 else 0
    days, remainder = divmod(total, _SECONDS_PER_DAY)
    hours, remainder = divmod(remainder, _SECONDS_PER_HOUR)
    minutes, secs = divmod(remainder, _SECONDS_PER_MINUTE)

    parts: list[str] = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if minutes:
        parts.append(f"{minutes} 分钟")
    if secs or not parts:
        parts.append(f"{secs} 秒")
    return " ".join(parts)


def _level_of(message: Message) -> Level:
    try:
        return Level(message.level)
    except ValueError:
        return Level.INFO


def _occurred_iso(message: Message) -> str:
    return as_utc(message.occurred_at).isoformat()


def notification_for_message(
    message: Message,
    *,
    kind: DeliveryEvent,
    now: datetime,
    todo: Todo | None = None,
) -> NotificationMessage:
    """把一条消息（可带待办）转成与渠道无关的 :class:`NotificationMessage`。"""
    level = _level_of(message)
    source = message.source
    category = message.category or ""
    body_text = message.body or ""
    occurred_at = as_utc(message.occurred_at)

    if kind is DeliveryEvent.REMINDER and todo is not None:
        overdue_seconds = (now - as_utc(todo.first_notified_at)).total_seconds()
        duration = format_duration(overdue_seconds)
        title = f"[待办超时 {duration}] {todo.title}"
        body = "\n".join(
            [
                f"来源: {source}",
                f"分类: {category}",
                f"已超时: {duration}",
                f"待办 id: {todo.id}",
                "",
                body_text,
            ]
        )
        return NotificationMessage(
            title=title,
            body=body,
            level=level,
            source=source,
            occurred_at=occurred_at,
            kind=kind,
            todo_id=todo.id,
            overdue_seconds=overdue_seconds,
            category=message.category,
            meta={
                "todo_id": todo.id,
                "message_id": todo.message_id,
                "reminder_count": todo.reminder_count,
            },
        )

    body = "\n".join(
        [
            f"来源: {source}",
            f"分类: {category}",
            f"时间: {_occurred_iso(message)}",
            "",
            body_text,
        ]
    )
    return NotificationMessage(
        title=message.title,
        body=body,
        level=level,
        source=source,
        occurred_at=occurred_at,
        kind=kind,
        todo_id=None,
        overdue_seconds=None,
        category=message.category,
        meta={},
    )
