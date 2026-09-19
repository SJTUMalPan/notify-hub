"""FastAPI 应用工厂与生命周期。

**本文件由架构师维护（阶段 0 共享契约），任何模块都不得修改。**
见 ``openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md`` 第 4.5 节。

生命周期顺序是有意为之的：
- 启动：分类器（先能分类）→ 受理管道（先能收）→ 提醒调度（最后才开始提醒）
- 关闭：提醒调度 → 受理管道 → 分类器 → 数据库

路由组成（架构师维护，模块不得各自增删）：
- M7 的 API 路由（含 `/healthz`）
- M8 的 Web 待办界面路由

鉴权（add-public-access，见该变更 ``architecture.md`` §3 与 ``design.md`` D4）：
:class:`notify_hub.auth.AuthGuard` 以**纯 ASGI 中间件**的形式装在**整个应用**之前，
因此它同时覆盖 API 与 Web 两条路由——包括**完全无鉴权的投递入口** ``POST /api/v1/messages``。
令牌取自 ``ctx.settings.auth_token``；为 ``None`` 时守卫关闭、行为与本变更之前完全一致
（刻意的 fail-open 默认，暴露侧的启动脚本负责拦住漏配）。

**禁止**用 try/except ImportError 之类的静默降级来让应用「跑起来」——路由缺失应当直接
在启动时暴露，而不是变成一台少了一半功能的服务器。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from notify_hub import __version__
from notify_hub.api import create_api_router
from notify_hub.auth import AuthGuard
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

    ``AuthGuard`` **只装在 ``create_app`` 里**，不装进 ``create_web_app`` / ``create_api_app``
    这两个仅供模块级测试使用的独立应用——否则既有模块测试全都要凭空携带凭据。
    生产装配的鉴权行为由跨模块集成测试负责验证。
    """
    if ctx is None:
        ctx = build_context(settings if settings is not None else load_settings())

    app = FastAPI(title="notify-hub", version=__version__, lifespan=_lifespan)
    app.state.ctx = ctx
    # 中间件必须在应用启动前挂上；装在路由之前，两条路由都被覆盖。
    app.add_middleware(AuthGuard, token=ctx.settings.auth_token)
    app.include_router(create_api_router(ctx))
    app.include_router(create_web_router(ctx))
    return app
