"""全模块共享的 pytest fixture。

**本文件由架构师维护（阶段 0 共享契约），任何模块都不得修改。**
模块需要额外 fixture 时，定义在自己的测试文件里。

**惰性导入是硬要求**：文件顶部只导入标准库、pytest 与阶段 0 的共享模块。所有对 M1–M8
的导入都写在 fixture 函数体内。否则在阶段 A（模块尚未实现时）pytest 会在**收集阶段**
失败，导致所有测试文件都跑不起来——包括测试已经写好的那些模块。

见 ``openspec/changes/add-notify-hub/architecture.md`` 第 3.2 节。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from notify_hub.clock import Clock, ManualClock

#: 与 ``notify_hub.clock.DEFAULT_START`` 保持一致。
START = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

#: 共享的脱敏测试常量：用于断言日志/记录/响应中不出现它。
FAKE_TOKEN = "SECRETTOKEN"


# --------------------------------------------------------------------------- #
# 时钟
# --------------------------------------------------------------------------- #
@pytest.fixture
def manual_clock() -> ManualClock:
    """可控时钟，起点固定为 ``2024-01-01T00:00:00+00:00``。"""
    return ManualClock(START)


# --------------------------------------------------------------------------- #
# 配置 / 数据库
# --------------------------------------------------------------------------- #
_MINIMAL_RULES: dict[str, Any] = {
    "case_sensitive": False,
    "defaults": {
        "category": "uncategorized",
        "labels": [],
        "need_ack": False,
        "channel": None,
    },
    "rules": [],
}


def _write_minimal_rules(path: Path) -> Path:
    path.write_text(yaml.safe_dump(_MINIMAL_RULES, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture
def tmp_settings(tmp_path: Path):
    """临时目录下的 ``Settings``：无渠道、默认分类、提醒参数秒级化。

    刻意不声明任何渠道、``default_channel`` 为 null——这样测试注入的注册表完全掌控投递，
    投递记录里的 ``is_preferred``/``is_fallback`` 判定不会被配置干扰。
    """
    from notify_hub.config import load_settings  # M1，惰性

    _write_minimal_rules(tmp_path / "rules.yaml")
    config = {
        "server": {"host": "127.0.0.1", "port": 8000, "log_level": "INFO"},
        "storage": {"db_path": "./data/notify.db"},
        "rules": {"path": "./rules.yaml", "poll_interval_seconds": 5},
        "reminders": {
            "scan_interval_seconds": 1,
            "first_reminder_after_seconds": 2,
            "reminder_interval_seconds": 3,
        },
        "default_channel": None,
        "channels": [],
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return load_settings(config_path)


@pytest.fixture
def db(tmp_settings):
    """已建表的 ``Database``，指向 ``tmp_settings.db_path``。"""
    from notify_hub.db import Database  # M2，惰性

    database = Database(tmp_settings.db_path)
    database.init_schema()
    yield database
    database.dispose()


# --------------------------------------------------------------------------- #
# 通知渠道测试替身
# --------------------------------------------------------------------------- #
_RECORDING_CLS: Any = None


def _recording_notifier_cls():
    """惰性构造 ``RecordingNotifier`` 类（依赖 M4 的 ``DeliveryResult``）。"""
    global _RECORDING_CLS
    if _RECORDING_CLS is not None:
        return _RECORDING_CLS

    from notify_hub.notifiers.base import (  # M4，惰性
        ChannelCapabilities,
        DeliveryResult,
        NotificationMessage,
    )

    class RecordingNotifier:
        """记录每次 ``send()`` 的入参，并按配置返回成功或失败。

        绝不抛异常——除非测试显式要求（``raise_exc``），用于验证上层的异常隔离。
        """

        channel_id: str

        def __init__(
            self,
            channel_id: str = "recording",
            *,
            ok: bool = True,
            delay: float = 0.0,
            error_reason: str = "模拟投递失败",
            receipt: str | None = None,
            raise_exc: BaseException | None = None,
        ) -> None:
            self.channel_id = channel_id
            self.ok = ok
            self.delay = delay
            self.error_reason = error_reason
            self.receipt = receipt
            self.raise_exc = raise_exc
            self.sent: list[NotificationMessage] = []

        def capabilities(self) -> "ChannelCapabilities":
            return ChannelCapabilities()

        def send(self, msg: "NotificationMessage") -> "DeliveryResult":
            if self.delay:
                time.sleep(self.delay)
            self.sent.append(msg)
            if self.raise_exc is not None:
                raise self.raise_exc
            if self.ok:
                receipt = self.receipt or f"{self.channel_id}-receipt-{len(self.sent)}"
                return DeliveryResult.success(receipt=receipt)
            return DeliveryResult.failure(self.error_reason)

    _RECORDING_CLS = RecordingNotifier
    return RecordingNotifier


@pytest.fixture
def make_recording_notifier() -> Callable[..., Any]:
    """工厂 fixture：按需造多个不同配置的记录型适配器。

    签名刻意与 ``RecordingNotifier.__init__`` 对齐：``channel_id`` 既可位置传递也可关键字传递。
        make_recording_notifier()
        make_recording_notifier("slow", delay=2.0)
        make_recording_notifier(channel_id="slow", delay=2.0)
    其余关键字参数（``ok`` / ``delay`` / ``error_reason`` / ``receipt`` / ``raise_exc``）
    透传给 ``RecordingNotifier``。
    """
    cls = _recording_notifier_cls()

    def _make(channel_id: str = "recording", **kwargs: Any):
        return cls(channel_id=channel_id, **kwargs)

    return _make


@pytest.fixture
def recording_notifier(make_recording_notifier):
    """缺省的记录型适配器，``channel_id="recording"``，投递恒成功。"""
    return make_recording_notifier()


# --------------------------------------------------------------------------- #
# 应用上下文 / HTTP 客户端
# --------------------------------------------------------------------------- #
@pytest.fixture
def make_context() -> Callable[..., Any]:
    """工厂 fixture：用给定的 ``Settings`` / 时钟 / 渠道构造测试上下文。

    ``pipeline`` 以 ``run_inline=True`` 装配——投递同步完成，断言确定。需要验证
    「投递不阻塞受理响应」的用例应自行构造 ``run_inline=False`` 的上下文。
    """
    from notify_hub.context import build_test_context  # 惰性
    from notify_hub.notifiers import NotifierRegistry  # M4，惰性

    def _make(settings, *, clock: Clock | None = None, notifiers=()):
        registry = NotifierRegistry()
        for notifier in notifiers:
            registry.register(notifier)
        return build_test_context(settings, clock=clock, registry=registry)

    return _make


@pytest.fixture
def ctx(tmp_settings, manual_clock, recording_notifier, make_context):
    """模块级测试用的 ``AppContext``：可控时钟 + 一个记录型渠道。"""
    return make_context(tmp_settings, clock=manual_clock, notifiers=[recording_notifier])


@pytest.fixture
def api_client(ctx):
    """挂在 ``create_api_app(ctx)`` 上的同步 ``TestClient``。

    注意：``create_api_app`` **不启动**后台线程（那是 ``create_app`` 的 lifespan 职责），
    因此这里不适合验证 scheduler 的时间行为。
    """
    from fastapi.testclient import TestClient  # 惰性

    from notify_hub.api import create_api_app  # M7，惰性

    with TestClient(create_api_app(ctx)) as client:
        yield client
