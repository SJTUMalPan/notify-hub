"""M8 Web 待办界面：服务端渲染的列表、详情与「完成」表单。

见 ``openspec/changes/add-notify-hub/architecture.md`` 第 6 节「模块 M8」。

硬约束：

- 只读经 ``ctx.todos`` / ``ctx.messages`` 取数；**不导入** M7 的 ``api`` / ``pipeline``。
- 所有渲染经 Jinja2 自动转义——消息正文可能含 HTML，禁止任何绕过转义的写法。
- 超时时长**复用** ``notify_hub.services.notifications.format_duration``，不在 web 层另写一份。
- 时间文本冻结为 ``YYYY-MM-DD HH:MM:SS``（UTC、去微秒）。
- 模板目录经 ``__file__`` 定位，不依赖 cwd。
- ``create_web_app(ctx)`` **不启动任何后台线程**。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from notify_hub.clock import as_utc
from notify_hub.context import AppContext
from notify_hub.domain import TodoStatus
from notify_hub.services.notifications import format_duration

__all__ = ["TEMPLATES_DIR", "create_web_router", "create_web_app"]

#: 模板目录：经 ``__file__`` 定位，绝对路径，与 cwd 无关。
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

#: 时间文本格式（冻结）：UTC、去微秒。
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

#: ``TodoService.list`` 不限条数时用的上限（避免默认 100 截断）。
_UNBOUNDED = 1_000_000_000

_TODO_STATUS_LABEL = {
    TodoStatus.PENDING.value: "待完成",
    TodoStatus.DONE.value: "已完成",
}

_EVENT_KIND_LABEL = {
    "created": "创建",
    "reminder": "提醒",
    "completed": "完成",
}

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def format_time(value: datetime | None) -> str:
    """把 tz-aware（或 SQLite 读出的 naive）时刻渲染为 UTC ``YYYY-MM-DD HH:MM:SS``。"""
    if value is None:
        return ""
    return as_utc(value).strftime(TIME_FORMAT)


def status_label(value: Any) -> str:
    """待办状态的展示文案。"""
    if value is None:
        return ""
    raw = value.value if isinstance(value, TodoStatus) else str(value)
    return _TODO_STATUS_LABEL.get(raw, raw)


def event_kind_label(value: Any) -> str:
    """待办事件的展示文案。"""
    raw = value.value if hasattr(value, "value") else str(value)
    return _EVENT_KIND_LABEL.get(raw, raw)


templates.env.filters["format_time"] = format_time
templates.env.filters["status_label"] = status_label
templates.env.filters["event_kind_label"] = event_kind_label
templates.env.globals["format_duration"] = format_duration


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def _status_filter(status: str) -> TodoStatus | None:
    if status == "all":
        return None
    if status == "done":
        return TodoStatus.DONE
    return TodoStatus.PENDING


def create_web_router(ctx: AppContext) -> APIRouter:
    """返回 Web 待办界面的全部页面路由。"""
    router = APIRouter(tags=["web"])

    @router.get("/")
    def root() -> RedirectResponse:
        return RedirectResponse(url="/todos", status_code=303)

    @router.get("/todos")
    def todos_list(
        request: Request,
        status: str = Query(default="pending"),
        ctx: AppContext = Depends(_ctx),
    ):
        views = ctx.todos.list(status=_status_filter(status), limit=_UNBOUNDED)
        return templates.TemplateResponse(
            request,
            "todos_list.html",
            {"todos": views, "status": status},
        )

    @router.post("/todos/{todo_id}/done")
    def todo_done(
        todo_id: int,
        ctx: AppContext = Depends(_ctx),
    ) -> RedirectResponse:
        outcome = ctx.todos.complete(todo_id)
        if outcome.status == "not_found":
            raise HTTPException(status_code=404, detail=f"待办不存在: {todo_id}")
        return RedirectResponse(url="/todos", status_code=303)

    @router.get("/todos/{todo_id}")
    def todo_detail(
        request: Request,
        todo_id: int,
        ctx: AppContext = Depends(_ctx),
    ):
        detail = ctx.todos.detail(todo_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"待办不存在: {todo_id}")
        return templates.TemplateResponse(
            request,
            "todo_detail.html",
            {"detail": detail},
        )

    @router.get("/messages")
    def messages_list(
        request: Request,
        limit: int = Query(default=50, ge=1, le=1000),
        ctx: AppContext = Depends(_ctx),
    ):
        messages = ctx.messages.list(limit=limit)
        return templates.TemplateResponse(
            request,
            "messages_list.html",
            {"messages": messages, "limit": limit},
        )

    @router.get("/messages/{message_id}")
    def message_detail(
        request: Request,
        message_id: int,
        ctx: AppContext = Depends(_ctx),
    ):
        message = ctx.messages.get(message_id)
        if message is None:
            raise HTTPException(status_code=404, detail=f"消息不存在: {message_id}")
        return templates.TemplateResponse(
            request,
            "message_detail.html",
            {
                "message": message,
                "deliveries": ctx.messages.deliveries(message_id),
                "todo": ctx.messages.todo_for(message_id),
            },
        )

    return router


def create_web_app(ctx: AppContext) -> FastAPI:
    """模块级测试用的独立应用；只挂 web 路由，**不启动任何后台线程**。"""
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(create_web_router(ctx))
    return app
