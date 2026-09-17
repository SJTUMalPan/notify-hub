"""日志基础：一次性配置根 logger，并保证任何 handler 都不会输出凭据。

见 ``openspec/changes/add-notify-hub/architecture.md`` 第 6 节「模块 M1」第 2/3 段。

关键点：``SecretFilter`` 装在**根 logger 的每个 handler** 上（不只装在 logger 上），
这样经由 propagation 到达的任意子 logger 记录、以及 ``caplog`` 的捕获 handler
都会被脱敏。
"""

from __future__ import annotations

import logging
from typing import Iterable

from notify_hub.redact import redact_text

__all__ = ["SecretFilter", "setup_logging", "get_logger"]

#: 模块级标志：保证根 logger 的 handler 只添加一次。
_configured = False

_DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class SecretFilter(logging.Filter):
    """对每条 ``LogRecord`` 的 ``msg``/``args`` 施加 :func:`redact_text`。"""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__(name="notify_hub.secret_filter")
        self._secrets: tuple[str, ...] = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API 名称
        if not self._secrets:
            return True

        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg, self._secrets)
        elif record.msg is not None:
            record.msg = redact_text(str(record.msg), self._secrets)

        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_text(arg, self._secrets) if isinstance(arg, str) else arg
                for arg in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: (redact_text(value, self._secrets) if isinstance(value, str) else value)
                for key, value in record.args.items()
            }
        return True


def _install_secret_filter(handler: logging.Handler, secrets: tuple[str, ...]) -> None:
    """幂等地替换 handler 上的 SecretFilter，使最新 secrets 生效。"""
    handler.filters = [
        existing for existing in handler.filters
        if not isinstance(existing, SecretFilter)
    ]
    handler.addFilter(SecretFilter(secrets))


def setup_logging(level: str = "INFO", *, secrets: Iterable[str] = ()) -> logging.Logger:
    """配置根 logger（仅一次，重复调用不叠加 handler），安装 :class:`SecretFilter`。"""
    global _configured

    secret_tuple = tuple(s for s in secrets if s)
    root = logging.getLogger()
    if not _configured:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
        root.addHandler(handler)
        _configured = True

    resolved_level = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(resolved_level)
    for handler in root.handlers:
        _install_secret_filter(handler, secret_tuple)
        if handler.level == logging.NOTSET:
            handler.setLevel(resolved_level)

    logger = logging.getLogger("notify_hub")
    logger.setLevel(resolved_level)
    _install_secret_filter(logger, secret_tuple)
    return logger


def get_logger(name: str = "notify_hub") -> logging.Logger:
    """返回命名 logger（缺省为项目根 logger ``'notify_hub'``）。"""
    return logging.getLogger(name)
