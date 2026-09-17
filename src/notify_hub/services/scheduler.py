"""超时提醒调度：守护线程 + ``threading.Event``（架构 1.1 节，**不是** asyncio）。

``run_once()`` 的行为与返回值语义（架构 6-M6「M6 语义裁定」）：

- 对每个 ``due_for_reminder`` 的待办：重新读取（防并发完成）→ 仍为 pending 才继续 →
  构造提醒文案 → ``delivery.deliver()`` → ``todos.record_reminder()``。
- 返回值 = **本轮为该待办发起了一次提醒并完成记账的条数**；**投递成功或失败都计入**
  （失败也要按间隔重试）。只有「投递调用抛异常被隔离」与「间隙内变已完成被跳过」不计入。
- 单条待办的异常**不得**中断整轮：捕获、记日志、继续下一条。
"""

from __future__ import annotations

import logging
import threading

from notify_hub.clock import Clock
from notify_hub.config import ReminderSettings
from notify_hub.delivery import DeliveryService
from notify_hub.domain import DeliveryEvent, TodoStatus
from notify_hub.services.notifications import notification_for_message
from notify_hub.services.todos import TodoService

__all__ = ["ReminderScheduler"]


class ReminderScheduler:
    """周期性扫描到期待办并发出提醒。"""

    def __init__(
        self,
        *,
        todos: TodoService,
        delivery: DeliveryService,
        clock: Clock,
        settings: ReminderSettings,
        logger: logging.Logger,
    ) -> None:
        self._todos = todos
        self._delivery = delivery
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
                self._logger.warning("提醒扫描轮次异常: %s", exc)

    # ------------------------------------------------------------------ #
    # 单轮
    # ------------------------------------------------------------------ #
    def run_once(self) -> int:
        """执行一轮；返回实际记账（发送）提醒的待办条数。"""
        count = 0
        for candidate in self._todos.due_for_reminder(settings=self._settings):
            try:
                todo = self._todos.get(candidate.id)
                if todo is None or todo.status != TodoStatus.PENDING:
                    continue
                message = self._todos.message_for(todo.message_id)
                if message is None:
                    self._logger.warning("待办关联的消息不存在: todo_id=%s", todo.id)
                    continue

                msg = notification_for_message(
                    message,
                    kind=DeliveryEvent.REMINDER,
                    now=self._clock.now(),
                    todo=todo,
                )
                outcome = self._delivery.deliver(
                    msg,
                    preferred_channel=todo.preferred_channel,
                    message_id=todo.message_id,
                    todo_id=todo.id,
                )
                self._todos.record_reminder(
                    todo.id,
                    delivered=outcome.ok,
                    channel_id=outcome.channel_id,
                )
                count += 1
            except Exception as exc:  # noqa: BLE001 - 单条失败不得中断整轮
                self._logger.warning(
                    "提醒待办失败，跳过继续: todo_id=%s: %s",
                    getattr(candidate, "id", None),
                    exc,
                )
                continue
        return count
