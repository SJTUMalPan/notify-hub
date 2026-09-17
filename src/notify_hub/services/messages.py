"""消息领域服务：把受理草稿 + 分类结论落库，并提供只读查询。

接口见 ``openspec/changes/add-notify-hub/architecture.md`` 第 6 节「模块 M6」的
``services/messages.py`` 段。时间语义遵守 1.2 节：``received_at`` 取 ``clock.now()``，
``occurred_at`` 缺省等于 ``received_at``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from sqlmodel import select

from notify_hub.clock import Clock, as_utc
from notify_hub.db import Database
from notify_hub.domain import ClassificationVerdict, Level
from notify_hub.models import DeliveryRecord, Message, Todo

__all__ = ["MessageDraft", "MessageService"]


@dataclass(frozen=True)
class MessageDraft:
    """受理侧提交的原始消息（尚未分类、尚未落库）。"""

    source: str
    title: str
    body: str | None = None
    level: Level = Level.INFO
    need_ack: bool = False
    dedup_key: str | None = None
    occurred_at: datetime | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)


class MessageService:
    """消息行的写入与查询。"""

    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def create(self, draft: MessageDraft, verdict: ClassificationVerdict) -> Message:
        """写入一条消息行，分类结论逐字段来自 ``verdict``。返回带 ``id`` 的行。"""
        received_at = self._clock.now()
        occurred_at = as_utc(draft.occurred_at) if draft.occurred_at is not None else received_at
        row = Message(
            source=draft.source,
            title=draft.title,
            body=draft.body,
            level=draft.level.value,
            need_ack_declared=draft.need_ack,
            dedup_key=draft.dedup_key,
            occurred_at=occurred_at,
            received_at=received_at,
            meta=dict(draft.meta),
            rule_id=verdict.rule_id,
            category=verdict.category,
            labels=list(verdict.labels),
            needs_ack=verdict.need_ack,
            ack_reason=verdict.ack_reason.value,
            preferred_channel=verdict.preferred_channel,
        )
        with self._db.session() as session:
            session.add(row)
        return row

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def get(self, message_id: int) -> Message | None:
        with self._db.session() as session:
            return session.get(Message, message_id)

    def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        source: str | None = None,
    ) -> list[Message]:
        """按 ``received_at`` 降序返回消息。"""
        statement = select(Message)
        if source is not None:
            statement = statement.where(Message.source == source)
        statement = statement.order_by(
            Message.received_at.desc(), Message.id.desc()
        ).offset(offset).limit(limit)
        with self._db.session() as session:
            return list(session.exec(statement).all())

    def count(self, *, source: str | None = None) -> int:
        statement = select(Message)
        if source is not None:
            statement = statement.where(Message.source == source)
        with self._db.session() as session:
            return len(list(session.exec(statement).all()))

    def deliveries(self, message_id: int) -> list[DeliveryRecord]:
        """该消息的投递记录，按 ``attempted_at`` 升序。"""
        statement = (
            select(DeliveryRecord)
            .where(DeliveryRecord.message_id == message_id)
            .order_by(DeliveryRecord.attempted_at.asc(), DeliveryRecord.id.asc())
        )
        with self._db.session() as session:
            return list(session.exec(statement).all())

    def todo_for(self, message_id: int) -> Todo | None:
        """返回该消息关联的待办（若有）。"""
        statement = select(Todo).where(Todo.message_id == message_id)
        with self._db.session() as session:
            return session.exec(statement).first()
