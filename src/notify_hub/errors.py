"""notify-hub 的跨模块异常类型。

**本文件由架构师维护（阶段 0 共享契约），任何模块都不得修改。**
见 ``openspec/changes/add-notify-hub/architecture.md`` 第 3 节。
"""

from __future__ import annotations

__all__ = [
    "NotifyHubError",
    "ConfigurationError",
    "RuleFileError",
    "TodoNotFound",
    "MessageNotFound",
]


class NotifyHubError(Exception):
    """本项目所有自定义异常的基类。"""


class ConfigurationError(NotifyHubError):
    """配置缺失或非法。

    安全约束：异常的 ``str()`` 中 **MUST NOT** 出现任何凭据值（webhook token、
    SMTP 密码等）。只能出现配置键名与环境变量名。
    """


class RuleFileError(NotifyHubError):
    """分类规则文件存在语法或语义错误。

    消息需能定位到具体位置：文件名 + 规则 id 或下标 + 问题描述。
    """


class TodoNotFound(NotifyHubError):
    """指定的待办不存在。"""


class MessageNotFound(NotifyHubError):
    """指定的消息不存在。"""
