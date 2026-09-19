"""每日汇总调度：守护线程 + ``threading.Event``（架构 1.1 节，**不是** asyncio）。

``add-daily-digest`` 把「单项超时间隔提醒」整体替换为「每天固定本地时刻发一条汇总」。
判定顺序按 ``openspec/changes/add-daily-digest/architecture.md`` 第 3.3 节的冻结伪码：

1. 本地时间未到 ``settings.trigger_time`` → 返回 0；
2. 当日已定案（``delivered`` 或 ``todo_count == 0``）→ 返回 0；
3. 空待办 → 记 ``todo_count=0, delivered=True`` 定案，返回 0；
4. 投递汇总（渠道交给 ``DeliveryService`` 的默认渠道与降级链，调度器**不做渠道选择**）；
5. 成功 → 记 ``delivered=True / fired_at``，逐条待办记账，返回 1；失败 → 记失败原因，返回 0。

硬要求：

- 状态全在 ``digest_runs`` 表，模块内**不得**保存「今天发过了」的内存标记；
- 单条待办记账失败不得中断整轮；整轮任何异常都必须捕获并记日志，线程不得退出；
- 单轮最多汇总 1000 条待办（刻意的上限，不是分页）。
"""

from __future__ import annotations

import logging
import threading

from notify_hub.clock import Clock
from notify_hub.config import ReminderSettings
from notify_hub.delivery import DeliveryService
from notify_hub.domain import TodoStatus
from notify_hub.services.digest import DigestService
from notify_hub.services.notifications import notification_for_digest
from notify_hub.services.todos import TodoService

__all__ = ["ReminderScheduler"]

#: 单轮汇总的待办上限：避免异常情况下构造出超大消息（架构 3.3 节）。
_MAX_TODOS_PER_ROUND = 1000


class ReminderScheduler:
    """周期性检查本地时刻，每天发出至多一条汇总。"""

    def __init__(
        self,
        *,
        todos: TodoService,
        delivery: DeliveryService,
        digest: DigestService,
        clock: Clock,
        settings: ReminderSettings,
        logger: logging.Logger,
    ) -> None:
        self._todos = todos
        self._delivery = delivery
        self._digest = digest
        self._clock = clock
        self._settings = settings
        self._logger = logger
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False

    # ------------------------------------------------------------------ #
    # 生命周期（同步方法）
    # ------------------------------------------------------------------ #
    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        """启动守护线程；重复调用是 no-op。"""
        if self._running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="notify-hub-reminder-scheduler",
            daemon=True,
        )
        self._running = True
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """停止守护线程；可重复调用且不抛异常。"""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None
        self._running = False

    def _loop(self) -> None:
        interval = self._settings.scan_interval_seconds
        if interval <= 0:
            interval = 1.0
        while not self._stop_event.wait(interval):
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - 线程不得因异常退出
                self._logger.warning("汇总检查轮次异常: %s", exc)

    # ------------------------------------------------------------------ #
    # 单轮
    # ------------------------------------------------------------------ #
    def run_once(self) -> int:
        """执行一轮检查；返回本次**成功投递**的汇总条数（0 或 1）。"""
        try:
            return self._run_once()
        except Exception as exc:  # noqa: BLE001 - 整轮异常隔离
            self._logger.warning("汇总轮次异常: %s", exc)
            return 0

    def _run_once(self) -> int:
        now = self._clock.now()
        local = now.astimezone(self._settings.zone)
        if local.time() < self._settings.trigger_time:
            return 0

        today = local.date()
        state = self._digest.state_for(today)
        settled = state is not None and (state.delivered or state.todo_count == 0)
        if settled:
            return 0

        todos = self._todos.list(status=TodoStatus.PENDING, limit=_MAX_TODOS_PER_ROUND)
        if len(todos) >= _MAX_TODOS_PER_ROUND:
            self._logger.warning(
                "单轮待办达到上限 %s 条，超出部分本轮不汇总", _MAX_TODOS_PER_ROUND
            )

        if not todos:
            self._digest.record(today, todo_count=0, delivered=True)
            return 0

        message = notification_for_digest(todos, now=now)
        # 渠道选择交给 DeliveryService 自身的默认渠道与既有降级链。
        outcome = self._delivery.deliver(
            message, preferred_channel=None, message_id=None, todo_id=None
        )

        if not outcome.ok:
            self._digest.record(
                today,
                todo_count=len(todos),
                delivered=False,
                error=outcome.error_reason,
            )
            return 0

        self._digest.record(
            today,
            todo_count=len(todos),
            delivered=True,
            fired_at=self._clock.now(),
        )
        for todo in todos:
            try:
                self._todos.record_reminder(
                    todo.id,
                    delivered=True,
                    channel_id=outcome.channel_id,
                )
            except Exception as exc:  # noqa: BLE001 - 单条记账失败不得中断整轮
                self._logger.warning(
                    "汇总记账失败，跳过继续: todo_id=%s: %s", todo.id, exc
                )
        return 1
