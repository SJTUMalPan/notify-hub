"""M1 模块测试：脱敏原语与日志端到端脱敏。

依据：``openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md`` 第 6 节「模块 M1」第 4 段第 6–8 条，
以及 ``specs/message-ingest``「凭据隔离」、``specs/notification-delivery``「凭据管理」两条需求。

实现（``notify_hub.redact`` / ``notify_hub.logging_setup``）在本阶段尚不存在，因此本文件顶部
**不做**模块级导入；对实现符号的导入都写在测试函数体内，以保证 pytest 能成功**收集**本文件。
"""

from __future__ import annotations

import logging

import pytest

from notify_hub.errors import ConfigurationError  # noqa: F401  (阶段 0 已存在，验证可导入)

MASK = "***"


# --------------------------------------------------------------------------- #
# 第 4 段 6：redact_url / redact_text / redact_mapping / redact_exception
# --------------------------------------------------------------------------- #
def test_redact_url_masks_sensitive_query_values() -> None:
    from notify_hub.redact import redact_url

    token = "abc123def"
    url = f"https://oapi.dingtalk.com/robot/send?access_token={token}&x=1"

    out = redact_url(url)

    assert "access_token=" + MASK in out
    assert token not in out


def test_redact_url_masks_userinfo_password_with_secret() -> None:
    from notify_hub.redact import redact_url

    password = "pa55w0rd"
    url = f"https://user:{password}@h/p"

    out = redact_url(url, secrets=[password])

    assert password not in out
    assert "user" in out


def test_redact_url_masks_userinfo_password_without_secrets() -> None:
    """无条件生效（M1 第 2 段 redact_url 第 1 条）：不传 secrets 也必须掩码密码。"""
    from notify_hub.redact import redact_url

    password = "pa55w0rd"
    url = f"https://user:{password}@h/p"

    out = redact_url(url)  # 不传 secrets

    assert password not in out
    assert "user" in out          # 只掩码密码，保留用户名


def test_redact_url_masks_sensitive_query_without_secrets() -> None:
    """无条件生效（M1 第 2 段 redact_url 第 2 条）：不传 secrets 也必须掩码敏感键的值。"""
    from notify_hub.redact import redact_url

    token = "abc123def"

    out = redact_url(f"https://h/p?access_token={token}")  # 不传 secrets

    assert token not in out


def test_redact_text_removes_every_occurrence() -> None:
    from notify_hub.redact import redact_text

    secret = "p@ss"

    out = redact_text(f"auth failed for {secret} (again {secret})", [secret])

    assert secret not in out
    assert MASK in out


def test_redact_text_ignores_empty_secret_and_accepts_iterable() -> None:
    from notify_hub.redact import redact_text

    secret = "TOPSECRET"

    assert redact_text("plain text", [""]) == "plain text"
    assert secret not in redact_text(f"x {secret}", (s for s in ["", secret]))


def test_redact_mapping_masks_by_key_name_recursively() -> None:
    from notify_hub.redact import redact_mapping

    data = {"password": "s3cr3t", "n": 1, "nested": [{"token": "t0k"}, "keep"]}

    out = redact_mapping(data)

    assert out["password"] == MASK
    assert out["nested"][0]["token"] == MASK
    assert out["nested"][1] == "keep"
    assert out["n"] == 1
    assert "s3cr3t" not in str(out)
    assert "t0k" not in str(out)


def test_redact_mapping_applies_secrets_to_free_text() -> None:
    from notify_hub.redact import redact_mapping

    secret = "TOPSECRET"

    out = redact_mapping({"note": f"see {secret}", "password": "x"}, [secret])

    assert secret not in str(out)
    assert out["password"] == MASK


def test_redact_exception_names_type_and_hides_secret() -> None:
    from notify_hub.redact import redact_exception

    secret = "p@ss"

    out = redact_exception(ValueError(f"bad {secret}"), [secret])

    assert "ValueError" in out
    assert secret not in out


def test_redact_exception_of_configuration_error_hides_credentials() -> None:
    from notify_hub.redact import redact_exception

    token = "BOTSECRET"

    out = redact_exception(ConfigurationError(f"channel url={token} invalid"), [token])

    assert "ConfigurationError" in out
    assert token not in out


