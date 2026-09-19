"""M9「文档与部署」的**文档契约测试**（作用域 1：实现尚不存在时编写）。

规格：``openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md`` 第 6 节「模块 M9」第 2/4 段，
以及 ``tasks.md`` 10.1–10.4。

这个模块的交付物是 Markdown，不是代码，所以唯一有判别力的验证方式是：**把文档里的
代码块当成可执行断言**——文档与真实代码脱节、或文档撒谎，测试就必须红。

覆盖规格第 4 段的 7 条：

1. ``test_rule_example_block_loads_and_classifies`` —— ``docs/rule-authoring.md`` 中
   标记为 ```` ```yaml rules-example ```` 的代码块能被 M3 的 ``RuleLoader`` 真加载，
   且示例消息经 ``RuleClassifier`` 得到与文档所描述规则一致的 ``category``。
2. ``test_configuration_doc_covers_every_config_key`` —— ``config.example.yaml`` 的
   顶层键（含嵌套键）与 ``docs/configuration.md`` 的小节标题双向对齐。
3. ``test_dummy_adapter_block_is_a_working_notifier`` —— ``docs/adapter-guide.md`` 中
   标记为 ```` ```python dummy-adapter ```` 的代码块 ``exec`` 后满足 ``Notifier``
   协议、``send()`` 返回 ``DeliveryResult``、可 ``register()`` 进 ``NotifierRegistry``。
4. ``test_readme_cli_options_are_registered`` —— README 里 ``notify ...`` 行出现的
   每个长选项名都必须在 ``notify_hub.cli.app`` 的已注册参数名/``--help`` 输出里。
5. ``test_deployment_doc_states_loopback_and_systemd_boundary`` —— 部署文档的回环
   地址强制边界、systemd 段与 ``ExecStart``。
6. ``test_no_document_leaks_credentials`` —— 五份文档全文不出现「键名=长随机串」形态的凭据。
7. ``test_readme_doc_links_resolve`` —— README 中每个 ``docs/*.md`` 相对链接都可达。

**另有第 8 条（M10 增量，非规格第 4 段原文）**：
``test_every_registered_channel_type_is_documented`` —— 「每一个已在
``NOTIFIER_FACTORIES`` 注册的渠道类型都必须在 ``docs/configuration.md`` 中被记载」。
M10 新增 ``feishu`` 注册项时，配置文档一个字都没提飞书，属于「发布了渠道却没有文档覆盖」
的真实缺口；本条把这类缺口变成 CI 红灯，且**只判「类型名是否出现在文档全文」**，
不要求章节标题或句式，以免过度约束文档写法。

**红是预期结果**：``README.md`` 与 ``docs/`` 下的四份文档当前都不存在（它们是 M9 的
实现路径，由开发子代理负责）。因此提取逻辑在文件缺失时给出的是**带期望路径的、可读的
失败信息**，而不是含糊的 ``FileNotFoundError`` 栈——这样「红」的原因可以被一眼判定为
「文档缺失」而非「测试文件自身写错」。

**本文件只读地引用 M1/M3/M4 的产物**，不创建/修改 ``src/`` 下任何文件，不修改
``tests/conftest.py``、``config.example.yaml``、``rules.example.yaml``、``pyproject.toml``
或 ``openspec/`` 下任何文件。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

# --------------------------------------------------------------------------- #
# 常量：文档契约的冻结部分
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent

#: M9 的实现路径（architecture.md 第 6 节「模块 M9 · 文件边界」）。
README_PATH = REPO_ROOT / "README.md"
RULE_DOC_PATH = REPO_ROOT / "docs" / "rule-authoring.md"
CONFIG_DOC_PATH = REPO_ROOT / "docs" / "configuration.md"
ADAPTER_DOC_PATH = REPO_ROOT / "docs" / "adapter-guide.md"
DEPLOYMENT_DOC_PATH = REPO_ROOT / "docs" / "deployment.md"
ALL_DOC_PATHS = (README_PATH, CONFIG_DOC_PATH, RULE_DOC_PATH, ADAPTER_DOC_PATH, DEPLOYMENT_DOC_PATH)

#: 只读引用的 M1/M3 产物（不得修改）。
CONFIG_EXAMPLE_PATH = REPO_ROOT / "config.example.yaml"
RULES_EXAMPLE_PATH = REPO_ROOT / "rules.example.yaml"

#: 规格第 4 段第 4 条列举的长选项名。前四项是「至少一个可复制 CLI 示例」的最小充分集，
#: 后四项中 ``--endpoint`` 由同一段文字点名；其余为出现即校验。
REQUIRED_CLI_OPTIONS = (
    "--title",
    "--body",
    "--level",
    "--source",
    "--body-stdin",
    "--need-ack",
    "--dedup-key",
    "--endpoint",
)
MINIMAL_CLI_OPTIONS = ("--title", "--body", "--level", "--source", "--endpoint")

#: 规格第 4 段第 6 条冻结的凭据正则。**不做任何「加工」后再匹配**：先按原样扫描，
#: 再把命中行里的占位符形态（``https://`` 里的主机名、``<...>`` 角括号、``your-token-here``
#: 这类连字符占位词）剔除；任何剩余命中都是真失败。
CREDENTIAL_PATTERN = re.compile(r"(?i)(token|password|secret)\s*[:=]\s*[A-Za-z0-9_\-]{12,}")

#: 占位词：命中值只由这些词与数字/连字符组成时，视为文档占位符而非真实凭据。
_PLACEHOLDER_WORDS = {
    "your",
    "yours",
    "here",
    "example",
    "examples",
    "sample",
    "placeholder",
    "changeme",
    "change",
    "me",
    "redacted",
    "fake",
    "dummy",
    "test",
    "todo",
    "xxx",
    "xxxx",
    "xxxxx",
    "token",
    "password",
    "passwd",
    "secret",
    "env",
    "var",
    "value",
}

#: ```` ```yaml ```` 围栏的信息串标记约定（规格第 4 段第 1/3 条冻结）。
_RULE_BLOCK_MARKER = "rules-example"
_ADAPTER_BLOCK_MARKER = "dummy-adapter"

#: 剔除正文中「说明文字里其它代码块」用的围栏正则（``_prose_only`` 使用）。
_FENCE_FOR_STRIP = re.compile(
    r"^(?:`{3,}|~{3,})[^\n]*\n.*?^(?:`{3,}|~{3,})[ \t]*$", re.MULTILINE | re.DOTALL
)

#: 「正文在谈某个 category 的取值」的上下文形态（YAML 用 ``category:``，中文说明用
#: 「category 为/是/等于 backxx」）。比「这个词出现在文档任意位置」精确得多，
#: 也不会把规则 id / source 取值误判成 category。
_CATEGORY_CONTEXT = re.compile(
    r"(?:category|分类|类别)\s*[`\"'）)]*\s*(?:[:：=]|为|是|等于|should be)\s*[`\"'（(]*"
    r"([A-Za-z0-9_.\-]+)",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# 读取与提取工具（文件缺失时给出可读、带期望路径的失败信息）
# --------------------------------------------------------------------------- #
class MissingDocument(AssertionError):
    """文档缺失/为空：失败信息必须包含期望路径，便于一眼判定「红」的原因。"""


def _read_text(path: Path, *, what: str) -> str:
    """读取文档；缺失或为空时以断言失败的形式报告期望路径。"""
    if not path.exists():
        raise MissingDocument(
            f"{what}缺失：期望文件 {path}。该文件是 M9 的实现路径"
            f"（openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md 第 6 节「模块 M9」），"
            f"在开发子代理写出它之前，本条契约无法被满足——这是预期的红，不是测试写错。"
        )
    if not path.is_file():
        raise MissingDocument(f"{what}不是普通文件：{path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise MissingDocument(f"{what}存在但为空：{path}（规格要求文档非空）")
    return text


def _extract_marked_block(text: str, *, marker: str, path: Path) -> str:
    """提取信息串中含 ``marker`` 的围栏代码块（标记可与语言名共存、顺序不限）。

    同时接受三反引号与 ``~~~`` 围栏，避免把文档作者的三反引号选择变成失败原因。
    """
    pattern = re.compile(
        r"^(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>[^\n]*)$\n"
        r"(?P<body>.*?)"
        r"^(?P=fence)[ \t]*$",
        re.MULTILINE | re.DOTALL,
    )
    for match in pattern.finditer(text):
        info = match.group("info")
        if marker in info.split():
            return match.group("body")
    raise AssertionError(
        f"在 {path} 中找不到标记为 '{marker}' 的围栏代码块。"
        f"规格冻结的标记约定：信息串必须含 '{marker}'，"
        f"例如 '```yaml {marker}' 或 '```python {marker}'。"
        f"文档中出现的围栏信息串：{[m.group('info') for m in pattern.finditer(text)]}"
    )


def _extract_marked_python_block(text: str, *, marker: str, path: Path) -> str:
    block = _extract_marked_block(text, marker=marker, path=path)
    if not block.strip():
        raise AssertionError(f"{path} 中标记为 '{marker}' 的代码块是空的")
    return block


def _shell_command_lines(text: str) -> list[str]:
    """提取文档中的「可复制命令」行。

    接受裸 ``notify ...`` 与 ``$ notify ...``（README 的可复制示例两种写法都常见）。
    """
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        stripped = re.sub(r"^(?:\$|>|PS>)\s+", "", line)
        if re.match(r"^notify\s", stripped):
            lines.append(stripped)
    return lines


def _long_options_in(line: str) -> set[str]:
    """一行命令里出现的长选项名（``--x`` 与 ``--x=value`` 都识别）。"""
    return {opt.split("=", 1)[0] for opt in re.findall(r"--[A-Za-z][A-Za-z0-9-]*", line)}


def _placeholder_hit(hit: str) -> bool:
    """判断正则命中是否为文档占位符（而非真实凭据）。

    规则收紧但明确：
      - 命中值两侧是 ``<``/``{``/``%``（``<token>``、``{TOKEN}``、``%TOKEN%``）→ 占位符；
      - 命中值含 ``_`` 或纯数字 → 像环境变量名/编号，不是凭据；
      - 命中值按 ``[-_.]`` 切分后只由已知占位词与数字组成（``your-token-here``）→ 占位符。
    """
    brackets = (
        (hit.startswith("<") and hit.endswith(">"))
        or (hit.startswith("{") and hit.endswith("}"))
        or (hit.startswith("%") and hit.endswith("%"))
    )
    if brackets:
        return True

    value = hit.split("=", 1)[1] if "=" in hit else hit.split(":", 1)[1]
    value = value.strip()
    if value.startswith("//"):  # https://<token> 形态：URL 主机名，不是凭据
        return True
    if "_" in value or value.isdigit():
        return True
    parts = [p for p in re.split(r"[-_.]", value) if p]
    return bool(parts) and all(p.lower() in _PLACEHOLDER_WORDS or p.isdigit() for p in parts)


def _english_words(text: str, min_length: int = 3) -> set[str]:
    """正文里的英文词（用于「键名必须在正文出现」这类断言）。"""
    return {m.group(0).lower() for m in re.finditer(r"[A-Za-z]{%d,}" % min_length, text)}


def _prose_only(doc: str) -> str:
    """去掉所有围栏代码块后的正文（用于检查「文档文字怎么描述示例」）。"""
    return _FENCE_FOR_STRIP.sub("\n", doc)


# --------------------------------------------------------------------------- #
# 第 7 条：五份文档存在且非空（其余各条的前置条件）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ALL_DOC_PATHS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_document_exists_and_is_not_empty(path: Path) -> None:
    _read_text(path, what="M9 文档")


# --------------------------------------------------------------------------- #
# 第 1 条（tasks 10.2）：规则示例可被真加载并分类
# --------------------------------------------------------------------------- #
def test_rule_example_block_loads_and_classifies(tmp_path: Path) -> None:
    from notify_hub.classifier import RuleClassifier
    from notify_hub.classifier.loader import RuleLoader
    from notify_hub.domain import Level

    doc = _read_text(RULE_DOC_PATH, what="规则编写指南")
    block = _extract_marked_block(doc, marker=_RULE_BLOCK_MARKER, path=RULE_DOC_PATH)

    target = tmp_path / "rules.yaml"
    target.write_text(block, encoding="utf-8")

    loader = RuleLoader(target)
    assert loader.load_initial() is True, (
        f"从 {RULE_DOC_PATH} 提取的 '{_RULE_BLOCK_MARKER}' 代码块无法被 M3 的 RuleLoader 加载："
        f"last_error={loader.last_error!r}。文档里的示例必须与 rules.example.yaml 一样真实可用。"
    )
    assert loader.last_error is None, f"示例规则加载后 last_error 应为 None，实际 {loader.last_error!r}"

    ruleset = loader.ruleset
    assert len(ruleset.rules) >= 1, (
        f"{RULE_DOC_PATH} 的 '{_RULE_BLOCK_MARKER}' 代码块至少要有一条规则，实际 {len(ruleset.rules)} 条"
    )

    # 用「按文档顺序第一条规则自己的条件」构造示例消息：只要文档示例可加载，
    # 这条消息就必然命中该规则，因此断言不依赖具体 category 字面量。
    rule = ruleset.rules[0]
    assert rule.match, (
        f"文档示例的第一条规则 {rule.id!r} 没有匹配条件（空 match 会匹配一切），"
        f"无法由此构造示例消息；文档应给出带条件的可执行示例"
    )

    source = "doc-example-source"
    level = Level.INFO
    title = "doc-example-title"
    body_parts: list[str] = []
    for condition in rule.match:
        value = condition.values[0]
        if condition.field == "source":
            source = value
        elif condition.field == "level":
            try:
                level = Level(value)
            except ValueError:
                level = Level.ERROR if value.lower() == "error" else Level.INFO
        elif condition.field == "title":
            title = value
        elif condition.field == "title_contains":
            title = f"{value} doc example"
        elif condition.field == "body":
            body_parts.append(value)
        elif condition.field == "body_contains":
            body_parts.append(f"{value} doc example")
        else:
            raise AssertionError(
                f"文档示例规则 {rule.id!r} 出现未预期的匹配字段 {condition.field!r}"
            )
    body = "\n".join(body_parts) or "doc-example-body"

    classifier = RuleClassifier(loader)
    verdict = classifier.classify(source=source, level=level, title=title, body=body)

    assert verdict.rule_id == rule.id, (
        f"用文档示例第一条规则 {rule.id!r} 的条件构造的消息没有命中它，"
        f"实际 rule_id={verdict.rule_id!r}（文档对匹配语义的描述与示例自相矛盾）"
    )
    assert verdict.category == rule.category, (
        f"文档示例规则 {rule.id!r} 声明的 category 是 {rule.category!r}，"
        f"但按文档描述的示例消息分类得到 {verdict.category!r}（文档文字与示例不一致）"
    )
    # 「与文档文字描述一致」：示例里**每条规则**声明的 category 都必须在文档正文里出现，
    # 否则文档没有描述该规则的分类结果（示例与文字脱节）。
    undocumented = [r.id for r in ruleset.rules if r.category not in doc]
    assert not undocumented, (
        f"{RULE_DOC_PATH} 的示例规则 {undocumented} 的 category 在正文中完全没有出现，"
        f"无法据此判定文档描述了示例的分类结果；示例里声明的 category："
        f"{[(r.id, r.category) for r in ruleset.rules]}"
    )

    # 反向：文档正文里「category 为/是 xxx」「category: xxx」这类**明确描述分类取值**的地方，
    # 出现的名字都必须是示例真实声明过的 category。否则 YAML 改了名而同段落还写着旧名，
    # 文档就在撒谎——这是「文档与真实代码不脱节」的直接检验。
    declared = {r.category for r in ruleset.rules} | {ruleset.defaults.category}
    mentioned = {m.group(1).strip("`\"'（）()") for m in _CATEGORY_CONTEXT.finditer(_prose_only(doc))}
    stale = sorted(name for name in mentioned if name and name not in declared)
    assert not stale, (
        f"{RULE_DOC_PATH} 的说明文字里把分类结果写成 {stale}，"
        f"但示例真实声明的 category 只有 {sorted(declared)}（文档文字与示例不一致）"
    )


# --------------------------------------------------------------------------- #
# 第 2 条（tasks 10.1）：配置文档覆盖 config.example.yaml 的每个键
# --------------------------------------------------------------------------- #
def _collect_config_keys(data: Any, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    if isinstance(data, dict):
        for key, value in data.items():
            name = str(key)
            keys.add(name)
            keys.add(f"{prefix}.{name}" if prefix else name)
            keys |= _collect_config_keys(value, name)
    elif isinstance(data, list):
        for item in data:
            keys |= _collect_config_keys(item, prefix)
    return keys


def _headings(text: str) -> list[str]:
    """`##`/`###` 小节标题的标题文本（不含 ``#`` 与行尾 ``#``）。"""
    return [
        m.group("title").strip().rstrip("#").strip()
        for m in re.finditer(r"(?m)^(?P<hashes>#{2,3})[ \t]+(?P<title>.+?)[ \t]*$", text)
    ]


def test_configuration_doc_covers_every_config_key() -> None:
    text = _read_text(CONFIG_DOC_PATH, what="配置说明文档")
    raw = _read_text(CONFIG_EXAMPLE_PATH, what="config.example.yaml（M1 产物，只读引用）")
    data = yaml.safe_load(raw)
    assert isinstance(data, dict), f"{CONFIG_EXAMPLE_PATH} 的顶层必须是映射"

    top_level = {str(key) for key in data}
    all_keys = _collect_config_keys(data)
    titles = _headings(text)
    assert titles, (
        f"{CONFIG_DOC_PATH} 中没有任何 '##'/'###' 小节标题，"
        f"规格要求每个配置键都有对应小节"
    )

    heading_text = " ".join(titles)

    missing = sorted(key for key in top_level if not re.search(rf"\b{re.escape(key)}\b", heading_text))
    assert not missing, (
        f"{CONFIG_DOC_PATH} 的小节标题里缺少以下 config.example.yaml 顶层键：{missing}；"
        f"现有标题：{titles}"
    )

    # 嵌套键（channels[].id / params.host / credentials.password ...）：标题里可以作为
    # 独立小节出现，也可以写在其父键小节的标题里（如 '### channels[].params'）。
    nested_extra: set[str] = set()
    for key in all_keys:
        leaf = key.rsplit(".", 1)[-1]
        if leaf in top_level:
            continue
        nested_extra.add(leaf)
    undocumented = sorted(
        key for key in nested_extra if not re.search(rf"\b{re.escape(key)}\b", heading_text)
    )
    assert not undocumented, (
        f"{CONFIG_DOC_PATH} 的小节标题里缺少以下 config.example.yaml 嵌套键：{undocumented}；"
        f"现有标题：{titles}"
    )

    # 反向：文档标题里出现的「配置键形状」的词必须真实存在于 config.example.yaml，
    # 既不漏写也不臆造。泛化词（配置/说明/示例……）不计。
    generic = {
        "configuration",
        "config",
        "example",
        "overview",
        "usage",
        "introduction",
        "keys",
        "key",
        "options",
        "settings",
    }
    invented: list[str] = []
    for title in titles:
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", title):
            lowered = token.lower()
            if lowered in generic or len(token) < 2:
                continue
            leaf = token.rsplit(".", 1)[-1]
            if token in all_keys or leaf in all_keys:
                continue
            if any(key.startswith(token) or key.startswith(f"{token}.") for key in all_keys):
                continue
            # 仅当它出现在一个含 '.'/'[' 或下划线的「键形状」标题里才算断言对象。
            if "." in token or "[" in title or "_" in token:
                invented.append(f"{token}（标题：{title}）")
    assert not invented, (
        f"{CONFIG_DOC_PATH} 的标题里出现了 config.example.yaml 中不存在的键：{invented}；"
        f"config.example.yaml 的键集合：{sorted(all_keys)}"
    )


# --------------------------------------------------------------------------- #
# 第 8 条（M10 增量）：每个已注册渠道类型都必须在配置文档里被记载
# --------------------------------------------------------------------------- #
#: 「渠道类型名出现在文档里」的判据：大小写不敏感，且允许被下划线/连字符/反引号/中文括号
#: 等**任意非字母数字字符**包围（``type: feishu``、`` `feishu` ``、``飞书（feishu）``、
#: ``NOTIFY_FEISHU_WEBHOOK_URL`` 都算「出现过这个名字」）。刻意**不**要求章节标题、
#: 句式或 ``type: <name>`` 的写法，也不要求该名以独立词出现——避免把文档的排版选择
#: 变成失败原因；本条的判据严格限定为「类型名在不在文档里」。
def _channel_type_is_mentioned(text: str, channel_type: str) -> bool:
    pattern = re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(channel_type)}(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    return pattern.search(text) is not None


def _registered_channel_types() -> list[str]:
    """``NOTIFIER_FACTORIES`` 的全部 key（导入 ``notify_hub.notifiers`` 包即完成注册）。

    必须导入**包**而不是 ``notify_hub.notifiers.registry``：注册动作写在包的
    ``__init__.py`` 里（M4 的 webhook/email，M10 追加 feishu）。
    """
    from notify_hub.notifiers import NOTIFIER_FACTORIES

    registered = sorted(str(key) for key in NOTIFIER_FACTORIES)
    assert registered, (
        "NOTIFIER_FACTORIES 为空：导入 `notify_hub.notifiers` 应完成 M4 内置适配器"
        "（webhook/email，M10 追加 feishu）的工厂注册。注册表为空时本条契约会"
        "「零个渠道、零个缺失」地空转通过，因此先在此处失败，"
        "而不是让文档契约失去判别力。"
    )
    return registered


def test_every_registered_channel_type_is_documented() -> None:
    """**契约**：每个已在 ``NOTIFIER_FACTORIES`` 注册的渠道类型，都必须在
    ``docs/configuration.md`` 全文中出现（M10 缺口：``feishu`` 已注册但文档未写）。

    这是「实现了渠道但没写文档」的永久自检：下一个适配器忘记写文档时，CI 直接红。
    判据只看「渠道类型名是否出现在文档里」，不看章节标题与句式。
    """
    registered = _registered_channel_types()
    text = _read_text(CONFIG_DOC_PATH, what="配置说明文档")

    missing = [name for name in registered if not _channel_type_is_mentioned(text, name)]
    assert not missing, (
        "以下已注册的通知渠道类型未在 docs/configuration.md 中记载："
        + "、".join(f"渠道类型 `{name}` 未在 docs/configuration.md 中记载" for name in missing)
        + f"。已注册的渠道类型（NOTIFIER_FACTORIES 的 key）：{registered}；"
        f"其中已出现在文档里的：{sorted(set(registered) - set(missing))}。"
        f"请在 {CONFIG_DOC_PATH} 中补上这些渠道类型的说明与配置示例"
        f"（例如 `channels[].type` 的取值列表、以及 `type: {missing[0]}` 的可复制配置片段），"
        f"使文档与真实注册表对齐。"
    )

    # 反向不设断言：文档里出现未注册的 `type: xxx`（如 adapter-guide 的 dummy 示例）
    # 是合法的教学写法，不应被本条契约判红；「文档不能臆造配置键」已由第 2 条覆盖。


# --------------------------------------------------------------------------- #
# 第 3 条（tasks 10.3）：dummy 适配器可直接运行、可注册
# --------------------------------------------------------------------------- #
def test_dummy_adapter_block_is_a_working_notifier() -> None:
    from datetime import datetime, timezone

    from notify_hub.domain import Level
    from notify_hub.notifiers import NotifierRegistry
    from notify_hub.notifiers.base import DeliveryResult, NotificationMessage, Notifier

    doc = _read_text(ADAPTER_DOC_PATH, what="适配器开发指南")
    source = _extract_marked_python_block(doc, marker=_ADAPTER_BLOCK_MARKER, path=ADAPTER_DOC_PATH)

    namespace: dict[str, Any] = {"__name__": "notify_hub_docs_dummy_adapter"}
    try:
        exec(compile(source, str(ADAPTER_DOC_PATH), "exec"), namespace)
    except Exception as exc:  # noqa: BLE001 —— 文档代码块执行失败必须让测试失败
        raise AssertionError(
            f"{ADAPTER_DOC_PATH} 中 '{_ADAPTER_BLOCK_MARKER}' 代码块无法执行："
            f"{type(exc).__name__}: {exc}"
        ) from exc

    # 候选类：文档代码块自己定义的、且「像适配器」的类（有 send()，或有 channel_id 声明）。
    # 通用兜底只认「能作为 channel_id='dummy' 工作的类」，避免把文档里恰好出现的
    # 非适配器类（如某个结果类型）误当成示例适配器。
    defined_types = [
        value
        for name, value in namespace.items()
        if isinstance(value, type) and getattr(value, "__module__", None) == "notify_hub_docs_dummy_adapter"
    ]
    adapter_candidates = [
        value for value in defined_types if callable(getattr(value, "send", None)) or hasattr(value, "channel_id")
    ]

    probe = NotificationMessage(
        title="文档契约测试",
        body="dummy 适配器示例可运行性验证",
        level=Level.INFO,
        source="docs-contract",
        occurred_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    def _construct(cls):
        try:
            return cls(channel_id="dummy")
        except TypeError:
            return cls("dummy")

    if not adapter_candidates:
        # 没有候选类时，允许「只有一个自定义类」的写法（名字里不一定有 dummy）。
        if len(defined_types) == 1:
            adapter_candidates = defined_types
        else:
            raise AssertionError(
                f"{ADAPTER_DOC_PATH} 的 '{_ADAPTER_BLOCK_MARKER}' 代码块里没有定义适配器类"
                f"（需要 send() 或 channel_id）；代码块中定义的类：{[c.__name__ for c in defined_types]}"
            )

    ready: list[tuple[type, Any]] = []
    protocol_errors: list[str] = []
    for cls in adapter_candidates:
        try:
            candidate = _construct(cls)
        except Exception as exc:  # noqa: BLE001
            protocol_errors.append(f"{cls.__name__}: 无法以 channel_id='dummy' 构造（{type(exc).__name__}）")
            continue
        if not isinstance(candidate, Notifier):
            protocol_errors.append(f"{cls.__name__}: 实例不满足 Notifier 协议")
            continue
        try:
            result = candidate.send(probe)
        except Exception as exc:  # noqa: BLE001
            protocol_errors.append(
                f"{cls.__name__}.send() 抛了异常（{type(exc).__name__}: {exc}）——"
                f"Notifier 契约要求 send() MUST NOT 抛异常"
            )
            continue
        if not isinstance(result, DeliveryResult):
            protocol_errors.append(
                f"{cls.__name__}.send() 返回 {type(result).__name__}，必须返回 DeliveryResult"
            )
            continue
        ready.append((cls, candidate))

    assert ready, (
        f"{ADAPTER_DOC_PATH} 的 '{_ADAPTER_BLOCK_MARKER}' 代码块里的适配器示例不可用：{protocol_errors}；"
        f"示例必须能 isinstance(实例, Notifier) 且 send(msg) 返回 DeliveryResult"
    )

    dummy_cls, instance = ready[0]
    assert instance.send(probe).ok is True, (
        f"文档示例 {dummy_cls.__name__} 对一条正常消息应投递成功（ok=True）"
    )

    registry = NotifierRegistry()
    registry.register(instance)
    assert "dummy" in registry.ids(), (
        f"把文档示例适配器以 channel_id='dummy' 注册进 NotifierRegistry 后 ids() 应含 'dummy'，"
        f"实际 {registry.ids()}"
    )


# --------------------------------------------------------------------------- #
# 第 4 条（tasks 10.1）：README 命令面与 CLI 真实参数一致
# --------------------------------------------------------------------------- #
def _cli_help_text() -> str:
    from typer.testing import CliRunner

    from notify_hub.cli import app

    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, (
        f"`notify --help` 退出码应为 0，实际 {result.exit_code}；输出：{result.output[-2000:]}"
    )
    return result.output


def _help_option_names(help_text: str) -> set[str]:
    """从 ``--help`` 输出里抠出长选项名。

    不依赖 rich 的换行/表格布局：先把 ``-x, --long`` 这类参数块压平（去掉换行与缩进），
    再按 ``--`` 前缀切分。
    """
    flattened = re.sub(r"\n[ \t]+", " ", help_text)
    flattened = re.sub(r"[ \t]+", " ", flattened)
    names: set[str] = set()
    for chunk in flattened.split("--")[1:]:
        match = re.match(r"[A-Za-z][A-Za-z0-9-]*", chunk)
        if match:
            names.add("--" + match.group(0))
    return names


def _registered_cli_option_names() -> set[str]:
    """从 ``notify_hub.cli.app`` 已注册的参数声明里取长选项名（与 --help 互为佐证）。"""
    from notify_hub.cli import app

    names: set[str] = set()
    registered = getattr(app, "registered_commands", ()) or ()
    for command in registered:
        callback = getattr(command, "callback", None)
        if callback is None:
            continue
        for param in getattr(callback, "__click_params__", ()) or ():
            for opt in getattr(param, "opts", ()) or ():
                if opt.startswith("--"):
                    names.add(opt)
    return names


def test_readme_cli_options_are_registered() -> None:
    text = _read_text(README_PATH, what="README")
    command_lines = _shell_command_lines(text)
    assert command_lines, (
        f"{README_PATH} 中没有可复制的 `notify ...` 命令示例。"
        f"规格要求 README 给出 HTTP 与 CLI 各一个可复制示例。"
    )

    readme_options: set[str] = set()
    for line in command_lines:
        readme_options |= _long_options_in(line)
    assert readme_options, (
        f"{README_PATH} 的 notify 命令里没有出现任何长选项：{command_lines}"
    )

    missing_minimal = sorted(opt for opt in MINIMAL_CLI_OPTIONS if opt not in readme_options)
    assert not missing_minimal, (
        f"{README_PATH} 的 CLI 示例缺少这些长选项：{missing_minimal}；"
        f"现有示例：{command_lines}"
    )

    help_text = _cli_help_text()
    available = _help_option_names(help_text) | _registered_cli_option_names()
    assert available, f"无法从 `notify --help` 输出或已注册参数中解析出任何长选项名：{help_text[-2000:]}"

    unknown = sorted(
        opt
        for opt in readme_options
        if opt not in REQUIRED_CLI_OPTIONS and opt not in available
    )
    assert not unknown, (
        f"{README_PATH} 的示例里出现了 CLI 未注册的长选项：{unknown}；"
        f"`notify --help` 实际提供：{sorted(available)}"
    )

    # 规格第 4 段点名的 8 个长选项：文档只需用到其中一部分，但凡用到就必须真实存在。
    named_but_missing = sorted(
        opt for opt in REQUIRED_CLI_OPTIONS if opt in readme_options and opt not in available
    )
    assert not named_but_missing, (
        f"README 用到的长选项在 CLI 中不存在：{named_but_missing}；"
        f"`notify --help` 实际提供：{sorted(available)}"
    )


def test_readme_cli_example_runs_against_installed_cli() -> None:
    """可复制示例必须真能执行：文档在，且 CLI 在一个干净进程里可运行（``--help`` 退出码 0）。"""
    _read_text(README_PATH, what="README")
    src_dir = REPO_ROOT / "src"
    assert src_dir.is_dir(), f"源码目录不存在：{src_dir}"

    proc = subprocess.run(
        [sys.executable, "-m", "notify_hub.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(src_dir)},
    )
    if proc.returncode != 0 and "No module named" in (proc.stderr or ""):
        # 兼容实现没有给 cli 模块加 `if __name__ == "__main__"` 的情形。
        proc = subprocess.run(
            [sys.executable, "-c", "from notify_hub.cli import app; app(['--help'])"],
            capture_output=True,
            text=True,
            timeout=120,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(src_dir)},
        )
    assert proc.returncode == 0, (
        f"README 承诺的 CLI 不可执行——依赖 M5（`notify_hub.cli`）落地。"
        f"returncode={proc.returncode}\nstdout={proc.stdout[-1000:]}\nstderr={proc.stderr[-1000:]}"
    )


def test_readme_cli_options_exist_in_real_cli() -> None:
    """README 命令面：示例里写到的**每个长选项名都必须真实存在**。

    **纯静态/参数层面的契约（规格第 6 节 M9 第 4 段冻结）**：只比对「文档写的选项名」与
    「CLI 的 ``--help`` 输出 / 已注册参数名」，**不执行** README 里的命令行——它们是真实可复制的
    投递命令，离线必然以退出码 3（不可达）结束；把「示例可复制」偷换成「示例能离线跑通」会逼着
    文档作者写伪示例。CLI 的**行为**验证是 M5 的测试职责。
    """
    text = _read_text(README_PATH, what="README")
    command_lines = _shell_command_lines(text)
    assert command_lines, (
        f"{README_PATH} 中没有可复制的 `notify ...` 命令示例。"
        f"规格要求 README 给出 HTTP 与 CLI 各一个可复制示例。"
    )

    readme_options: set[str] = set()
    for line in command_lines:
        readme_options |= _long_options_in(line)
    assert readme_options, f"{README_PATH} 的 notify 命令里没有出现任何长选项：{command_lines}"

    help_text = _cli_help_text()
    available = _help_option_names(help_text) | _registered_cli_option_names()
    assert available, f"无法从 `notify --help` 输出或已注册参数中解析出任何长选项名：{help_text[-2000:]}"

    unknown = sorted(opt for opt in readme_options if opt not in available)
    assert not unknown, (
        f"{README_PATH} 的示例里出现了 CLI 未注册的长选项：{unknown}；"
        f"`notify --help` 实际提供：{sorted(available)}"
    )


# --------------------------------------------------------------------------- #
# 第 5 条（tasks 10.4）：部署安全边界
# --------------------------------------------------------------------------- #
def test_deployment_doc_states_loopback_and_systemd_boundary() -> None:
    text = _read_text(DEPLOYMENT_DOC_PATH, what="部署说明")

    assert ("回环" in text) or ("127.0.0.1" in text), (
        f"{DEPLOYMENT_DOC_PATH} 必须写明「默认只绑定回环地址」的强制安全边界"
        f"（应出现「回环」或 127.0.0.1）"
    )
    assert "[Unit]" in text and "[Service]" in text, (
        f"{DEPLOYMENT_DOC_PATH} 必须包含可复制的 systemd 单元示例（[Unit] 与 [Service] 段）"
    )
    # add-public-access 之后本文件的边界契约被**有意取代**：服务现在内置访问令牌认证
    # （server.auth_token），公网暴露是被支持且有文档的部署方式，因此不再写
    # 「不要暴露到公网」——那句话在本次变更后已不成立。契约随之改为断言**新的**真话：
    # 暴露前必须配置令牌，且应用自身不得改绑 0.0.0.0。
    # 裁定记录见 openspec/changes/add-public-access/architecture.md §6 的 R4。
    assert "auth_token" in text, (
        f"{DEPLOYMENT_DOC_PATH} 必须写明访问令牌的配置键 server.auth_token"
    )
    assert ("必须先配令牌" in text) or ("先配令牌" in text), (
        f"{DEPLOYMENT_DOC_PATH} 必须含「暴露之前必须先配令牌」的显式警示"
        f"（未配置令牌时鉴权整体关闭）"
    )
    assert "0.0.0.0" in text, (
        f"{DEPLOYMENT_DOC_PATH} 必须点名「不要改成 0.0.0.0」这个反面，"
        f"把「应用自身只绑回环」的边界写死"
    )
    assert ("反向代理" in text) and ("鉴权" in text), (
        f"{DEPLOYMENT_DOC_PATH} 必须说明跨机器使用需自加反向代理与鉴权（本版本不内置 TLS）"
    )

    exec_lines = [line.strip() for line in text.splitlines() if "ExecStart" in line]
    assert exec_lines, f"{DEPLOYMENT_DOC_PATH} 的 systemd 单元里必须有 ExecStart 行"
    assert any("notify_hub" in line for line in exec_lines), (
        f"{DEPLOYMENT_DOC_PATH} 的 ExecStart 行必须包含 notify_hub，实际：{exec_lines}"
    )


# --------------------------------------------------------------------------- #
# 第 6 条：无凭据泄漏
# --------------------------------------------------------------------------- #
#: 文档里出现的完整 URL（``http(s)://...``）。扫描凭据正则前先挖掉它们——URL 内嵌的
#: ``?access_token=...`` 不是「把凭据写进文档」，而是「示例地址长什么样」；真凭据会以
#: 键值赋值形态出现在 URL 之外。挖掉用等长空格替换，保持行号/列号不变。
_URL_PATTERN = re.compile(r"https?://\S+")


def _credential_hits(text: str) -> list[str]:
    """按冻结正则扫描凭据，返回**剔除占位符与 URL 之后**仍命中的片段。"""
    scrubbed = _URL_PATTERN.sub(lambda m: " " * len(m.group(0)), text)
    hits: list[str] = []
    for match in CREDENTIAL_PATTERN.finditer(scrubbed):
        hit = match.group(0)
        if not _placeholder_hit(hit):
            hits.append(text[match.start() : match.end()])
    return hits


@pytest.mark.parametrize("path", ALL_DOC_PATHS, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_document_leaks_credentials(path: Path) -> None:
    text = _read_text(path, what="M9 文档")
    hits = _credential_hits(text)
    assert not hits, (
        f"{path} 命中凭据形态正则 {CREDENTIAL_PATTERN.pattern!r}（已剔除占位符）：{hits}；"
        f"文档中禁止出现真实凭据/真实 token（只允许写环境变量名与占位符）"
    )


# --------------------------------------------------------------------------- #
# 第 7 条：文档索引可达
# --------------------------------------------------------------------------- #
_LINK_PATTERN = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _docs_relative_links(text: str) -> set[str]:
    links: set[str] = set()
    for target in _LINK_PATTERN.findall(text):
        target = target.strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        clean = target.split("#", 1)[0].split("?", 1)[0].strip()
        if not clean or clean.startswith("//"):
            continue
        links.add(clean)
    return links


def test_readme_doc_links_resolve() -> None:
    text = _read_text(README_PATH, what="README")
    links = _docs_relative_links(text)
    assert links, (
        f"{README_PATH} 中没有任何相对链接（规格要求 README 含「文档索引」，"
        f"把 docs/ 下的四份文档串起来）"
    )

    docs_links = {link for link in links if link.startswith("docs/") or "/docs/" in link}
    assert docs_links, (
        f"{README_PATH} 的相对链接里没有指向 docs/*.md 的索引项，实际链接：{sorted(links)}"
    )

    missing: list[str] = []
    for link in sorted(docs_links):
        candidate = (REPO_ROOT / link).resolve()
        if not candidate.exists():
            missing.append(f"{link} -> {candidate}")
    assert not missing, f"{README_PATH} 中的文档链接指向不存在的文件：{missing}"
