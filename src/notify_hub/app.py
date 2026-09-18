"""FastAPI 应用工厂与生命周期。

**本文件由架构师维护（阶段 0 共享契约），任何模块都不得修改。**
见 ``openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md`` 第 4.5 节。

生命周期顺序是有意为之的：
- 启动：分类器（先能分类）→ 受理管道（先能收）→ 提醒调度（最后才开始提醒）
- 关闭：提醒调度 → 受理管道 → 分类器 → 数据库

路由组成（架构师维护，模块不得各自增删）：
- M7 的 API 路由（含 `/healthz`）
- M8 的 Web 待办界面路由

**禁止**用 try/except ImportError 之类的静默降级来让应用「跑起来」——路由缺失应当直接
在启动时暴露，而不是变成一台少了一半功能的服务器。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from notify_hub import __version__
from notify_hub.api import create_api_router
from notify_hub.config import load_settings
from notify_hub.context import AppContext, build_context
from notify_hub.web import create_web_router


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    ctx: AppContext = app.state.ctx
    ctx.classifier.start()
    ctx.pipeline.start()
    ctx.scheduler.start()
    try:
        yield
    finally:
        ctx.scheduler.stop()
        ctx.pipeline.stop()
        ctx.classifier.stop()
        ctx.db.dispose()


def create_app(settings=None, *, ctx: AppContext | None = None) -> FastAPI:
    """构造应用。

    ``ctx`` 为 None 时按 ``settings``（再缺省则 ``load_settings()``）装配一个生产上下文。
    传入 ``ctx`` 的场景是集成测试与未来的进程内复用。
    """
    if ctx is None:
        ctx = build_context(settings if settings is not None else load_settings())

    app = FastAPI(title="notify-hub", version=__version__, lifespan=_lifespan)
    app.state.ctx = ctx
    app.include_router(create_api_router(ctx))
    app.include_router(create_web_router(ctx))
    return app
