"""跨模块共享的时钟抽象与时间归一化。

**为什么需要它**：整个服务的时间语义必须一致，而测试必须能在不真实等待的前提下
推进时间（超时提醒、提醒间隔都依赖它）。实测确认 SQLite **不保留时区**，从 ORM 读出的
``datetime`` 必然是 naive 的，直接参与算术会静默出错——所以 ``as_utc()`` 是强制的。

**本文件由架构师维护（阶段 0 共享契约），任何模块都不得修改。**
见 ``openspec/changes/add-notify-hub/architecture.md`` 第 4.3 节。

Python 3.10：使用 ``datetime.timezone.utc``，**不得**使用 3.11+ 的 ``datetime.UTC``。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

__all__ = ["UTC", "as_utc", "Clock", "SystemClock", "ManualClock"]

UTC = timezone.utc

#: ``ManualClock`` 的缺省起点，与 ``tests/conftest.py`` 保持一致。
DEFAULT_START = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)


def as_utc(value: datetime) -> datetime:
    """把时间戳归一化为 tz-aware UTC。

    naive 的时间戳按 UTC 解释（本项目所有入库时间都以 UTC 写入）。已是 aware 的
    时间戳转换到 UTC。

    **调用约定**：任何从数据库读出、即将参与比较或算术的时间，都必须先经过本函数。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@runtime_checkable
class Clock(Protocol):
    """时间来源。所有需要「现在」的代码都通过它取时间，不得直接调用 ``datetime.now()``。"""

    def now(self) -> datetime:
        """返回 tz-aware 的 UTC 当前时间。"""
        ...


class SystemClock:
    """生产用时钟。"""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """测试用可控时钟：只有显式调用 ``advance``/``set`` 才会前进。"""

    __slots__ = ("_now",)

    def __init__(self, start: datetime | None = None) -> None:
        self._now = as_utc(start) if start is not None else DEFAULT_START

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        """前进给定秒数（可为负，便于构造边界，但常规用例只前进）。"""
        self._now = self._now + timedelta(seconds=seconds)

    def set(self, moment: datetime) -> None:
        """直接跳到某个时刻。"""
        self._now = as_utc(moment)
