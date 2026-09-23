"""待办查询与完成端点（architecture.md 6-M7 第 2、3 段）。

- ``GET /api/v1/todos``：``?status=pending|done|all``，默认 ``pending``；按
  ``overdue_seconds`` 降序。``total`` 是**匹配过滤条件的总数**，不受 ``limit``/``offset``
  影响（即分页前的命中数）。
- ``POST /api/v1/todos/{id}/done``：幂等，重复完成返回 ``already_completed``；不存在 404。

本文件由 M7 模块负责。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from notify_hub.context import AppContext
from notify_hub.domain import TodoStatus
from notify_hub.services.todos import CompleteOutcome, TodoView

from .schemas import TodoDoneOut, TodoListOut, TodoOut

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/todos", tags=["todos"])

#: ``TodoService.list`` 不限条数时用的上限（避免默认 100 截断）。
_UNBOUNDED = 1_000_000_000


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def _todo_out(view: TodoView) -> TodoOut:
    return TodoOut(
        id=view.id,
        source=view.source,
        category=view.category,
        title=view.title,
        status=view.status,
        ack_reason=view.ack_reason.value,
        preferred_channel=view.preferred_channel,
        created_at=view.created_at,
        first_notified_at=view.first_notified_at,
        last_notified_at=view.last_notified_at,
        reminder_count=view.reminder_count,
        completed_at=view.completed_at,
        overdue_seconds=view.overdue_seconds,
    )


@router.get("", response_model=TodoListOut)
def list_todos(
    status: Literal["pending", "done", "all"] = "pending",
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    ctx: AppContext = Depends(_ctx),
) -> TodoListOut:
    """按状态过滤并排序（``overdue_seconds`` 降序，即最久优先）。"""
    if status == "all":
        filter_status: TodoStatus | None = None
    elif status == "done":
        filter_status = TodoStatus.DONE
    else:
        filter_status = TodoStatus.PENDING

    every = ctx.todos.list(status=filter_status, limit=_UNBOUNDED, offset=0)
    total = len(every)
    page = every[offset : offset + limit]
    return TodoListOut(todos=[_todo_out(view) for view in page], total=total)


@router.post("/{todo_id}/done", response_model=TodoDoneOut)
def complete_todo(
    todo_id: int,
    ctx: AppContext = Depends(_ctx),
) -> TodoDoneOut:
    """标记待办完成；幂等。"""
    outcome: CompleteOutcome = ctx.todos.complete(todo_id)
    if outcome.status == "not_found":
        raise HTTPException(status_code=404, detail=f"待办不存在: {todo_id}")
    return TodoDoneOut(
        todo_id=outcome.todo_id,
        status=outcome.status,
        completed_at=outcome.completed_at,
    )
