"""消息接入端点：单条/批量投递与详情（architecture.md 6-M7 第 2、3 段）。

- 校验失败一律走 FastAPI 默认 422 形状（``{"detail": [{"loc": [...]}]}``），**不写库**。
- 批量请求体是**裸 JSON 数组**；逐条用 :class:`MessageIn` 校验，单条非法不影响其它条目，
  整体返回 207。
- 响应模型字段名是冻结契约。

本文件由 M7 模块负责。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import ValidationError

from notify_hub.clock import as_utc
from notify_hub.context import AppContext
from notify_hub.domain import Level
from notify_hub.models import DeliveryRecord, Message, Todo
from notify_hub.services.messages import MessageDraft

from .schemas import (
    BatchAccepted,
    BatchItemResult,
    DeliveryOut,
    MessageAccepted,
    MessageIn,
    MessageOut,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/messages", tags=["messages"])


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


# --------------------------------------------------------------------------- #
# 模型 → 响应
# --------------------------------------------------------------------------- #
def _delivery_out(record: DeliveryRecord) -> DeliveryOut:
    return DeliveryOut(
        channel_id=record.channel_id,
        attempted_at=as_utc(record.attempted_at),
        ok=bool(record.ok),
        error_reason=record.error_reason,
        receipt=record.receipt,
        is_preferred=bool(record.is_preferred),
        is_fallback=bool(record.is_fallback),
        fallback_reason=record.fallback_reason,
        event=record.event,
    )


def _message_out(
    message: Message,
    todo: Todo | None,
    deliveries: list[DeliveryRecord],
) -> MessageOut:
    return MessageOut(
        id=message.id,
        source=message.source,
        title=message.title,
        body=message.body,
        level=Level(message.level),
        need_ack=bool(message.need_ack_declared),
        dedup_key=message.dedup_key,
        occurred_at=as_utc(message.occurred_at),
        received_at=as_utc(message.received_at),
        meta=dict(message.meta or {}),
        rule_id=message.rule_id,
        category=message.category,
        labels=list(message.labels or []),
        needs_ack=message.needs_ack,
        ack_reason=message.ack_reason,
        preferred_channel=message.preferred_channel,
        todo_id=todo.id if todo is not None else None,
        deliveries=[_delivery_out(record) for record in deliveries],
    )


def _draft(payload: MessageIn) -> MessageDraft:
    return MessageDraft(
        source=payload.source,
        title=payload.title,
        body=payload.body,
        level=payload.level,
        need_ack=payload.need_ack,
        dedup_key=payload.dedup_key,
        occurred_at=payload.occurred_at,
        meta=payload.meta,
    )


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #
@router.post("", response_model=MessageAccepted, status_code=202)
def post_message(
    payload: MessageIn,
    ctx: AppContext = Depends(_ctx),
) -> MessageAccepted:
    """受理单条消息并立即返回；投递（若不同步）交给后台工作线程。"""
    outcome = ctx.pipeline.accept(_draft(payload))
    return MessageAccepted(message_id=outcome.message_id, todo_id=outcome.todo_id)


def _error_fields(exc: ValidationError) -> list[str]:
    """从 pydantic 的 ``loc`` 中提取字段名（去重保序）。"""
    fields: list[str] = []
    for error in exc.errors():
        loc = error.get("loc") or ()
        if not loc:
            continue
        name = str(loc[-1])
        if name not in fields:
            fields.append(name)
    return fields


def _error_message(exc: ValidationError, fields: list[str]) -> str:
    """人类可读且**点出第一个出错字段名**的错误说明。"""
    first = fields[0] if fields else None
    detail = ""
    errors = exc.errors()
    if errors:
        detail = str(errors[0].get("msg", ""))
    if first is None:
        return f"请求条目格式非法: {detail}".strip()
    return f"字段 {first} 校验失败: {detail}".strip()


@router.post("/batch", response_model=BatchAccepted, status_code=207)
def post_batch(
    items: list[Any] = Body(...),
    ctx: AppContext = Depends(_ctx),
) -> BatchAccepted:
    """逐条独立处理裸 JSON 数组；单条非法不影响其它条目。"""
    results: list[BatchItemResult] = []
    accepted_count = 0
    for index, raw in enumerate(items):
        try:
            payload = MessageIn.model_validate(raw)
        except ValidationError as exc:
            fields = _error_fields(exc)
            results.append(
                BatchItemResult(
                    index=index,
                    accepted=False,
                    error=_error_message(exc, fields),
                    error_fields=fields,
                )
            )
            continue

        outcome = ctx.pipeline.accept(_draft(payload))
        accepted_count += 1
        results.append(
            BatchItemResult(
                index=index,
                accepted=True,
                message_id=outcome.message_id,
                todo_id=outcome.todo_id,
            )
        )

    return BatchAccepted(
        results=results,
        accepted_count=accepted_count,
        rejected_count=len(items) - accepted_count,
    )


@router.get("/{message_id}", response_model=MessageOut)
def get_message(
    message_id: int,
    ctx: AppContext = Depends(_ctx),
) -> MessageOut:
    """消息详情：分类结论与投递记录。"""
    message = ctx.messages.get(message_id)
    if message is None:
        raise HTTPException(status_code=404, detail=f"消息不存在: {message_id}")
    todo = ctx.messages.todo_for(message_id)
    deliveries = ctx.messages.deliveries(message_id)
    return _message_out(message, todo, deliveries)
