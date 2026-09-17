"""``python -m notify_hub`` 与 console script 的统一入口。

**本文件由架构师维护（阶段 0 共享契约），任何模块都不得修改。**
"""

from __future__ import annotations

import uvicorn

from notify_hub.app import create_app
from notify_hub.config import load_settings

__all__ = ["main"]


def main() -> None:
    """加载配置并启动单进程 uvicorn 服务。

    默认绑定回环地址（``settings.host``，缺省 ``127.0.0.1``）——这是 ``design.md``
    决策 8 的强制安全边界，不要改成 ``0.0.0.0``。
    """
    settings = load_settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
