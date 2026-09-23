"""M7 HTTP 接入层的对外入口。

冻结契约：

- ``create_api_router(ctx)`` 返回**包含 ``/healthz`` 在内的全部 API 端点**。
- ``create_api_app(ctx)`` = ``FastAPI()`` + ``app.state.ctx = ctx`` +
  ``include_router(create_api_router(ctx))``；**不启动任何后台线程**
  （classifier/pipeline/scheduler 的线程是 ``create_app()`` 的 lifespan 职责）。
- 路由通过 ``request.app.state.ctx`` 取得 :class:`AppContext`。

本文件由 M7 模块负责，见 ``openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md`` 第 6 节「模块 M7」。
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from notify_hub.context import AppContext

from .health import router as health_router
from .messages import router as messages_router
from .todos import router as todos_router

__all__ = ["create_api_router", "create_api_app"]


def create_api_router(ctx: AppContext) -> APIRouter:
    """返回包含 ``/healthz`` 在内的全部 API 端点。"""
    router = APIRouter()
    router.include_router(health_router)
    router.include_router(messages_router)
    router.include_router(todos_router)
    return router


def create_api_app(ctx: AppContext) -> FastAPI:
    """模块级测试用的独立应用；不启动任何后台线程。"""
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(create_api_router(ctx))
    return app
