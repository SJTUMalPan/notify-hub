"""健康检查端点（architecture.md 6-M7 第 2、3 段；spec message-ingest）。

**MUST NOT** 探测任何通知渠道、调用投递层或访问网络——渠道故障时仍要能区分
「服务本身挂了」与「渠道发不出去」。因此这里只读时钟与版本号。

本文件由 M7 模块负责。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from notify_hub import __version__
from notify_hub.context import AppContext

from .schemas import HealthOut

__all__ = ["router"]

router = APIRouter(tags=["health"])


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


@router.get("/healthz", response_model=HealthOut)
def healthz(ctx: AppContext = Depends(_ctx)) -> HealthOut:
    """返回服务可用状态与当前时间（tz-aware UTC）。"""
    return HealthOut(status="ok", time=ctx.clock.now(), version=__version__)
