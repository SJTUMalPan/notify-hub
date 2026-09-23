"""M3 规则加载器：文件解析、mtime 轮询热加载、坏配置降级。

不变量（architecture.md M3 第 2/3 节）：
* ``ruleset`` 始终可用——初始为 ``DEFAULT_RULESET``。
* ``load_initial`` / ``reload`` / ``poll_once`` **MUST NOT** 向调用方抛异常。
* 失败时保留上一份可用规则集，且 ``loaded_at`` 取注入 clock 的 ``now()``。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import yaml

from notify_hub.classifier.rules import DEFAULT_RULESET, RuleSet, parse_ruleset
from notify_hub.clock import Clock, SystemClock

__all__ = ["RuleLoader"]

_LOGGER_NAME = "notify_hub.classifier"


class RuleLoader:
    """从 YAML 文件加载 ``RuleSet``，并按 mtime 变化支持热加载。"""

    def __init__(
        self,
        path: str | Path,
        *,
        poll_interval: float = 5.0,
        clock: Clock | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._path = Path(path)
        self._poll_interval = float(poll_interval)
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._logger = logger if logger is not None else logging.getLogger(_LOGGER_NAME)

        self._ruleset: RuleSet = DEFAULT_RULESET
        self._last_error: str | None = None
        self._mtime_ns: int | None = None

    # ------------------------------------------------------------------ #
    # 只读属性
    # ------------------------------------------------------------------ #
    @property
    def path(self) -> Path:
        return self._path

    @property
    def ruleset(self) -> RuleSet:
        return self._ruleset

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def loaded_at(self) -> datetime:
        return self._ruleset.loaded_at

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #
    def _read_mtime_ns(self) -> int | None:
        try:
            return self._path.stat().st_mtime_ns
        except OSError:
            return None

    def _log_failure(self, reason: str) -> None:
        # 规格要求的可定位告警文案：文件名 + 原因。
        self._logger.warning(
            "规则文件解析失败，继续使用上一份可用规则集: %s: %s", self._path, reason
        )

    def load_initial(self) -> bool:
        """首次加载。失败返回 ``False``、记 ``last_error``、不抛，且不降级已有规则集。"""
        return self._load()

    def reload(self) -> bool:
        """强制重载。失败保留旧集合、记 ``last_error``、返回 ``False``、不抛。"""
        return self._load()

    def _load(self) -> bool:
        try:
            text = self._path.read_text(encoding="utf-8")
            data = yaml.safe_load(text)
            ruleset = parse_ruleset(
                data, source_path=self._path, loaded_at=self._clock.now()
            )
        except Exception as exc:  # 坏配置不得让服务不可用
            reason = f"{type(exc).__name__}: {exc}"
            self._last_error = (
                f"规则文件解析失败，继续使用上一份可用规则集: {self._path}: {reason}"
            )
            self._mtime_ns = self._read_mtime_ns()
            self._log_failure(reason)
            # 注意：不动 self._ruleset —— 已有可用规则集不得被降级为 DEFAULT_RULESET。
            return False

        self._ruleset = ruleset
        self._last_error = None
        self._mtime_ns = self._read_mtime_ns()
        return True

    # ------------------------------------------------------------------ #
    # 轮询
    # ------------------------------------------------------------------ #
    def poll_once(self) -> bool:
        """mtime 未变返回 ``False``；变了则等价于 ``reload()``。

        返回值语义 = 规则集是否**真的被替换**（检测到变更但重载失败 -> ``False``）。
        """
        current = self._read_mtime_ns()
        if current == self._mtime_ns:
            return False
        return self._load()
