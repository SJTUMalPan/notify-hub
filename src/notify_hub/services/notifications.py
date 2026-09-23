"""通知文案构造与时长格式化。

冻结文案见 ``openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md`` 第 6 节「模块 M6」
与 ``openspec/changes/add-daily-digest/architecture.md`` 第 3.3 节：

- **首次通知**：``overdue_seconds=None``、``todo_id=None``、``title`` 不加前缀；
  正文依次含来源、分类、时间、空行、原始正文。
- **每日汇总**（``add-daily-digest``）：输入是**未完成待办列表**（不是单条消息），
  ``title = "[待办汇总] <N> 项未完成"``，正文逐条列出标题与已超时时长。

单项超时提醒已随 ``add-daily-digest`` 整体移除：``notification_for_message`` 不再有
``kind=REMINDER`` 分支（已无调用方），汇总文案由 :func:`notification_for_digest` 负责。

时间列一律先经 :func:`notify_hub.clock.as_utc` 归一化（架构 1.2 节硬要求）。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Sequence

from notify_hub.clock import as_utc
from notify_hub.domain import DeliveryEvent, Level
from notify_hub.models import Message, Todo
from notify_hub.notifiers.base import NotificationMessage

if TYPE_CHECKING:  # 仅类型标注；避免运行时与 todos 模块形成导入环
    from notify_hub.services.todos import TodoView

__all__ = ["format_duration", "notification_for_message", "notification_for_digest"]

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
    """把一条消息转成与渠道无关的 :class:`NotificationMessage`。

    ``todo`` / ``now`` 保留在冻结签名中；单项超时提醒已移除，故 ``todo`` 不再改变文案。
    """
    level = _level_of(message)
    source = message.source
    category = message.category or ""
    body_text = message.body or ""
    occurred_at = as_utc(message.occurred_at)

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


def notification_for_digest(
    todos: Sequence[TodoView], *, now: datetime
) -> NotificationMessage:
    """把未完成待办渲染成一条每日汇总通知（冻结文案见架构 3.3 节）。

    前置：``todos`` 非空（为空时调用方不应调用本函数）。
    排序：调用方保证已按 ``overdue_seconds`` 降序；本函数**不重排**。
    ``title`` 是独立的一段，正文**不**重复表头（正文渲染归属不变量）。
    """
    lines: list[str] = []
    for index, todo in enumerate(todos, start=1):
        clauses = [
            f"已超时 {format_duration(todo.overdue_seconds)}",
            f"来源 {todo.source}",
        ]
        if todo.category:
            clauses.append(f"分类 {todo.category}")
        lines.append(f"{index}. {todo.title}（{'；'.join(clauses)}）")

    body = "\n".join([*lines, "", "请到待办页面处理。"])
    return NotificationMessage(
        title=f"[待办汇总] {len(todos)} 项未完成",
        body=body,
        level=Level.WARNING,
        source="notify-hub",
        occurred_at=as_utc(now),
        kind=DeliveryEvent.REMINDER,
        todo_id=None,
        overdue_seconds=None,
        category=None,
        meta={},
    )
