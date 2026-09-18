"""M2 的 SQLModel 表定义。

四张表：``messages`` / ``todos`` / ``deliveries`` / ``todo_events``。
本模块只做**结构声明**（列、约束、缺省值），不含业务方法、不含查询逻辑，
也不负责时间归一化（调用方读出时间列后必须经 ``clock.as_utc()`` 再参与计算，见架构 1.2 节）。

约定：
- 所有时间列用 ``DateTime(timezone=True)`` 声明（SQLite 仍会丢时区，见 1.2 节）。
- JSON 列显式命名：``meta -> meta_json``、``labels -> labels_json``、``detail -> detail_json``。
- ``Todo`` 上的部分唯一索引由 ``__table_args__`` 声明，SQLite / Postgres 各自带 WHERE 条件。
- 模型缺省值是契约的一部分：``Todo.status='pending'``、``Todo.reminder_count=0``、
  ``Message.level=Level.INFO.value``。

本文件由 M2 模块负责，见 ``openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md``
第 6 节「模块 M2：数据模型与持久化」。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, DateTime, Index, text
from sqlmodel import Field, SQLModel

from .domain import Level, TodoStatus

__all__ = ["Message", "Todo", "DeliveryRecord", "TodoEvent"]

#: 部分唯一索引的 WHERE 条件（SQLite 与 Postgres 共用同一语义）。
_PARTIAL_UNIQUE_WHERE = "dedup_key IS NOT NULL AND status = 'pending'"


class Message(SQLModel, table=True):
    """一条被受理的消息及其分类结果。"""

    __tablename__ = "messages"

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)
    title: str
    body: str | None = Field(default=None)
    level: str = Field(default=Level.INFO.value)
    need_ack_declared: bool = Field(default=False)
    dedup_key: str | None = Field(default=None)
    occurred_at: datetime = Field(sa_type=DateTime(timezone=True))
    received_at: datetime = Field(sa_type=DateTime(timezone=True))
    meta: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("meta_json", JSON, nullable=False, default=dict),
    )
    rule_id: str | None = Field(default=None)
    category: str | None = Field(default=None)
    labels: list[str] = Field(
        default_factory=list,
        sa_column=Column("labels_json", JSON, nullable=False, default=list),
    )
    needs_ack: bool | None = Field(default=None)
    ack_reason: str | None = Field(default=None)
    preferred_channel: str | None = Field(default=None)


class Todo(SQLModel, table=True):
    """由一条消息派生的待办。

    不变量：同一 ``source`` + 非空 ``dedup_key`` 在**待完成**状态下至多一条；
    ``message_id`` 在表内唯一（一条消息至多一条待办）。
    """

    __tablename__ = "todos"
    __table_args__ = (
        Index(
            "uq_todos_source_dedup_key_pending",
            "source",
            "dedup_key",
            unique=True,
            sqlite_where=text(_PARTIAL_UNIQUE_WHERE),
            postgresql_where=text(_PARTIAL_UNIQUE_WHERE),
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    message_id: int = Field(foreign_key="messages.id", unique=True)
    source: str
    dedup_key: str | None = Field(default=None)
    category: str | None = Field(default=None)
    title: str
    status: str = Field(default=TodoStatus.PENDING.value)
    ack_reason: str
    preferred_channel: str | None = Field(default=None)
    created_at: datetime = Field(sa_type=DateTime(timezone=True))
    first_notified_at: datetime = Field(sa_type=DateTime(timezone=True))
    last_notified_at: datetime = Field(sa_type=DateTime(timezone=True))
    reminder_count: int = Field(default=0)
    completed_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


class DeliveryRecord(SQLModel, table=True):
    """一次投递尝试的记录（挂在消息或待办上）。

    ``error_reason`` / ``fallback_reason`` 必须由调用方保证已脱敏。
    """

    __tablename__ = "deliveries"

    id: int | None = Field(default=None, primary_key=True)
    message_id: int | None = Field(default=None, foreign_key="messages.id")
    todo_id: int | None = Field(default=None, foreign_key="todos.id")
    channel_id: str | None = Field(default=None)
    attempted_at: datetime = Field(sa_type=DateTime(timezone=True))
    ok: bool
    error_reason: str | None = Field(default=None)
    receipt: str | None = Field(default=None)
    is_preferred: bool = Field(default=False)
    is_fallback: bool = Field(default=False)
    fallback_reason: str | None = Field(default=None)
    event: str


class TodoEvent(SQLModel, table=True):
    """待办时间序列上的一个事件。"""

    __tablename__ = "todo_events"

    id: int | None = Field(default=None, primary_key=True)
    todo_id: int = Field(foreign_key="todos.id")
    kind: str
    occurred_at: datetime = Field(sa_type=DateTime(timezone=True))
    detail: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column("detail_json", JSON, nullable=True),
    )
    channel_id: str | None = Field(default=None)
    delivery_ok: bool | None = Field(default=None)
