"""M3 规则模型与解析。

规格：``openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md`` 第 6 节「模块 M3」。
本文件只依赖标准库（YAML 的解析由调用方完成），**不**导入其它模块。

依赖方向：``rules.py`` <- ``engine.py`` / ``loader.py``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from notify_hub.errors import RuleFileError

__all__ = [
    "MatchCondition",
    "Rule",
    "RuleDefaults",
    "RuleSet",
    "DEFAULT_RULESET",
    "parse_ruleset",
]

#: ``match`` 的键 -> 匹配模式（冻结，见 architecture.md M3 第 2 节）。
_FIELD_MODES: Mapping[str, str] = {
    "source": "equals",
    "level": "equals",
    "title": "equals",
    "body": "equals",
    "title_contains": "contains",
    "body_contains": "contains",
}


@dataclass(frozen=True)
class MatchCondition:
    """单条匹配条件：``field`` 上的取值需落在 ``values`` 之一（或关系）。"""

    field: str
    mode: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class Rule:
    """一条分类规则。``match`` 为空元组表示匹配一切。"""

    id: str
    match: tuple[MatchCondition, ...]
    category: str
    labels: tuple[str, ...] = ()
    need_ack: bool = False
    channel: str | None = None


@dataclass(frozen=True)
class RuleDefaults:
    """未命中任何规则时套用的默认处置动作。"""

    category: str = "uncategorized"
    labels: tuple[str, ...] = ()
    need_ack: bool = False
    channel: str | None = None


@dataclass(frozen=True)
class RuleSet:
    """一份完整、自洽的规则集。``loaded_at`` 必须来自注入的 clock。"""

    defaults: RuleDefaults
    rules: tuple[Rule, ...]
    case_sensitive: bool
    source_path: Path
    loaded_at: datetime


#: 内置兜底规则集：任何情况下 ``RuleLoader.ruleset`` 都可用。
DEFAULT_RULESET = RuleSet(
    defaults=RuleDefaults(),
    rules=(),
    case_sensitive=False,
    source_path=Path("<builtin>"),
    loaded_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
)


def _as_str_tuple(raw: Any) -> tuple[str, ...]:
    """标量按单元素列表处理；其余可迭代值逐个转字符串。"""
    if raw is None:
        return ()
    if isinstance(raw, (str, bytes)):
        return (raw.decode() if isinstance(raw, bytes) else raw,)
    if isinstance(raw, (list, tuple, set, frozenset)):
        return tuple(str(item) for item in raw)
    return (str(raw),)


def _as_optional_str(raw: Any) -> str | None:
    if raw is None or raw == "":
        return None
    return str(raw)


def _fail(source_path: Path, detail: str) -> RuleFileError:
    return RuleFileError(f"{source_path.name}: {detail}")


def _parse_match(
    raw_match: Any, *, index: int, rule_id: str, source_path: Path
) -> tuple[MatchCondition, ...]:
    if raw_match is None:
        return ()
    if not isinstance(raw_match, Mapping):
        raise _fail(
            source_path,
            f"规则 #{index} (id={rule_id}) 的 match 必须是映射，实际是 {type(raw_match).__name__}",
        )

    conditions: list[MatchCondition] = []
    for raw_field, raw_values in raw_match.items():
        field = str(raw_field)
        mode = _FIELD_MODES.get(field)
        if mode is None:
            raise _fail(
                source_path,
                f"规则 #{index} (id={rule_id}) 的 match 含不支持的字段 {field!r}；"
                f"支持的字段：{', '.join(_FIELD_MODES)}",
            )
        conditions.append(
            MatchCondition(field=field, mode=mode, values=_as_str_tuple(raw_values))
        )
    return tuple(conditions)


def _parse_rule(raw: Any, *, index: int, source_path: Path) -> Rule:
    if not isinstance(raw, Mapping):
        raise _fail(
            source_path,
            f"规则 #{index} 必须是映射，实际是 {type(raw).__name__}",
        )

    if "id" not in raw or raw["id"] in (None, ""):
        raise _fail(source_path, f"规则 #{index} 缺少必填键 'id'")
    rule_id = str(raw["id"])

    if "category" not in raw or raw["category"] in (None, ""):
        raise _fail(
            source_path,
            f"规则 #{index} (id={rule_id}) 缺少必填键 'category'",
        )

    return Rule(
        id=rule_id,
        match=_parse_match(raw.get("match"), index=index, rule_id=rule_id, source_path=source_path),
        category=str(raw["category"]),
        labels=_as_str_tuple(raw.get("labels")),
        need_ack=bool(raw.get("need_ack", False)),
        channel=_as_optional_str(raw.get("channel")),
    )


def parse_ruleset(
    data: Mapping[str, Any], *, source_path: Path, loaded_at: datetime
) -> RuleSet:
    """把已解析的 YAML 映射转成 ``RuleSet``。

    抛 ``RuleFileError``：消息定位到文件名 + 规则下标/id + 问题描述。
    """
    source_path = Path(source_path)
    if not isinstance(data, Mapping):
        raise _fail(
            source_path,
            f"规则文件顶层必须是映射，实际是 {type(data).__name__}",
        )

    raw_defaults = data.get("defaults") or {}
    if not isinstance(raw_defaults, Mapping):
        raise _fail(source_path, "defaults 必须是映射")
    defaults = RuleDefaults(
        category=str(raw_defaults.get("category", RuleDefaults.category)),
        labels=_as_str_tuple(raw_defaults.get("labels")),
        need_ack=bool(raw_defaults.get("need_ack", False)),
        channel=_as_optional_str(raw_defaults.get("channel")),
    )

    raw_rules = data.get("rules") or []
    if not isinstance(raw_rules, (list, tuple)):
        raise _fail(source_path, "rules 必须是列表")

    rules = tuple(
        _parse_rule(raw, index=index, source_path=source_path)
        for index, raw in enumerate(raw_rules)
    )

    return RuleSet(
        defaults=defaults,
        rules=rules,
        case_sensitive=bool(data.get("case_sensitive", False)),
        source_path=source_path,
        loaded_at=loaded_at,
    )
