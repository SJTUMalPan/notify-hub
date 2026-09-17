"""组合根：把各模块装配成一个 ``AppContext``。

**为什么由架构师写**：这是整份规格里唯一需要「全局视野」的地方——它同时引用 M1–M7 的
构造函数。把它放在阶段 0 写完，模块之间就不必互相等待，也不会出现两个模块同时改装配代码
而互相覆盖的情况。模块因此被降级为「构造签名已冻结的库」。

**本文件由架构师维护（阶段 0 共享契约），任何模块都不得修改。**
见 ``openspec/changes/add-notify-hub/architecture.md`` 第 4.4 节。

注意：本文件在阶段 A（模块尚未实现时）无法导入成功，这是**预期**的。子代理不得为了
「让它能 import」而在这里加 try/except 或桩代码。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from notify_hub.classifier import RuleClassifier
from notify_hub.classifier.loader import RuleLoader
from notify_hub.clock import Clock, SystemClock
from notify_hub.config import Settings, credential_values
from notify_hub.db import Database
from notify_hub.delivery import DeliveryService
from notify_hub.logging_setup import setup_logging
from notify_hub.notifiers import NotifierRegistry
from notify_hub.pipeline import IngestPipeline
from notify_hub.services.messages import MessageService
from notify_hub.services.scheduler import ReminderScheduler
from notify_hub.services.todos import TodoService

__all__ = ["AppContext", "build_context", "build_test_context"]


@dataclass(frozen=True)
class AppContext:
    """服务运行期全部长生命周期对象的集合。

    路由、Web 页面与测试都通过 ``request.app.state.ctx`` 取得本对象。
    """

    settings: Settings
    clock: Clock
    logger: logging.Logger
    db: Database
    classifier: RuleClassifier
    registry: NotifierRegistry
    delivery: DeliveryService
    messages: MessageService
    todos: TodoService
    pipeline: IngestPipeline
    scheduler: ReminderScheduler


def _assemble(
    settings: Settings,
    *,
    clock: Clock | None,
    registry: NotifierRegistry | None,
    run_inline: bool,
) -> AppContext:
    """按固定顺序装配。顺序有依赖含义，不得随意调整（见规格 4.4）。"""
    # 1. 凭据值先算出来，供日志脱敏使用——必须在任何可能记录日志的动作之前完成。
    secrets = credential_values(settings)

    # 2. 日志：安装 SecretFilter，保证之后任何日志都不会带出凭据。
    logger = setup_logging(settings.log_level, secrets=secrets)

    # 3. 数据库：建表是幂等的。
    db = Database(settings.db_path)
    db.init_schema()

    # 4. 时钟。
    resolved_clock: Clock = clock if clock is not None else SystemClock()

    # 5. 分类器：构造时加载一次规则；加载失败会降级到内置默认规则集（不抛异常）。
    loader = RuleLoader(
        settings.rules_path,
        poll_interval=settings.rules_poll_interval_seconds,
        clock=resolved_clock,
        logger=logger,
    )
    classifier = RuleClassifier(loader, logger=logger)

    # 6. 渠道注册表：外部注入时跳过配置构造（测试注入 RecordingNotifier 的路径）。
    if registry is None:
        registry = NotifierRegistry()
        registry.build_from_specs(settings.channels, secrets=secrets)

    # 7. 投递服务。
    delivery = DeliveryService(
        db,
        registry,
        default_channel=settings.default_channel,
        channel_order=registry.ids(),
        clock=resolved_clock,
        logger=logger,
        secrets=secrets,
    )

    # 8-9. 领域服务。
    messages = MessageService(db, resolved_clock)
    todos = TodoService(db, resolved_clock, logger=logger)

    # 10. 受理编排（含首次通知的后台工作线程）。
    pipeline = IngestPipeline(
        classifier=classifier,
        messages=messages,
        todos=todos,
        delivery=delivery,
        clock=resolved_clock,
        logger=logger,
        run_inline=run_inline,
    )

    # 11. 超时提醒调度。
    scheduler = ReminderScheduler(
        todos=todos,
        delivery=delivery,
        clock=resolved_clock,
        settings=settings.reminders,
        logger=logger,
    )

    return AppContext(
        settings=settings,
        clock=resolved_clock,
        logger=logger,
        db=db,
        classifier=classifier,
        registry=registry,
        delivery=delivery,
        messages=messages,
        todos=todos,
        pipeline=pipeline,
        scheduler=scheduler,
    )


def build_context(
    settings: Settings,
    *,
    clock: Clock | None = None,
    registry: NotifierRegistry | None = None,
) -> AppContext:
    """生产装配：首次通知经后台工作线程派发（不阻塞受理响应）。"""
    return _assemble(settings, clock=clock, registry=registry, run_inline=False)


def build_test_context(
    settings: Settings,
    *,
    clock: Clock | None = None,
    registry: NotifierRegistry | None = None,
) -> AppContext:
    """测试装配：``pipeline`` 以 ``run_inline=True`` 构造，投递同步完成以便断言确定。"""
    return _assemble(settings, clock=clock, registry=registry, run_inline=True)
