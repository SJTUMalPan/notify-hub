"""M6：消息/待办领域服务 + 超时提醒调度。

本包提供四块内容（见 ``openspec/changes/add-notify-hub/architecture.md`` 第 6 节「模块 M6」）：

- :mod:`notify_hub.services.messages`：消息的写入与查询。
- :mod:`notify_hub.services.todos`：待办的创建、去重、完成、列表与详情。
- :mod:`notify_hub.services.notifications`：首次通知/提醒的文案构造与时长格式化。
- :mod:`notify_hub.services.scheduler`：周期性超时提醒调度（守护线程）。

依赖方向只允许向下：本包依赖 M1/M2/M4 与阶段 0 的共享契约，**不得**被 M3/M4/M5 依赖。
"""

from __future__ import annotations

from .messages import MessageDraft, MessageService
from .notifications import format_duration, notification_for_message
from .scheduler import ReminderScheduler
from .todos import CompleteOutcome, TodoDetail, TodoService, TodoView

__all__ = [
    "MessageDraft",
    "MessageService",
    "TodoService",
    "TodoView",
    "TodoDetail",
    "CompleteOutcome",
    "ReminderScheduler",
    "notification_for_message",
    "format_duration",
]
