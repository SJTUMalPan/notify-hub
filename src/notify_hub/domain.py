"""notify-hub 的跨模块共享领域类型。

这里只放**被两个以上模块共同引用的类型**：枚举与 ``ClassificationVerdict``。
本文件 **MUST NOT** 导入任何其它 ``notify_hub`` 子模块，否则会形成导入环。

**本文件由架构师维护（阶段 0 共享契约），任何模块都不得修改。**
见 ``openspec/changes/add-notify-hub/architecture.md`` 第 4.1 节。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "Level",
    "TodoStatus",
    "AckReason",
    "DeliveryEvent",
    "DeliveryTarget",
    "TodoEventKind",
    "ClassificationVerdict",
]


class Level(str, Enum):
    """消息级别。取值即对外 HTTP 契约与配置中的字面量。"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class TodoStatus(str, Enum):
    """待办状态。只允许 ``PENDING -> DONE`` 单向流转，已完成不得回退。"""

    PENDING = "pending"
    DONE = "done"


class AckReason(str, Enum):
    """消息为什么需要（或不需要）进入待办——用于事后追溯。"""

    CALLER_DECLARED = "caller_declared"  # 调用方声明 need_ack=true
    RULE = "rule"  # 命中规则的处置动作决定
    NONE = "none"  # 不入待办


class DeliveryEvent(str, Enum):
    """一次投递是首次通知还是超时提醒。"""

    FIRST_NOTICE = "first_notice"
    REMINDER = "reminder"


class DeliveryTarget(str, Enum):
    """一次投递记录挂在消息上还是待办上。"""

    MESSAGE = "message"
    TODO = "todo"


class TodoEventKind(str, Enum):
    """待办时间序列的事件类型。"""

    CREATED = "created"
    REMINDER = "reminder"
    COMPLETED = "completed"


@dataclass(frozen=True)
class ClassificationVerdict:
    """分类器的唯一输出，也是待办与投递层的唯一分类输入。

    不变量：
    - ``need_ack`` 为真时 ``ack_reason`` 必为 ``CALLER_DECLARED`` 或 ``RULE``；
      ``need_ack`` 为假时必为 ``NONE``。
    - 调用方声明 ``need_ack=True`` 时 ``need_ack`` 必为真（规则不得降级）。
    - ``category`` 永不为空串：未命中任何规则时套用配置的默认分类。
    """

    rule_id: str | None  # None 表示未命中任何规则、套用了 defaults
    category: str
    labels: tuple[str, ...]
    need_ack: bool
    ack_reason: AckReason
    preferred_channel: str | None
