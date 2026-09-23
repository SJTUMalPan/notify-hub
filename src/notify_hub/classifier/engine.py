"""M3 匹配引擎：把原始消息映射为 ``ClassificationVerdict``。

``RuleEngine.classify`` 是纯函数（无 IO、无状态突变、不缓存分类结果），便于在请求路径上
高频调用。规格：``openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md`` 第 6 节「模块 M3」。
"""

from __future__ import annotations

from notify_hub.classifier.rules import MatchCondition, RuleSet
from notify_hub.domain import AckReason, ClassificationVerdict, Level
from notify_hub.errors import RuleFileError

__all__ = ["RuleEngine"]


class RuleEngine:
    """按 first-match-wins 语义对一份固定 ``RuleSet`` 求值。"""

    def __init__(self, ruleset: RuleSet) -> None:
        self._ruleset = ruleset

    @property
    def ruleset(self) -> RuleSet:
        return self._ruleset

    # ------------------------------------------------------------------ #
    # 条件求值
    # ------------------------------------------------------------------ #
    def _field_value(
        self, condition: MatchCondition, *, source: str, level: Level, title: str, body: str | None
    ) -> str | None:
        field = condition.field
        if field == "source":
            return source
        if field == "level":
            return level.value
        if field == "title":
            return title
        if field == "body":
            return body
        if field == "title_contains":
            return title
        if field == "body_contains":
            return body
        # parse_ruleset 已拦截未知字段；这里兜底为「不匹配」而不是抛异常。
        return None

    def _condition_matches(
        self, condition: MatchCondition, *, source: str, level: Level, title: str, body: str | None
    ) -> bool:
        actual = self._field_value(
            condition, source=source, level=level, title=title, body=body
        )
        # body is None 时 body 类条件不匹配，且不得抛异常。
        if actual is None:
            return False

        case_sensitive = self._ruleset.case_sensitive

        if condition.mode == "equals":
            for candidate in condition.values:
                if self._compare_equals(actual, candidate, case_sensitive=case_sensitive):
                    return True
            return False

        if condition.mode == "contains":
            haystack = actual if case_sensitive else actual.casefold()
            for candidate in condition.values:
                needle = candidate if case_sensitive else candidate.casefold()
                if needle and needle in haystack:
                    return True
            return False

        raise RuleFileError(f"未知的匹配模式: {condition.mode!r}")

    @staticmethod
    def _compare_equals(actual: str, expected: str, *, case_sensitive: bool) -> bool:
        if not case_sensitive:
            return actual.casefold() == expected.casefold()
        return actual == expected

    def _rule_matches(
        self, rule, *, source: str, level: Level, title: str, body: str | None
    ) -> bool:
        # 条件之间是「与」；空元组匹配一切。
        for condition in rule.match:
            if not self._condition_matches(
                condition, source=source, level=level, title=title, body=body
            ):
                return False
        return True

    # ------------------------------------------------------------------ #
    # 分类
    # ------------------------------------------------------------------ #
    def classify(
        self,
        *,
        source: str,
        level: Level,
        title: str,
        body: str | None,
        declared_need_ack: bool,
    ) -> ClassificationVerdict:
        for rule in self._ruleset.rules:
            if not self._rule_matches(rule, source=source, level=level, title=title, body=body):
                continue

            need_ack = declared_need_ack or rule.need_ack
            if declared_need_ack:
                ack_reason = AckReason.CALLER_DECLARED
            elif rule.need_ack:
                ack_reason = AckReason.RULE
            else:
                ack_reason = AckReason.NONE

            return ClassificationVerdict(
                rule_id=rule.id,
                category=rule.category,
                labels=rule.labels,
                need_ack=need_ack,
                ack_reason=ack_reason,
                preferred_channel=rule.channel,
            )

        defaults = self._ruleset.defaults
        return ClassificationVerdict(
            rule_id=None,
            category=defaults.category,
            labels=defaults.labels,
            need_ack=declared_need_ack,
            ack_reason=AckReason.CALLER_DECLARED if declared_need_ack else AckReason.NONE,
            preferred_channel=defaults.channel,
        )
