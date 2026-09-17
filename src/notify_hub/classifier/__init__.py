"""M3 分类器的对外入口。

`RuleClassifier` 是组合根（``context.py``）依赖的符号，**必须**定义在本文件：
``from notify_hub.classifier import RuleClassifier``。

``ruleset`` / ``last_error`` 实时委派给 ``RuleLoader``——热加载后调用方立刻看到新规则，
而不是构造时的快照。热加载后台线程是「守护线程 + ``threading.Event``」（并发模型见
architecture.md 1.1 节），``start()`` / ``stop()`` 均为同步方法。
"""

from __future__ import annotations

import logging
import threading

from notify_hub.classifier.engine import RuleEngine
from notify_hub.classifier.loader import RuleLoader
from notify_hub.classifier.rules import (
    DEFAULT_RULESET,
    MatchCondition,
    Rule,
    RuleDefaults,
    RuleSet,
    parse_ruleset,
)
from notify_hub.domain import ClassificationVerdict, Level

__all__ = [
    "RuleClassifier",
    "RuleEngine",
    "RuleLoader",
    "RuleSet",
    "Rule",
    "RuleDefaults",
    "MatchCondition",
    "DEFAULT_RULESET",
    "parse_ruleset",
]

_LOGGER_NAME = "notify_hub.classifier"
_THREAD_NAME = "notify-hub-rule-reloader"


class RuleClassifier:
    """分类器门面：对外提供 ``classify``，对内持有 ``RuleLoader`` 并驱动热加载。"""

    def __init__(self, loader: RuleLoader, *, logger: logging.Logger | None = None) -> None:
        self._loader = loader
        self._logger = logger if logger is not None else logging.getLogger(_LOGGER_NAME)

        self._engine: RuleEngine | None = None
        self._engine_ruleset: RuleSet | None = None
        self._engine_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

        # 构造时即尝试首次加载；失败不抛，规则集保持（初始为 DEFAULT_RULESET）。
        self._loader.load_initial()

    # ------------------------------------------------------------------ #
    # 实时委派给 loader
    # ------------------------------------------------------------------ #
    @property
    def loader(self) -> RuleLoader:
        return self._loader

    @property
    def ruleset(self) -> RuleSet:
        """实时委派——热加载后立刻可见。"""
        return self._loader.ruleset

    @property
    def last_error(self) -> str | None:
        """实时委派给 loader。"""
        return self._loader.last_error

    # ------------------------------------------------------------------ #
    # 分类
    # ------------------------------------------------------------------ #
    def _engine_for(self, ruleset: RuleSet) -> RuleEngine:
        with self._engine_lock:
            if self._engine is None or self._engine_ruleset is not ruleset:
                self._engine = RuleEngine(ruleset)
                self._engine_ruleset = ruleset
            return self._engine

    def classify(
        self,
        *,
        source: str,
        level: Level,
        title: str,
        body: str | None = None,
        declared_need_ack: bool = False,
    ) -> ClassificationVerdict:
        engine = self._engine_for(self.ruleset)
        return engine.classify(
            source=source,
            level=level,
            title=title,
            body=body,
            declared_need_ack=declared_need_ack,
        )

    # ------------------------------------------------------------------ #
    # 后台热加载线程
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """启动守护线程，每 ``poll_interval`` 秒调用一次 ``loader.poll_once()``。"""
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, name=_THREAD_NAME, daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        interval = self._loader._poll_interval
        while not self._stop_event.wait(interval):
            try:
                self._loader.poll_once()
            except Exception:  # 线程不得因异常退出
                self._logger.exception("规则热加载轮询出现未预期异常，已忽略并继续运行")

    def stop(self, timeout: float = 5.0) -> None:
        """停止后台线程；重复调用不抛异常。"""
        with self._thread_lock:
            thread = self._thread
            self._stop_event.set()
            self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout)