# --------------------------------------------------------------------------- #
# 第 4 段 5b：extract_url_secrets()（新增，安全关键；P1 修复的回归点）
#
# 这些用例按规格写死在「函数尚不存在」的状态下：导入失败本身就是「功能缺失」的证据。
# --------------------------------------------------------------------------- #
def test_extract_url_secrets_from_sensitive_query_value() -> None:
    """query 中命中 SENSITIVE_KEYS 的值被提取；非敏感键 `x=1` 的值不进入集合。"""
    from notify_hub.redact import extract_url_secrets

    found = extract_url_secrets("https://h/p?access_token=abc123def&x=1")

    assert "abc123def" in found
    assert "1" not in found
    assert set(found) == {"abc123def"}


def test_extract_url_secrets_from_userinfo_password() -> None:
    """userinfo 的密码部分被提取（用户名不算密钥）。"""
    from notify_hub.redact import extract_url_secrets

    found = extract_url_secrets("https://user:pa55w0rd@h/p")

    assert "pa55w0rd" in found
    assert set(found) == {"pa55w0rd"}


def test_extract_url_secrets_matches_key_case_and_suffix() -> None:
    """键名匹配大小写不敏感，且支持后缀匹配（`access_token` / `my_token` 都命中 `token`）。"""
    from notify_hub.redact import extract_url_secrets

    upper = extract_url_secrets("https://h/p?ACCESS_TOKEN=abcdefgh")
    suffixed = extract_url_secrets("https://h/p?my_token=abcdefgh")

    assert "abcdefgh" in upper
    assert "abcdefgh" in suffixed


def test_extract_url_secrets_enforces_min_length_safety_valve() -> None:
    """`min_length` 是安全阀：长度不足 6 的成分被丢弃；显式放小阈值时必须生效。"""
    from notify_hub.redact import extract_url_secrets

    assert extract_url_secrets("https://h/p?token=abc") == ()          # 默认 min_length=6
    assert "abc" in extract_url_secrets("https://h/p?token=abc", min_length=3)


def test_extract_url_secrets_returns_empty_for_non_url_without_raising() -> None:
    """非 URL 输入返回空元组且不抛异常。"""
    from notify_hub.redact import extract_url_secrets

    for not_a_url in ("not a url", ""):
        found = extract_url_secrets(not_a_url)

        assert isinstance(found, tuple)
        assert found == ()


def test_extract_url_secrets_treats_bare_query_string_as_non_url() -> None:
    """「是否算 URL」的冻结判定：以 ``urlsplit(url).scheme`` 非空为准。

    裸 query 串 ``"?ACCESS_TOKEN=abcdefgh"`` 没有 scheme，因此**不是** URL
    → 返回空元组，且不抛异常（不得因为键名命中敏感键就把它当 URL 解析）。
    """
    from notify_hub.redact import extract_url_secrets

    found = extract_url_secrets("?ACCESS_TOKEN=abcdefgh")

    assert isinstance(found, tuple)
    assert found == ()


def test_extract_url_secrets_deduplicates_and_drops_empty_values() -> None:
    """结果去重（同一成分既是密码又是敏感 query 值）且剔除空串。"""
    from notify_hub.redact import extract_url_secrets

    found = extract_url_secrets("https://u:abc123@h/p?token=abc123")

    assert len(found) == 1
    assert found.count("abc123") == 1
    assert extract_url_secrets("https://h/p?token=") == ()


# --------------------------------------------------------------------------- #
# 第 4 段 5b 末条：路径末段 token 提取（**第二次同类 P1 泄漏**的回归点）
#
# 飞书/Slack/Discord 把 webhook 凭据放在 URL 路径末段而非 query。若只提 query 与 userinfo，
# 平台在业务错误 msg 里回显的裸 token 就匹配不上（secrets 里只有完整 URL），明文经
# error_reason → 投递记录 → 查询接口/日志外泄。阈值 path_min_length=16 见 M1 第 2 段。
# --------------------------------------------------------------------------- #
def test_extract_url_secrets_from_feishu_style_path_token() -> None:
    """飞书形态：凭据在路径末段 ``/hook/<token>``，必须被提取为独立密钥。"""
    from notify_hub.redact import extract_url_secrets

    token = "FSECRETTOKEN1234567890"

    found = extract_url_secrets(f"https://open.feishu.cn/open-apis/bot/v2/hook/{token}")

    assert token in found


