"""M4 通知投递层：契约、注册表与内置适配器。

导入本包即完成内置工厂的注册（``type: "webhook"`` / ``type: "email"``），
``NotifierRegistry.build_from_specs`` 因此可直接构造真实适配器。

冻结导入路径（``context.py`` 依赖）::

    from notify_hub.notifiers import NotifierRegistry

本文件由 M4 模块负责，见 ``openspec/changes/add-notify-hub/architecture.md`` 第 6 节「模块 M4」。
"""

from __future__ import annotations

from .base import (
    ChannelCapabilities,
    DeliveryResult,
    NotificationMessage,
    Notifier,
)
from .email import EmailNotifier, build_email_notifier
from .registry import NOTIFIER_FACTORIES, NotifierRegistry
from .webhook import WebhookNotifier, build_webhook_notifier

#: 内置适配器工厂注册（key = ``ChannelSpec.type``）。
NOTIFIER_FACTORIES["webhook"] = build_webhook_notifier
NOTIFIER_FACTORIES["email"] = build_email_notifier

__all__ = [
    "ChannelCapabilities",
    "DeliveryResult",
    "NotificationMessage",
    "Notifier",
    "NotifierRegistry",
    "WebhookNotifier",
    "EmailNotifier",
    "NOTIFIER_FACTORIES",
    "build_webhook_notifier",
    "build_email_notifier",
]
