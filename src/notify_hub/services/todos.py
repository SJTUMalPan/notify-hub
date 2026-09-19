"""待办领域服务：创建/去重、完成、列表、详情与逐条提醒记账。

接口见 ``openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md`` 第 6 节「模块 M6」的
``services/todos.py`` 段。硬要求：

- 所有从 ORM 读出的时间在参与算术前必须经 ``clock.as_utc()`` 归一化（架构 1.2 节）。
- ``TodoView.overdue_seconds`` 用 ``max(0.0, ...)`` 兜底，避免时钟回拨产生负数。
- ``TodoService`` **不得**依赖 ``DeliveryService``（避免环）。

``list(status=None)`` 表示不过滤状态；缺省参数才是 ``TodoStatus.PENDING``。

``add-daily-digest`` 移除了单项超时提醒的判定入口（连同两个间隔配置键），
不再有「距上次通知 ≥ 间隔」的门槛；``record_reminder`` 仅负责逐条记账，保留不变。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from notify_hub.clock import Clock, as_utc
from notify_hub.db import Database
from notify_hub.domain import AckReason, TodoEventKind, TodoStatus
from notify_hub.models import DeliveryRecord, Message, Todo, TodoEvent

__all__ = ["TodoView", "TodoDetail", "CompleteOutcome", "TodoService"]


@dataclass(frozen=True)
class TodoView:
    """待办的对外视图（含计算出的 ``overdue_seconds``）。"""

    id: int
    source: str
    category: str | None
    title: str
    status: TodoStatus
    ack_reason: AckReason
    preferred_channel: str | None
    created_at: datetime
    first_notified_at: datetime
    last_notified_at: datetime
    reminder_count: int
    completed_at: datetime | None
    overdue_seconds: float


@dataclass(frozen=True)
class TodoDetail:
    """待办详情：视图 + 原始消息 + 投递记录 + 事件时间序列。"""

    todo: TodoView
    message: Message
    deliveries: tuple[DeliveryRecord, ...]
    events: tuple[TodoEvent, ...]


@dataclass(frozen=True)
class CompleteOutcome:
    """``complete()`` 的结果：``completed`` / ``already_completed`` / ``not_found``。"""

    status: str
    todo_id: int
    completed_at: datetime | None


def _overdue_seconds(todo: Todo, now: datetime) -> float:
    first = as_utc(todo.first_notified_at)
    if todo.status == TodoStatus.DONE.value and todo.completed_at is not None:
        return max(0.0, (as_utc(todo.completed_at) - first).total_seconds())
    return max(0.0, (now - first).total_seconds())


def _view(todo: Todo, now: datetime) -> TodoView:
    return TodoView(
        id=todo.id,
        source=todo.source,
        category=todo.category,
        title=todo.title,
        status=TodoStatus(todo.status),
        ack_reason=AckReason(todo.ack_reason),
        preferred_channel=todo.preferred_channel,
        created_at=as_utc(todo.created_at),
        first_notified_at=as_utc(todo.first_notified_at),
        last_notified_at=as_utc(todo.last_notified_at),
        reminder_count=todo.reminder_count,
        completed_at=as_utc(todo.completed_at) if todo.completed_at is not None else None,
        overdue_seconds=_overdue_seconds(todo, now),
    )


class TodoService:
    """待办的领域逻辑。"""

    def __init__(
        self,
        db: Database,
        clock: Clock,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._logger = logger or logging.getLogger("notify_hub.services.todos")

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #
    def _pending_by_dedup(self, source: str, dedup_key: str) -> Todo | None:
        statement = select(Todo).where(
            Todo.source == source,
            Todo.dedup_key == dedup_key,
            Todo.status == TodoStatus.PENDING.value,
        )
        with self._db.session() as session:
            return session.exec(statement).first()

    def _by_message(self, message_id: int) -> Todo | None:
        statement = select(Todo).where(Todo.message_id == message_id)
        with self._db.session() as session:
            return session.exec(statement).first()

    # ------------------------------------------------------------------ #
    # 创建与完成
    # ------------------------------------------------------------------ #
    def ensure_for_message(self, message: Message) -> Todo | None:
        """``needs_ack`` 为假返回 ``None``；否则按 ``(source, dedup_key)`` 去重后创建待办。"""
        if not message.needs_ack:
            return None

        if message.dedup_key:
            existing = self._pending_by_dedup(message.source, message.dedup_key)
            if existing is not None:
                return existing

        now = self._clock.now()
        try:
            with self._db.session() as session:
                todo = Todo(
                    message_id=message.id,
                    source=message.source,
                    dedup_key=message.dedup_key,
                    category=message.category,
                    title=message.title,
                    status=TodoStatus.PENDING.value,
                    ack_reason=message.ack_reason,
                    preferred_channel=message.preferred_channel,
                    created_at=now,
                    first_notified_at=now,
                    last_notified_at=now,
                    reminder_count=0,
                    completed_at=None,
                )
                session.add(todo)
                session.flush()
                session.add(
                    TodoEvent(
                        todo_id=todo.id,
                        kind=TodoEventKind.CREATED.value,
                        occurred_at=now,
                        detail=None,
                        channel_id=None,
                        delivery_ok=None,
                    )
                )
            return todo
        except IntegrityError:
            # 并发下唯一约束生效 = 已存在：回查后返回，不重复写事件。
            self._logger.info("待办已存在（并发唯一约束），回查返回: message_id=%s", message.id)
            existing = None
            if message.dedup_key:
                existing = self._pending_by_dedup(message.source, message.dedup_key)
            if existing is None:
                existing = self._by_message(message.id)
            return existing

    def get(self, todo_id: int) -> Todo | None:
        with self._db.session() as session:
            return session.get(Todo, todo_id)

    def complete(self, todo_id: int) -> CompleteOutcome:
        """标记完成；幂等（已完成返回 ``already_completed`` 且时间不变）。"""
        now = self._clock.now()
        with self._db.session() as session:
            todo = session.get(Todo, todo_id)
            if todo is None:
                return CompleteOutcome(status="not_found", todo_id=todo_id, completed_at=None)
            if todo.status == TodoStatus.DONE.value or todo.completed_at is not None:
                completed_at = (
                    as_utc(todo.completed_at) if todo.completed_at is not None else None
                )
                return CompleteOutcome(
                    status="already_completed",
                    todo_id=todo_id,
                    completed_at=completed_at,
                )
            todo.status = TodoStatus.DONE.value
            todo.completed_at = now
            session.add(todo)
            session.add(
                TodoEvent(
                    todo_id=todo.id,
                    kind=TodoEventKind.COMPLETED.value,
                    occurred_at=now,
                    detail=None,
                    channel_id=None,
                    delivery_ok=None,
                )
            )
        return CompleteOutcome(status="completed", todo_id=todo_id, completed_at=now)

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def list(
        self,
        *,
        status: TodoStatus | None = TodoStatus.PENDING,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TodoView]:
        """默认仅待完成；``status=None`` 表示不过滤。按 ``overdue_seconds`` 降序。"""
        statement = select(Todo)
        if status is not None:
            statement = statement.where(Todo.status == status.value)
        now = self._clock.now()
        with self._db.session() as session:
            rows = list(session.exec(statement).all())
        views = [_view(row, now) for row in rows]
        views.sort(key=lambda view: view.overdue_seconds, reverse=True)
        return views[offset : offset + limit]

    def detail(self, todo_id: int) -> TodoDetail | None:
        now = self._clock.now()
        with self._db.session() as session:
            todo = session.get(Todo, todo_id)
            if todo is None:
                return None
            message = session.get(Message, todo.message_id)
            if message is None:
                return None
            deliveries = list(
                session.exec(
                    select(DeliveryRecord)
                    .where(DeliveryRecord.todo_id == todo_id)
                    .order_by(DeliveryRecord.attempted_at.asc(), DeliveryRecord.id.asc())
                ).all()
            )
            events = list(
                session.exec(
                    select(TodoEvent)
                    .where(TodoEvent.todo_id == todo_id)
                    .order_by(TodoEvent.occurred_at.asc(), TodoEvent.id.asc())
                ).all()
            )
            return TodoDetail(
                todo=_view(todo, now),
                message=message,
                deliveries=tuple(deliveries),
                events=tuple(events),
            )

    def message_for(self, message_id: int) -> Message | None:
        """按主键读取消息（供提醒调度构造文案；避免 ``TodoService`` 依赖 ``MessageService``）。"""
        with self._db.session() as session:
            return session.get(Message, message_id)

    # ------------------------------------------------------------------ #
    # 提醒
    # ------------------------------------------------------------------ #
    def record_reminder(
        self,
        todo_id: int,
        *,
        delivered: bool,
        channel_id: str | None,
    ) -> None:
        """记账：``last_notified_at`` 无条件更新，``reminder_count += 1``，写 REMINDER 事件。"""
        now = self._clock.now()
        with self._db.session() as session:
            todo = session.get(Todo, todo_id)
            if todo is None:
                self._logger.warning("记录提醒时待办不存在: id=%s", todo_id)
                return
            todo.last_notified_at = now
            todo.reminder_count = todo.reminder_count + 1
            session.add(todo)
            session.add(
                TodoEvent(
                    todo_id=todo.id,
                    kind=TodoEventKind.REMINDER.value,
                    occurred_at=now,
                    detail=None,
                    channel_id=channel_id,
                    delivery_ok=delivered,
                )
            )
