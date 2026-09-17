"""模块 M3（分类器：规则加载 / 匹配 / 热加载）的模块级测试。

规格来源（写测试时的唯一依据）：
* ``openspec/changes/add-notify-hub/architecture.md`` 第 6 节「模块 M3」第 2/3/4 段
* ``openspec/changes/add-notify-hub/specs/message-classification/spec.md``（权威需求）
* ``openspec/changes/add-notify-hub/design.md`` 决策 3
* 共享契约：``domain.py`` / ``errors.py`` / ``clock.py``（第 4.1–4.3 节）

本文件写于实现之前：``src/notify_hub/classifier/`` 与项目根 ``rules.example.yaml`` 由开发
子代理交付。因此**所有对 M3 的导入都写在辅助函数体内（惰性导入）**，与
``tests/conftest.py`` 的约定一致——这样 pytest 能成功**收集**本文件，失败发生在每个用例的
运行时（``ModuleNotFoundError: notify_hub.classifier``），而不是在收集阶段。

热加载相关用例**不使用**真实 ``sleep`` 等待文件变更：一律显式改写 mtime（``os.utime``）
并用 ``ManualClock`` 推进时间后直接调用 ``poll_once()``。唯一的 ``sleep`` 出现在等待线程
登记/退出的有界轮询里（10ms 粒度、2s 上限），与文件变更无关。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from notify_hub.clock import ManualClock, as_utc
from notify_hub.domain import AckReason, Level
from notify_hub.errors import RuleFileError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_RULES = PROJECT_ROOT / "rules.example.yaml"

_DEFAULTS: dict[str, Any] = {
    "category": "uncategorized",
    "labels": [],
    "need_ack": False,
    "channel": None,
}


# --------------------------------------------------------------------------- #
# 惰性导入辅助（M3 尚不存在时，导入失败必须发生在用例运行时）
# --------------------------------------------------------------------------- #
def _load_rules_module():
    from notify_hub.classifier import rules as rules_module

    return rules_module


def _parse(data: dict[str, Any], *, path: Path, clock: ManualClock):
    from notify_hub.classifier.rules import parse_ruleset

    return parse_ruleset(data, source_path=path, loaded_at=clock.now())


def _make_loader(
    path: Path,
    *,
    clock: ManualClock,
    poll_interval: float = 5.0,
    logger: logging.Logger | None = None,
):
    from notify_hub.classifier.loader import RuleLoader

    return RuleLoader(path, poll_interval=poll_interval, clock=clock, logger=logger)


def _make_classifier(loader, *, logger: logging.Logger | None = None):
    from notify_hub.classifier import RuleClassifier

    return RuleClassifier(loader, logger=logger)


def _make_engine(ruleset):
    from notify_hub.classifier.engine import RuleEngine

    return RuleEngine(ruleset)


# --------------------------------------------------------------------------- #
# 规则文件与 mtime 辅助
# --------------------------------------------------------------------------- #
def _write_rules(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _touch_forward(path: Path, seconds: float = 10.0) -> None:
    """显式把 mtime 前移，代替真实等待文件系统的秒级时间戳。"""
    mtime_ns = path.stat().st_mtime_ns + int(seconds * 1_000_000_000)
    os.utime(path, ns=(mtime_ns, mtime_ns))


def _write_invalid_yaml(path: Path) -> Path:
    """非法 YAML：flow sequence 未闭合。"""
    path.write_text("rules: [{id: x}\n", encoding="utf-8")
    return path


def _ruleset_data(
    rules: list[dict[str, Any]],
    *,
    case_sensitive: bool = False,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "case_sensitive": case_sensitive,
        "defaults": dict(defaults or _DEFAULTS),
        "rules": rules,
    }


def _classify(classifier, **kwargs):
    params: dict[str, Any] = {"source": "svc", "level": Level.INFO, "title": "t"}
    params.update(kwargs)
    return classifier.classify(**params)


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    """有界等待（仅用于线程登记/退出，不用于文件变更轮询）。"""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _extra_threads(baseline: set[int]) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.ident not in baseline]


# --------------------------------------------------------------------------- #
# 1. 加载（tasks 4.1）
# --------------------------------------------------------------------------- #
def test_load_example_rules(tmp_path: Path, manual_clock: ManualClock) -> None:
    assert EXAMPLE_RULES.is_file(), "项目根必须存在 rules.example.yaml（M3 的交付物）"
    target = tmp_path / "rules.yaml"
    target.write_text(EXAMPLE_RULES.read_text(encoding="utf-8"), encoding="utf-8")

    loader = _make_loader(target, clock=manual_clock)
    assert loader.load_initial() is True

    ruleset = loader.ruleset
    assert len(ruleset.rules) == 1
    rule = ruleset.rules[0]
    assert rule.id == "backup-failure"
    assert rule.category == "backup-failure"
    assert rule.need_ack is True
    assert rule.channel == "email"
    assert rule.labels == ("infra", "backup")


def test_example_rules_classify_the_documented_scenario(
    tmp_path: Path, manual_clock: ManualClock
) -> None:
    """spec「命中规则并套用处置动作」场景。"""
    target = tmp_path / "rules.yaml"
    target.write_text(EXAMPLE_RULES.read_text(encoding="utf-8"), encoding="utf-8")

    classifier = _make_classifier(_make_loader(target, clock=manual_clock))
    verdict = classifier.classify(
        source="db-backup", level=Level.ERROR, title="备份失败", body="exit code 1"
    )

    assert verdict.rule_id == "backup-failure"
    assert verdict.category == "backup-failure"
    assert verdict.labels == ("infra", "backup")
    assert verdict.need_ack is True
    assert verdict.ack_reason is AckReason.RULE
    assert verdict.preferred_channel == "email"


def test_ruleset_is_default_before_initial_load(
    tmp_path: Path, manual_clock: ManualClock
) -> None:
    rules_module = _load_rules_module()
    loader = _make_loader(_write_rules(tmp_path / "rules.yaml", _ruleset_data([], )), clock=manual_clock)
    assert loader.ruleset == rules_module.DEFAULT_RULESET


def test_default_ruleset_shape() -> None:
    rules_module = _load_rules_module()
    fallback = rules_module.DEFAULT_RULESET
    assert fallback.rules == ()
    assert fallback.case_sensitive is False
    assert fallback.source_path == Path("<builtin>")
    assert fallback.defaults.category == "uncategorized"
    assert fallback.defaults.labels == ()
    assert fallback.defaults.need_ack is False
    assert fallback.defaults.channel is None


def test_loaded_at_uses_injected_clock(tmp_path: Path, manual_clock: ManualClock) -> None:
    """4.3 节不变量：测试中 ManualClock 是唯一允许的时间来源。"""
    manual_clock.set(datetime(2024, 3, 1, 6, 30, tzinfo=timezone.utc))
    target = _write_rules(tmp_path / "rules.yaml", _ruleset_data([]))
    loader = _make_loader(target, clock=manual_clock)
    assert loader.load_initial() is True
    assert as_utc(loader.loaded_at) == manual_clock.now()


# --------------------------------------------------------------------------- #
# 2. 与语义 / 3. 或语义（tasks 4.2）
# --------------------------------------------------------------------------- #
_WEB_ERROR_RULE: dict[str, Any] = {
    "id": "web-error",
    "match": {"source": ["web-01", "web-02"], "level": ["error"]},
    "category": "web-failure",
    "labels": ["web"],
    "need_ack": True,
    "channel": "email",
}
_DB_ERROR_RULE: dict[str, Any] = {
    "id": "db-error",
    "match": {"source": ["db-01"], "level": ["error"]},
    "category": "db-failure",
    "labels": ["db"],
    "need_ack": False,
    "channel": None,
}


def test_all_conditions_must_match_then_next_rule_is_tried(
    tmp_path: Path, manual_clock: ManualClock
) -> None:
    target = _write_rules(
        tmp_path / "rules.yaml", _ruleset_data([_WEB_ERROR_RULE, _DB_ERROR_RULE])
    )
    classifier = _make_classifier(_make_loader(target, clock=manual_clock))

    verdict = classifier.classify(source="db-01", level=Level.ERROR, title="t")

    assert verdict.rule_id == "db-error"
    assert verdict.category == "db-failure"


def test_all_conditions_must_match_without_followup_rule(
    tmp_path: Path, manual_clock: ManualClock
) -> None:
    target = _write_rules(tmp_path / "rules.yaml", _ruleset_data([_WEB_ERROR_RULE]))
    classifier = _make_classifier(_make_loader(target, clock=manual_clock))

    verdict = classifier.classify(source="db-01", level=Level.ERROR, title="t")

    assert verdict.rule_id is None


def test_candidate_values_match_with_or(tmp_path: Path, manual_clock: ManualClock) -> None:
    target = _write_rules(tmp_path / "rules.yaml", _ruleset_data([_WEB_ERROR_RULE]))
    classifier = _make_classifier(_make_loader(target, clock=manual_clock))

    first = classifier.classify(source="web-01", level=Level.ERROR, title="t")
    second = classifier.classify(source="web-02", level=Level.ERROR, title="t")

    assert first.rule_id == "web-error"
    assert second.rule_id == "web-error"


# --------------------------------------------------------------------------- #
# 4. first-match-wins（tasks 4.2）
# --------------------------------------------------------------------------- #
_OVERLAP_FIRST: dict[str, Any] = {
    "id": "rule-first",
    "match": {"source": ["dup"]},
    "category": "first-cat",
    "labels": ["a"],
    "need_ack": False,
    "channel": None,
}
_OVERLAP_SECOND: dict[str, Any] = {
    "id": "rule-second",
    "match": {"source": ["dup"], "level": ["error"]},
    "category": "second-cat",
    "labels": ["b"],
    "need_ack": True,
    "channel": "email",
}


def test_first_match_wins_and_follows_declaration_order(
    tmp_path: Path, manual_clock: ManualClock
) -> None:
    target = tmp_path / "rules.yaml"
    _write_rules(
        target, _ruleset_data([_OVERLAP_FIRST, _OVERLAP_SECOND])
    )
    loader = _make_loader(target, clock=manual_clock)
    classifier = _make_classifier(loader)

    assert _classify(classifier, source="dup", level=Level.ERROR).rule_id == "rule-first"

    _write_rules(target, _ruleset_data([_OVERLAP_SECOND, _OVERLAP_FIRST]))
    assert loader.reload() is True

    assert _classify(classifier, source="dup", level=Level.ERROR).rule_id == "rule-second"


# --------------------------------------------------------------------------- #
# 5. 关键词大小写策略（tasks 4.2）
# --------------------------------------------------------------------------- #
def _keyword_rules(case_sensitive: bool) -> dict[str, Any]:
    return _ruleset_data(
        [
            {
                "id": "keyword-failure",
                "match": {"title_contains": ["失败", "failed"]},
                "category": "backup-failure",
                "labels": ["infra"],
                "need_ack": True,
                "channel": "email",
            }
        ],
        case_sensitive=case_sensitive,
    )


def test_keyword_case_policy_is_explicit(tmp_path: Path, manual_clock: ManualClock) -> None:
    insensitive_path = _write_rules(tmp_path / "insensitive.yaml", _keyword_rules(False))
    sensitive_path = _write_rules(tmp_path / "sensitive.yaml", _keyword_rules(True))
    insensitive = _make_classifier(_make_loader(insensitive_path, clock=manual_clock))
    sensitive = _make_classifier(_make_loader(sensitive_path, clock=manual_clock))

    # case_sensitive=false：两侧 casefold，「失败」精确命中、「FAILED」命中「failed」
    assert _classify(insensitive, title="服务失败").rule_id == "keyword-failure"
    assert _classify(insensitive, title="BACKUP FAILED").rule_id == "keyword-failure"
    # case_sensitive=true：「失败」仍精确命中，但「FAILED」不再命中「failed」
    assert _classify(sensitive, title="服务失败").rule_id == "keyword-failure"
    assert _classify(sensitive, title="BACKUP FAILED").rule_id is None


def test_level_equals_compares_against_level_value(
    tmp_path: Path, manual_clock: ManualClock
) -> None:
    rules = _ruleset_data(
        [{"id": "err", "match": {"level": ["error"]}, "category": "err", "need_ack": False}]
    )
    classifier = _make_classifier(
        _make_loader(_write_rules(tmp_path / "rules.yaml", rules), clock=manual_clock)
    )
    # equals 与 Level.value 比较；大小写策略同样作用于它
    assert _classify(classifier, level=Level.ERROR).rule_id == "err"
    assert _classify(classifier, level=Level.WARNING).rule_id is None

    upper = _ruleset_data(
        [{"id": "err", "match": {"level": ["ERROR"]}, "category": "err", "need_ack": False}],
        case_sensitive=False,
    )
    insensitive = _make_classifier(
        _make_loader(_write_rules(tmp_path / "upper.yaml", upper), clock=manual_clock)
    )
    assert _classify(insensitive, level=Level.ERROR).rule_id == "err"


# --------------------------------------------------------------------------- #
# match 键 → MatchCondition 映射（第 2 段 YAML 形状）
# --------------------------------------------------------------------------- #
def test_match_keys_map_to_conditions(tmp_path: Path, manual_clock: ManualClock) -> None:
    from notify_hub.classifier.rules import MatchCondition

    data = _ruleset_data(
        [
            {
                "id": "all-keys",
                "match": {
                    "source": ["db-backup", "file-backup"],
                    "level": ["error"],
                    "title": ["exact title"],
                    "body": ["exact body"],
                    "title_contains": ["失败", "failed"],
                    "body_contains": ["exit code"],
                },
                "category": "c",
                "need_ack": True,
            }
        ]
    )
    ruleset = _parse(data, path=tmp_path / "rules.yaml", clock=manual_clock)
    match = ruleset.rules[0].match

    assert MatchCondition(field="source", mode="equals", values=("db-backup", "file-backup")) in match
    assert MatchCondition(field="level", mode="equals", values=("error",)) in match
    assert MatchCondition(field="title", mode="equals", values=("exact title",)) in match
    assert MatchCondition(field="body", mode="equals", values=("exact body",)) in match
    assert MatchCondition(field="title_contains", mode="contains", values=("失败", "failed")) in match
    assert MatchCondition(field="body_contains", mode="contains", values=("exit code",)) in match


def test_scalar_match_values_are_treated_as_single_element_list(
    tmp_path: Path, manual_clock: ManualClock
) -> None:
    from notify_hub.classifier.rules import MatchCondition

    data = _ruleset_data(
        [
            {
                "id": "scalar",
                "match": {"source": "db-01", "body_contains": "exit code"},
                "category": "scalar-cat",
                "need_ack": False,
            }
        ]
    )
    ruleset = _parse(data, path=tmp_path / "rules.yaml", clock=manual_clock)
    match = ruleset.rules[0].match

    assert MatchCondition(field="source", mode="equals", values=("db-01",)) in match
    assert MatchCondition(field="body_contains", mode="contains", values=("exit code",)) in match


def test_empty_match_matches_everything(tmp_path: Path, manual_clock: ManualClock) -> None:
    data = _ruleset_data(
        [{"id": "catch-all", "match": {}, "category": "everything", "need_ack": False}]
    )
    classifier = _make_classifier(
        _make_loader(_write_rules(tmp_path / "rules.yaml", data), clock=manual_clock)
    )
    assert _classify(classifier, source="anything", level=Level.WARNING, title="x").rule_id == "catch-all"
    assert _classify(classifier, source="other", level=Level.INFO, title="y").rule_id == "catch-all"


def test_optional_rule_fields_default_to_empty_and_none(
    tmp_path: Path, manual_clock: ManualClock
) -> None:
    data = _ruleset_data(
        [{"id": "minimal", "match": {"source": ["svc"]}, "category": "minimal-cat"}]
    )
    classifier = _make_classifier(
        _make_loader(_write_rules(tmp_path / "rules.yaml", data), clock=manual_clock)
    )
    verdict = _classify(classifier, source="svc")

    assert verdict.labels == ()
    assert verdict.need_ack is False
    assert verdict.ack_reason is AckReason.NONE
    assert verdict.preferred_channel is None


def test_body_contains_with_none_body_does_not_match_or_raise(
    tmp_path: Path, manual_clock: ManualClock
) -> None:
    data = _ruleset_data(
        [
            {
                "id": "body-rule",
                "match": {"body_contains": ["exit code"]},
                "category": "body-cat",
                "need_ack": True,
            }
        ]
    )
    classifier = _make_classifier(
        _make_loader(_write_rules(tmp_path / "rules.yaml", data), clock=manual_clock)
    )
    verdict = _classify(classifier, source="svc", body=None)

    assert verdict.rule_id is None


def test_rule_engine_is_usable_directly(tmp_path: Path, manual_clock: ManualClock) -> None:
    data = _ruleset_data([_WEB_ERROR_RULE])
    ruleset = _parse(data, path=tmp_path / "rules.yaml", clock=manual_clock)
    engine = _make_engine(ruleset)

    verdict = engine.classify(
        source="web-01",
        level=Level.ERROR,
        title="t",
        body=None,
        declared_need_ack=False,
    )

    assert verdict.rule_id == "web-error"
    assert verdict.category == "web-failure"
    assert verdict.preferred_channel == "email"


# --------------------------------------------------------------------------- #
# 6. 兜底（tasks 4.4）
# --------------------------------------------------------------------------- #
def test_unmatched_message_falls_back_to_defaults(
    tmp_path: Path, manual_clock: ManualClock
) -> None:
    defaults = {"category": "misc", "labels": ["other"], "need_ack": False, "channel": "webhook"}
    data = _ruleset_data([_WEB_ERROR_RULE], defaults=defaults)
    classifier = _make_classifier(
        _make_loader(_write_rules(tmp_path / "rules.yaml", data), clock=manual_clock)
    )

    verdict = classifier.classify(source="unknown-svc", level=Level.INFO, title="hello")

    assert verdict.rule_id is None
    assert verdict.category == "misc"
    assert verdict.labels == ("other",)
    assert verdict.need_ack is False
    assert verdict.ack_reason is AckReason.NONE
    assert verdict.preferred_channel == "webhook"


# --------------------------------------------------------------------------- #
# 7. 入待办判定三分支（tasks 4.3）
# --------------------------------------------------------------------------- #
_RULE_NO_ACK: dict[str, Any] = {
    "id": "rule-no-ack",
    "match": {"source": ["svc"]},
    "category": "notice",
    "labels": [],
    "need_ack": False,
    "channel": "webhook",
}
_RULE_ACK: dict[str, Any] = {
    "id": "rule-ack",
    "match": {"source": ["svc"]},
    "category": "actionable",
    "labels": ["urgent"],
    "need_ack": True,
    "channel": "email",
}


def _branch_classifier(tmp_path: Path, manual_clock: ManualClock):
    data = _ruleset_data([_RULE_NO_ACK, _RULE_ACK])
    return _make_classifier(
        _make_loader(_write_rules(tmp_path / "rules.yaml", data), clock=manual_clock)
    )


def test_declared_ack_wins_over_rule_without_ack(
    tmp_path: Path, manual_clock: ManualClock
) -> None:
    classifier = _branch_classifier(tmp_path, manual_clock)
    verdict = _classify(classifier, source="svc", declared_need_ack=True)

    assert verdict.rule_id == "rule-no-ack"
    assert verdict.need_ack is True
    assert verdict.ack_reason is AckReason.CALLER_DECLARED


def test_rule_ack_when_caller_declares_nothing(tmp_path: Path, manual_clock: ManualClock) -> None:
    data = _ruleset_data([_RULE_ACK])
    classifier = _make_classifier(
        _make_loader(_write_rules(tmp_path / "rules.yaml", data), clock=manual_clock)
    )
    verdict = _classify(classifier, source="svc", declared_need_ack=False)

    assert verdict.rule_id == "rule-ack"
    assert verdict.need_ack is True
    assert verdict.ack_reason is AckReason.RULE


def test_pure_notification_stays_out_of_todo(tmp_path: Path, manual_clock: ManualClock) -> None:
    classifier = _branch_classifier(tmp_path, manual_clock)
    verdict = _classify(classifier, source="svc", declared_need_ack=False)

    assert verdict.rule_id == "rule-no-ack"
    assert verdict.need_ack is False
    assert verdict.ack_reason is AckReason.NONE


def test_declared_ack_without_any_matching_rule(tmp_path: Path, manual_clock: ManualClock) -> None:
    data = _ruleset_data([_WEB_ERROR_RULE])
    classifier = _make_classifier(
        _make_loader(_write_rules(tmp_path / "rules.yaml", data), clock=manual_clock)
    )
    verdict = _classify(classifier, source="unmatched", declared_need_ack=True)

    assert verdict.rule_id is None
    assert verdict.need_ack is True
    assert verdict.ack_reason is AckReason.CALLER_DECLARED


# --------------------------------------------------------------------------- #
# 8. 热加载（tasks 4.5）
# --------------------------------------------------------------------------- #
_V1_RULES = _ruleset_data(
    [{"id": "v1-rule", "match": {"source": ["v1"]}, "category": "v1-cat", "need_ack": False}]
)
_V2_RULES = _ruleset_data(
    [
        {"id": "v1-rule", "match": {"source": ["v1"]}, "category": "v1-cat", "need_ack": False},
        {"id": "v2-rule", "match": {"source": ["v2"]}, "category": "v2-cat", "need_ack": True},
    ]
)


def test_mtime_driven_hot_reload(tmp_path: Path, manual_clock: ManualClock) -> None:
    target = _write_rules(tmp_path / "rules.yaml", _V1_RULES)
    loader = _make_loader(target, clock=manual_clock)
    classifier = _make_classifier(loader)

    assert _classify(classifier, source="v1").rule_id == "v1-rule"

    # mtime 未变 → 不重载
    assert loader.poll_once() is False
    assert loader.last_error is None

    # 写入 v2 并显式前移 mtime（不使用真实 sleep 等待时间戳变化）
    _write_rules(target, _V2_RULES)
    _touch_forward(target)
    manual_clock.advance(10.0)

    assert loader.poll_once() is True
    assert classifier.ruleset.rules == loader.ruleset.rules
    assert _classify(classifier, source="v1").rule_id == "v1-rule"
    assert _classify(classifier, source="v2").rule_id == "v2-rule"


def test_explicit_reload_picks_up_new_rules(tmp_path: Path, manual_clock: ManualClock) -> None:
    target = _write_rules(tmp_path / "rules.yaml", _V1_RULES)
    loader = _make_loader(target, clock=manual_clock)
    classifier = _make_classifier(loader)

    _write_rules(target, _V2_RULES)
    assert loader.reload() is True

    assert _classify(classifier, source="v2").rule_id == "v2-rule"


# --------------------------------------------------------------------------- #
# 9. 坏配置降级（tasks 4.5）
# --------------------------------------------------------------------------- #
def test_reload_bad_yaml_keeps_previous_ruleset(
    tmp_path: Path, manual_clock: ManualClock
) -> None:
    target = _write_rules(tmp_path / "rules.yaml", _V1_RULES)
    loader = _make_loader(target, clock=manual_clock)
    classifier = _make_classifier(loader)
    previous = loader.ruleset

    _write_invalid_yaml(target)
    _touch_forward(target)

    assert loader.reload() is False
    assert loader.ruleset == previous
    assert loader.last_error is not None
    assert str(target) in loader.last_error
    assert classifier.last_error is not None

    # 服务不可因坏配置而不可用：分类仍返回合法结论
    verdict = _classify(classifier, source="v1")
    assert verdict.rule_id == "v1-rule"
    assert verdict.category == "v1-cat"
    assert verdict.need_ack is False


def test_poll_once_with_bad_yaml_does_not_raise(
    tmp_path: Path, manual_clock: ManualClock
) -> None:
    target = _write_rules(tmp_path / "rules.yaml", _V1_RULES)
    loader = _make_loader(target, clock=manual_clock)
    assert loader.load_initial() is True

    _write_invalid_yaml(target)
    _touch_forward(target)

    assert loader.poll_once() is False
    assert loader.last_error is not None


def test_bad_config_logs_locatable_warning(
    tmp_path: Path, manual_clock: ManualClock, caplog: pytest.LogCaptureFixture
) -> None:
    target = _write_rules(tmp_path / "rules.yaml", _V1_RULES)
    logger = logging.getLogger("notify_hub.classifier.test")
    loader = _make_loader(target, clock=manual_clock, logger=logger)
    assert loader.load_initial() is True

    _write_invalid_yaml(target)
    with caplog.at_level(logging.WARNING, logger="notify_hub.classifier.test"):
        assert loader.reload() is False

    warnings = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert any("规则文件解析失败" in message and target.name in message for message in warnings), warnings


# --------------------------------------------------------------------------- #
# 10. 启动时坏配置
# --------------------------------------------------------------------------- #
def test_load_initial_bad_yaml_falls_back_to_default_ruleset(
    tmp_path: Path, manual_clock: ManualClock
) -> None:
    rules_module = _load_rules_module()
    target = _write_invalid_yaml(tmp_path / "rules.yaml")
    loader = _make_loader(target, clock=manual_clock)

    assert loader.load_initial() is False
    assert loader.ruleset == rules_module.DEFAULT_RULESET
    assert loader.last_error is not None
    assert str(target) in loader.last_error

    classifier = _make_classifier(loader)
    assert classifier.last_error is not None

    declared = _classify(classifier, source="s", level=Level.ERROR, declared_need_ack=True)
    assert declared.rule_id is None
    assert declared.category == "uncategorized"
    assert declared.need_ack is True
    assert declared.ack_reason is AckReason.CALLER_DECLARED

    undeclared = _classify(classifier, source="s", level=Level.ERROR, declared_need_ack=False)
    assert undeclared.category == "uncategorized"
    assert undeclared.need_ack is False
    assert undeclared.ack_reason is AckReason.NONE


def test_missing_rules_file_does_not_break_startup(
    tmp_path: Path, manual_clock: ManualClock
) -> None:
    rules_module = _load_rules_module()
    loader = _make_loader(tmp_path / "does-not-exist.yaml", clock=manual_clock)

    assert loader.load_initial() is False
    assert loader.ruleset == rules_module.DEFAULT_RULESET
    assert loader.last_error is not None


# --------------------------------------------------------------------------- #
# 11. 线程生命周期
# --------------------------------------------------------------------------- #
def test_classifier_start_stop_thread_lifecycle(tmp_path: Path, manual_clock: ManualClock) -> None:
    target = _write_rules(tmp_path / "rules.yaml", _V2_RULES)
    loader = _make_loader(target, clock=manual_clock, poll_interval=30.0)
    classifier = _make_classifier(loader, logger=logging.getLogger("notify_hub.classifier.thread"))

    baseline = {thread.ident for thread in threading.enumerate()}

    classifier.start()
    assert _wait_until(lambda: bool(_extra_threads(baseline))), "start() 未启动后台线程"
    spawned = _extra_threads(baseline)
    assert all(thread.daemon for thread in spawned), "热加载线程必须是守护线程"

    started = time.monotonic()
    classifier.stop()
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"stop() 未在 5 秒内返回（耗时 {elapsed:.2f}s）"

    assert _wait_until(lambda: not _extra_threads(baseline)), "stop() 后线程仍然存活"

    classifier.stop()  # 重复 stop() 不得抛异常


# --------------------------------------------------------------------------- #
# 异常场景：parse_ruleset 的 RuleFileError
# --------------------------------------------------------------------------- #
def test_rule_without_category_raises_rule_file_error(
    tmp_path: Path, manual_clock: ManualClock
) -> None:
    data = _ruleset_data([{"id": "broken-rule", "match": {"source": ["s"]}, "need_ack": True}])
    path = tmp_path / "rules.yaml"

    with pytest.raises(RuleFileError) as excinfo:
        _parse(data, path=path, clock=manual_clock)

    message = str(excinfo.value)
    assert "broken-rule" in message
    assert "category" in message
    assert path.name in message


def test_unsupported_match_field_raises_rule_file_error(
    tmp_path: Path, manual_clock: ManualClock
) -> None:
    data = _ruleset_data(
        [{"id": "bad-match", "match": {"foo": ["bar"]}, "category": "c", "need_ack": False}]
    )
    path = tmp_path / "rules.yaml"

    with pytest.raises(RuleFileError) as excinfo:
        _parse(data, path=path, clock=manual_clock)

    message = str(excinfo.value)
    assert "foo" in message
    assert path.name in message
