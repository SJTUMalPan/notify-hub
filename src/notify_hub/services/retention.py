"""数据保留与自动回收：按保留期回收历史数据，使长期运行后的存储占用有界。

接口见 ``openspec/changes/add-retention-and-web-ui/architecture.md`` 第 4 节。
硬要求：

- 删除顺序不可颠倒（``db.py`` 开启了 ``PRAGMA foreign_keys=ON``）：
  1. 删已完成待办 → 先删其 ``todo_events``，并把引用它的 ``deliveries.todo_id``
     **置 NULL（保留行）**；
  2. 删消息 → 先删 ``deliveries`` 中 ``message_id`` 指向它的行，再删消息；
  3. 删过期 ``digest_runs``。
- 两条硬约束：``todos.status = pending`` 的待办永不删除；被**任何** todo 引用的
  ``messages`` 行永不删除（``todos.message_id`` 是外键）。
- 时间基准：先把时间戳换算到本地时区（``zone``）再取日期。待办用 ``completed_at``，
  消息用 ``received_at``，汇总用 ``digest_runs.local_date``。
- **禁止**在回收路径里调用 ``datetime.now()``——必须用注入的 ``Clock``。
- **禁止**在回收前临时关闭外键强制。

``days == 0`` 表示关闭回收；``days < 0`` 不是受支持的输入（由配置层拒绝）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, exists, update
from sqlmodel import select

from notify_hub.clock import Clock, as_utc
from notify_hub.db import Database
from notify_hub.domain import TodoStatus
from notify_hub.models import DeliveryRecord, DigestRun, Message, Todo, TodoEvent

__all__ = ["PurgeReport", "RetentionService"]


@dataclass(frozen=True)
class PurgeReport:
    """一次回收各步骤的计数。

    ``deliveries_unlinked`` 度量的是 ``deliveries.todo_id`` 被置 NULL 的**行数**
    （不是删除数）；``deliveries`` 是被**删除**的行数。两者度量的是不同的事，
    允许重叠：同一行投递记录可能先被解绑，随后随其消息被删。
    """

    todos: int = 0
    todo_events: int = 0
    deliveries_unlinked: int = 0
    deliveries: int = 0
    messages: int = 0
    digest_runs: int = 0

    @property
    def deleted_total(self) -> int:
        """被删除的行数合计（不含 ``deliveries_unlinked``，那些行还在）。"""
        return (
            self.todos
            + self.todo_events
            + self.deliveries
            + self.messages
            + self.digest_runs
        )

    @property
    def is_empty(self) -> bool:
        """是否什么都没删、也没解绑。"""
        return self.deleted_total == 0 and self.deliveries_unlinked == 0


class RetentionService:
    """按本地自然日计算截止日，并分三步回收历史数据。"""

    def __init__(
        self,
        db: Database,
        clock: Clock,
        *,
        days: int = 30,
        zone: ZoneInfo,
        logger: logging.Logger | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._days = days
        self._zone = zone
        self._logger = logger or logging.getLogger("notify_hub.services.retention")

    # ------------------------------------------------------------------ #
    # 截止日
    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        """``days > 0`` 才启用回收；``0`` 表示关闭。"""
        return self._days > 0

    def cutoff_date(self, now: datetime | None = None) -> date:
        """返回 ``今天 - days`` 的本地自然日。

        ``now`` 为 naive 时先经 ``as_utc()`` 归一化（SQLite 读出的时间是裸的 UTC）。
        ``days`` 极大导致日期减法溢出时返回 ``date.min``——语义上等价于「没有任何记录
        早于它」，即不回收任何东西。配置层不设上界，所以这里必须兜住。
        """
        moment = as_utc(now) if now is not None else as_utc(self._clock.now())
        try:
            return moment.astimezone(self._zone).date() - timedelta(days=self._days)
        except OverflowError:
            return date.min

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #
    def _utc_boundary(self, local_date: date) -> datetime:
        """``local_date`` 本地 00:00 对应的 naive UTC 时刻（与库内时间列同格式）。

        ``date.min`` 的「本地 00:00」换算到 UTC 会越过 ``datetime.min`` 而抛
        ``OverflowError``（``days`` 极大时 ``cutoff_date()`` 正是返回 ``date.min``）。
        按 ``cutoff_date()`` 的语义「没有任何记录早于 ``date.min``」，此处直接返回
        ``datetime.min``——它是所有库内时间的下界，比较结果恒为「不删任何东西」，
        于是溢出根本不会发生，无需在回收路径里捕获或掩盖异常。
        """
        if local_date == date.min:
            return datetime.min
        start = datetime.combine(local_date, time.min).replace(tzinfo=self._zone)
        return as_utc(start).replace(tzinfo=None)

    def _log(self, report: PurgeReport) -> None:
        """发生回收时打一条 INFO；什么都没发生时不打日志。"""
        if report.deleted_total == 0 and report.deliveries_unlinked == 0:
            return
        self._logger.info(
            "数据回收完成: todos=%s todo_events=%s deliveries_unlinked=%s "
            "deliveries=%s messages=%s digest_runs=%s",
            report.todos,
            report.todo_events,
            report.deliveries_unlinked,
            report.deliveries,
            report.messages,
            report.digest_runs,
        )

    # ------------------------------------------------------------------ #
    # 三个步骤（每步各自一个 session 独立提交）
    # ------------------------------------------------------------------ #
    def _purge_completed(self, local_date: date) -> PurgeReport:
        """第 1 步：删除完成日期早于 ``local_date`` 的已完成待办及其事件，解绑投递。"""
        boundary = self._utc_boundary(local_date)
        with self._db.session() as session:
            todo_ids = list(
                session.exec(
                    select(Todo.id).where(
                        Todo.status == TodoStatus.DONE.value,
                        Todo.completed_at.is_not(None),
                        Todo.completed_at < boundary,
                    )
                ).all()
            )
            if not todo_ids:
                return PurgeReport()
            events = session.execute(
                delete(TodoEvent).where(TodoEvent.todo_id.in_(todo_ids))
            ).rowcount
            unlinked = session.execute(
                update(DeliveryRecord)
                .where(DeliveryRecord.todo_id.in_(todo_ids))
                .values(todo_id=None)
            ).rowcount
            todos = session.execute(
                delete(Todo).where(Todo.id.in_(todo_ids))
            ).rowcount
        return PurgeReport(
            todos=todos,
            todo_events=events,
            deliveries_unlinked=unlinked,
        )

    def _purge_messages(self, local_date: date) -> PurgeReport:
        """第 2 步：删除无任何 todo 引用、且收到日期早于 ``local_date`` 的消息。"""
        boundary = self._utc_boundary(local_date)
        with self._db.session() as session:
            message_ids = list(
                session.exec(
                    select(Message.id).where(
                        Message.received_at < boundary,
                        ~exists().where(Todo.message_id == Message.id),
                    )
                ).all()
            )
            if not message_ids:
                return PurgeReport()
            deliveries = session.execute(
                delete(DeliveryRecord).where(
                    DeliveryRecord.message_id.in_(message_ids)
                )
            ).rowcount
            messages = session.execute(
                delete(Message).where(Message.id.in_(message_ids))
            ).rowcount
        return PurgeReport(deliveries=deliveries, messages=messages)

    def _purge_digest_runs(self, local_date: date) -> PurgeReport:
        """第 3 步：删除 ``local_date`` 早于截止日的汇总状态行。"""
        with self._db.session() as session:
            runs = session.execute(
                delete(DigestRun).where(DigestRun.local_date < local_date)
            ).rowcount
        return PurgeReport(digest_runs=runs)

    # ------------------------------------------------------------------ #
    # 公开接口
    # ------------------------------------------------------------------ #
    def purge_completed_older_than(self, local_date: date) -> PurgeReport:
        """只删已完成待办（完成日期早于 ``local_date``）、其事件与投递归属。

        **不动 messages 与 digest_runs。** ``completed_at`` 为 NULL 的 done 待办不删。
        """
        report = self._purge_completed(local_date)
        self._log(report)
        return report

    def purge_expired(self, now: datetime | None = None) -> PurgeReport:
        """按保留期执行完整的三步回收；``days=0`` 时立即返回全 0 报告。"""
        if not self.enabled:
            return PurgeReport()
        cutoff = self.cutoff_date(now)
        completed = self._purge_completed(cutoff)
        messages = self._purge_messages(cutoff)
        digests = self._purge_digest_runs(cutoff)
        report = PurgeReport(
            todos=completed.todos,
            todo_events=completed.todo_events,
            deliveries_unlinked=completed.deliveries_unlinked,
            deliveries=messages.deliveries,
            messages=messages.messages,
            digest_runs=digests.digest_runs,
        )
        self._log(report)
        return report
