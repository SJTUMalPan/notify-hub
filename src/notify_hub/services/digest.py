"""每日汇总的状态服务：把「某个本地自然日是否已定案/已发出」全部落到 ``digest_runs`` 表。

见 ``openspec/changes/add-daily-digest/architecture.md`` 第 3.2 / 3.3 节。

设计要点（决策 1/2/3）：

- **状态只在库里**：不设内存标记，进程重启后仍能判断「当天发没发、试了几次」。
- **按 ``local_date`` 幂等**：每天至多一行；首次写入 ``attempts = 1`` 且记录定案时刻
  ``checked_at``，之后每次调用只累加 ``attempts``，``checked_at`` 保持不变。
- **失败重试不抹除历史**：``fired_at`` / ``error`` 为 ``None`` 时保留既有值——
  一次失败的跟进调用不得把此前成功发出的时刻或原因清空。
- 从库里读出的时间列一律先经 :func:`notify_hub.clock.as_utc` 归一化（架构 1.2 节）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlmodel import select

from notify_hub.clock import Clock, as_utc
from notify_hub.db import Database
from notify_hub.models import DigestRun

__all__ = ["DigestState", "DigestService"]


@dataclass(frozen=True)
class DigestState:
    """某一本地自然日的汇总状态（对外视图）。"""

    local_date: date
    checked_at: datetime
    fired_at: datetime | None
    todo_count: int
    delivered: bool
    attempts: int
    last_error: str | None


def _state(row: DigestRun) -> DigestState:
    return DigestState(
        local_date=row.local_date,
        checked_at=as_utc(row.checked_at),
        fired_at=as_utc(row.fired_at) if row.fired_at is not None else None,
        todo_count=row.todo_count,
        delivered=row.delivered,
        attempts=row.attempts,
        last_error=row.last_error,
    )


class DigestService:
    """每日汇总状态的唯一读写入口。"""

    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def _find(self, session, local_date: date) -> DigestRun | None:
        return session.exec(
            select(DigestRun).where(DigestRun.local_date == local_date)
        ).first()

    def state_for(self, local_date: date) -> DigestState | None:
        """查该本地日期的记录；不存在返回 ``None``。"""
        with self._db.session() as session:
            row = self._find(session, local_date)
            if row is None:
                return None
            return _state(row)

    def record(
        self,
        local_date: date,
        *,
        todo_count: int,
        delivered: bool,
        fired_at: datetime | None = None,
        error: str | None = None,
    ) -> DigestState:
        """写入或更新该本地日期的记录（按 ``local_date`` 幂等）。

        - 首次调用：``attempts = 1``、``checked_at = clock.now()``
        - 重复调用：``attempts += 1``、``checked_at`` 保持不变、其余字段以本次为准
        - ``fired_at`` 为 ``None`` 时保留已有值（失败重试不得抹掉此前成功发出的时刻）
        - ``error`` 为 ``None`` 时保留已有值（同上）
        - ``error`` 由调用方保证**已脱敏**
        """
        now = self._clock.now()
        with self._db.session() as session:
            row = self._find(session, local_date)
            if row is None:
                row = DigestRun(
                    local_date=local_date,
                    checked_at=now,
                    fired_at=fired_at,
                    todo_count=todo_count,
                    delivered=delivered,
                    attempts=1,
                    last_error=error,
                )
            else:
                row.attempts = row.attempts + 1
                row.todo_count = todo_count
                row.delivered = delivered
                if fired_at is not None:
                    row.fired_at = fired_at
                if error is not None:
                    row.last_error = error
            session.add(row)
            session.flush()
            return _state(row)
