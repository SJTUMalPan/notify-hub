"""受理编排：分类 → 落库 → 生成待办 → 首次通知派发（architecture.md 6-M7）。

设计要点（偏差 D-1，已获用户批准）：首次通知**不用** FastAPI ``BackgroundTasks``
（``TestClient`` 下会阻塞响应 1.01s），而是用进程内 ``queue.Queue`` + 单个守护工作线程。

不变量：
- ``run_inline=False`` 时 :meth:`IngestPipeline.accept` 只入队，**绝不在请求路径上投递**。
- 工作线程内的任何异常都被捕获并记日志，**线程不得退出**。
- 线程生命周期由调用方显式管理：``build_test_context()`` / ``create_api_app()`` 都不启动
  后台线程，需要异步语义的用例自行 ``start()``。

本文件由 M7 模块负责。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass

from notify_hub.clock import Clock
from notify_hub.delivery import DeliveryOutcome, DeliveryService
from notify_hub.domain import ClassificationVerdict, DeliveryEvent
from notify_hub.services.messages import MessageDraft, MessageService
from notify_hub.services.notifications import notification_for_message
from notify_hub.services.todos import TodoService

__all__ = ["AcceptOutcome", "IngestPipeline"]

#: 工作线程轮询间隔（秒）。
_POLL_SECONDS = 0.05
_THREAD_NAME = "notify-hub-ingest-worker"


@dataclass(frozen=True)
class AcceptOutcome:
    """一条消息的受理结果。"""

    message_id: int
    todo_id: int | None
    verdict: ClassificationVerdict


class IngestPipeline:
    """受理编排器：同步完成分类/落库/待办，首次通知按配置内联或入队。"""

    def __init__(
        self,
        *,
        classifier,
        messages: MessageService,
        todos: TodoService,
        delivery: DeliveryService,
        clock: Clock,
        logger: logging.Logger,
        run_inline: bool = False,
    ) -> None:
        self._classifier = classifier
        self._messages = messages
        self._todos = todos
        self._delivery = delivery
        self._clock = clock
        self._logger = logger
        self._run_inline = run_inline

        self._queue: "queue.Queue[int]" = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 受理
    # ------------------------------------------------------------------ #
    def accept(self, draft: MessageDraft) -> AcceptOutcome:
        """分类 → 落库 → 生成待办 →（内联投递 | 入队）。"""
        verdict = self._classifier.classify(
            source=draft.source,
            level=draft.level,
            title=draft.title,
            body=draft.body,
            declared_need_ack=draft.need_ack,
        )
        message = self._messages.create(draft, verdict)
        todo = self._todos.ensure_for_message(message)
        todo_id = todo.id if todo is not None else None

        if self._run_inline:
            self.dispatch(message.id)
        else:
            self._queue.put(message.id)

        return AcceptOutcome(
            message_id=message.id,
            todo_id=todo_id,
            verdict=verdict,
        )

    # ------------------------------------------------------------------ #
    # 首次通知派发
    # ------------------------------------------------------------------ #
    def dispatch(self, message_id: int) -> DeliveryOutcome | None:
        """读消息 → 构造首次通知 → 投递。消息不存在则记日志返回 ``None``。"""
        message = self._messages.get(message_id)
        if message is None:
            self._logger.warning("首次通知派发时消息不存在: id=%s", message_id)
            return None

        todo = self._messages.todo_for(message_id)
        notification = notification_for_message(
            message,
            kind=DeliveryEvent.FIRST_NOTICE,
            now=self._clock.now(),
            todo=None,
        )
        outcome = self._delivery.deliver(
            notification,
            preferred_channel=message.preferred_channel,
            message_id=message_id,
            todo_id=todo.id if todo is not None else None,
        )
        return outcome

    # ------------------------------------------------------------------ #
    # 工作线程生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """启动单个守护工作线程；重复调用幂等。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, name=_THREAD_NAME, daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """停止工作线程；可重复调用且不抛异常。"""
        with self._lock:
            thread = self._thread
            self._stop_event.set()
            self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def _run(self) -> None:
        """工作线程主体：任何异常都不得让它退出。"""
        while not self._stop_event.wait(_POLL_SECONDS):
            got = False
            try:
                message_id = self._queue.get(timeout=_POLL_SECONDS)
                got = True
                self.dispatch(message_id)
            except queue.Empty:
                continue
            except Exception:  # noqa: BLE001 - 线程不得因异常退出
                self._logger.exception(
                    "首次通知派发出现未预期异常，已忽略并继续运行"
                )
            finally:
                if got:
                    self._queue.task_done()

    def drain(self, timeout: float = 5.0) -> bool:
        """等待队列排空且当前任务完成；超时返回 ``False``。测试专用。"""
        if self._run_inline:
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        while self._queue.unfinished_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.01, remaining))
        return self._queue.unfinished_tasks == 0

    @property
    def pending(self) -> int:
        """尚未被工作线程取走的队列长度。"""
        return self._queue.qsize()
