"""M7 的 HTTP 请求/响应模型（字段名冻结，M5 CLI / M8 Web / 阶段 D 集成测试依赖）。

见 ``openspec/changes/add-notify-hub/architecture.md`` 第 6 节「模块 M7」第 2 段。
本文件不做业务逻辑，只声明契约。

本文件由 M7 模块负责。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from notify_hub.domain import Level, TodoStatus

__all__ = [
    "MessageIn",
    "MessageAccepted",
    "BatchItemResult",
    "BatchAccepted",
    "DeliveryOut",
    "MessageOut",
    "TodoOut",
    "TodoListOut",
    "TodoDoneOut",
    "HealthOut",
]


# --------------------------------------------------------------------------- #
# 请求
# --------------------------------------------------------------------------- #
class MessageIn(BaseModel):
    """``POST /api/v1/messages`` 的请求体，也是批量接口的逐条模型。"""

    source: str = Field(min_length=1)
    title: str = Field(min_length=1)
    body: str | None = None
    level: Level = Level.INFO
    need_ack: bool = False
    dedup_key: str | None = None
    occurred_at: datetime | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 响应（字段名冻结）
# --------------------------------------------------------------------------- #
class MessageAccepted(BaseModel):
    message_id: int
    todo_id: int | None = None


class BatchItemResult(BaseModel):
    index: int
    accepted: bool
    message_id: int | None = None
    todo_id: int | None = None
    error: str | None = None
    error_fields: list[str] = Field(default_factory=list)


class BatchAccepted(BaseModel):
    results: list[BatchItemResult]
    accepted_count: int
    rejected_count: int


class DeliveryOut(BaseModel):
    channel_id: str | None = None
    attempted_at: datetime
    ok: bool
    error_reason: str | None = None
    receipt: str | None = None
    is_preferred: bool = False
    is_fallback: bool = False
    fallback_reason: str | None = None
    event: str


class MessageOut(BaseModel):
    id: int
    source: str
    title: str
    body: str | None = None
    level: Level
    need_ack: bool
    dedup_key: str | None = None
    occurred_at: datetime
    received_at: datetime
    meta: dict[str, Any] = Field(default_factory=dict)
    rule_id: str | None = None
    category: str | None = None
    labels: list[str] = Field(default_factory=list)
    needs_ack: bool | None = None
    ack_reason: str | None = None
    preferred_channel: str | None = None
    todo_id: int | None = None
    deliveries: list[DeliveryOut] = Field(default_factory=list)


class TodoOut(BaseModel):
    id: int
    source: str
    category: str | None = None
    title: str
    status: TodoStatus
    ack_reason: str
    preferred_channel: str | None = None
    created_at: datetime
    first_notified_at: datetime
    last_notified_at: datetime
    reminder_count: int
    completed_at: datetime | None = None
    overdue_seconds: float


class TodoListOut(BaseModel):
    todos: list[TodoOut]
    total: int


class TodoDoneOut(BaseModel):
    todo_id: int
    status: str
    completed_at: datetime | None = None


class HealthOut(BaseModel):
    status: str
    time: datetime
    version: str
