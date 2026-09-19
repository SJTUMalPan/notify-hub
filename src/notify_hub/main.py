"""``python -m notify_hub`` 与 console script 的统一入口。

**本文件由架构师维护（阶段 0 共享契约），任何模块都不得修改。**
"""

from __future__ import annotations

import uvicorn

from notify_hub.app import create_app
from notify_hub.config import Settings, load_settings

__all__ = ["build_uvicorn_kwargs", "main"]


def build_uvicorn_kwargs(settings: Settings) -> dict:
    """返回 :func:`uvicorn.run` 的监听关键字参数。

    ``log_config=None`` 是 add-public-access 的**安全要求**，不是风格选择：

    uvicorn 默认安装自己的 ``dictConfig``，其中 ``uvicorn.access`` 设了
    ``propagate=False`` 并挂了自己的 handler。根 logger 上的
    :class:`notify_hub.logging_setup.SecretFilter` **覆盖不到它**，于是访问行会把
    ``GET /?token=<访问令牌> HTTP/1.1`` 明文写进日志。置空后 ``uvicorn.*`` 的日志
    全部经 propagation 回到根 logger，由既有的 SecretFilter 统一脱敏。

    见 ``openspec/changes/add-public-access/design.md`` 的 D8。
    """
    return {"host": settings.host, "port": settings.port, "log_config": None}


def main() -> None:
    """加载配置并启动单进程 uvicorn 服务。

    默认绑定回环地址（``settings.host``，缺省 ``127.0.0.1``）——这是
    ``add-notify-hub/design.md`` 决策 8 的强制安全边界，**不要改成 ``0.0.0.0``**。

    add-public-access 的公网暴露**不放松这条边界**：``0.0.0.0:3091`` 由一个独立的
    转发进程承担（见该变更 ``design.md`` 的 D9），应用自身仍只监听回环，暴露面因此
    可以被单独停止与审计。
    """
    settings = load_settings()
    uvicorn.run(create_app(settings), **build_uvicorn_kwargs(settings))


if __name__ == "__main__":
    main()