def test_extract_url_secrets_from_slack_style_path_token() -> None:
    """Slack 形态：``/services/T.../B.../<token>`` 的末段必须被提取（≥16）。"""
    from notify_hub.redact import extract_url_secrets

    token = "XXXXXXXXXXXXXXXXXXXXXXXX"

    found = extract_url_secrets(
        f"https://hooks.slack.com/services/T00000000/B00000000/{token}"
    )

    assert token in found


def test_extract_url_secrets_path_safety_valve_excludes_ordinary_segment() -> None:
    """安全阀必须生效：``https://h/status`` → 空元组。

    9 字符的 ``status`` 若进入全局密钥集合，``redact_text`` 会把日志里**所有** ``status``
    打成 ``***``（同 ``?key=1`` 的理由）。阈值 16 是刻意取的更严一侧。
    """
    from notify_hub.redact import extract_url_secrets

    found = extract_url_secrets("https://h/status")

    assert isinstance(found, tuple)
    assert found == ()


def test_extract_url_secrets_path_requires_token_charset() -> None:
    """字符集限制：仅收形如不透明令牌（``[A-Za-z0-9_-]``）的片段。

    ``/aaaa.bbbb.cccc.dddd`` 被 ``.`` 切分成多个短片段，全部落入长度门槛之下，不得提取。
    """
    from notify_hub.redact import extract_url_secrets

    found = extract_url_secrets("https://h/aaaa.bbbb.cccc.dddd")

    assert isinstance(found, tuple)
    assert found == ()


def test_extract_url_secrets_path_threshold_boundary_is_inclusive() -> None:
    """阈值是「≥ ``path_min_length``」：长度 16 的片段被提取，长度 15 的不被提取。"""
    from notify_hub.redact import extract_url_secrets

    at_threshold = "A" * 16
    below_threshold = "B" * 15

    found_at = extract_url_secrets(f"https://h/{at_threshold}")
    found_below = extract_url_secrets(f"https://h/{below_threshold}")

    assert at_threshold in found_at
    assert below_threshold not in found_below


# --------------------------------------------------------------------------- #
# 第 4 段 7：日志端到端脱敏（tasks 1.2 / 9.6 的服务端一半）
# --------------------------------------------------------------------------- #
def test_setup_logging_redacts_secret_end_to_end(caplog) -> None:
    from notify_hub.logging_setup import setup_logging

    secret = "TOPSECRET"
    logger = setup_logging("INFO", secrets=[secret])

    with caplog.at_level(logging.INFO):
        logger.error("url=%s", f"https://h/?token={secret}")

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert rendered, "日志记录未被捕获，无法证明脱敏生效"
    assert secret not in rendered
    assert MASK in rendered


def test_setup_logging_returns_notify_hub_logger_and_installs_filter(caplog) -> None:
    from notify_hub.logging_setup import SecretFilter, get_logger, setup_logging

    logger = setup_logging("INFO", secrets=["TOPSECRET"])

    assert logger.name == "notify_hub"
    assert get_logger().name == "notify_hub"

    root = logging.getLogger()
    filters = [f for handler in root.handlers for f in handler.filters]
    assert any(isinstance(f, SecretFilter) for f in filters)


def test_setup_logging_is_idempotent_for_root_handlers() -> None:
    from notify_hub.logging_setup import setup_logging

    root = logging.getLogger()
    before = len(root.handlers)

    setup_logging("INFO", secrets=["TOPSECRET"])
    once = len(root.handlers)
    setup_logging("INFO", secrets=["TOPSECRET"])
    twice = len(root.handlers)

    assert once == twice, "重复调用 setup_logging 叠加了 handler"
    assert twice - before <= 1, "单次调用不应添加多个 handler"


def test_caplog_records_are_redacted_for_inherited_loggers(caplog) -> None:
    """任何子 logger 的日志都不得泄漏 secret（脱敏装在 handler 上而非仅 logger 上）。"""
    from notify_hub.logging_setup import setup_logging

    secret = "TOPSECRET"
    setup_logging("INFO", secrets=[secret])

    child = logging.getLogger("notify_hub.classifier")

    with caplog.at_level(logging.INFO):
        child.error("loading %s failed", f"rules?token={secret}")

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert rendered
    assert secret not in rendered
