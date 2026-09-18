"""M8 Web 待办界面的对外入口。

冻结契约（``architecture.md`` 第 6 节「模块 M8」）：

- ``create_web_router(ctx) -> APIRouter``：Web 页面路由，由架构师在 ``create_app()`` 中挂载。
- ``notify_hub.web.routes.create_web_app(ctx) -> FastAPI``：模块级测试用（不启动后台线程）。
- ``notify_hub.web.routes.TEMPLATES_DIR: Path``。
"""

from __future__ import annotations

from .routes import TEMPLATES_DIR, create_web_app, create_web_router

__all__ = ["create_web_router", "create_web_app", "TEMPLATES_DIR"]
