"""M1 模块测试：配置加载、路径解析、环境变量凭据解析与错误契约。

依据：``openspec/changes/add-notify-hub/architecture.md`` 第 6 节「模块 M1」第 2/3/4 段。
实现（``notify_hub.config``）在本阶段尚不存在，因此本文件顶部**不做**模块级导入；
所有对实现符号的导入都写在测试函数体内（与 ``conftest.py`` 的惰性导入约定一致），
以保证 pytest 能成功**收集**本文件、失败只发生在运行时的「实现缺失」。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from notify_hub.errors import ConfigurationError

# --------------------------------------------------------------------------- #
# 辅助：按 architecture.md 第 6 节「3. 内部实现」中的配置形状写出合法 YAML
# --------------------------------------------------------------------------- #

VALID_CONFIG = """\
server:
  host: 127.0.0.1
  port: 8000
  log_level: DEBUG
storage:
  db_path: ./data/notify.db
rules:
  path: ./rules.yaml
  poll_interval_seconds: 7
reminders:
  scan_interval_seconds: 60
  first_reminder_after_seconds: 1800
  reminder_interval_seconds: 3600
default_channel: webhook
channels:
  - id: webhook
    type: webhook
    enabled: true
    params:
      url_env: NOTIFY_WEBHOOK_URL
      field_map: {}
      headers: {}
    credentials:
      url: NOTIFY_WEBHOOK_URL
  - id: email
    type: email
    enabled: false
    params:
      host: smtp.example.com
      port: 587
      use_tls: true
      sender: notify@example.com
      recipients: [me@example.com]
    credentials:
      password: NOTIFY_SMTP_PASSWORD
"""

WEBHOOK_URL = "https://h/x?token=SECRET123"


def _write_config(directory: Path, body: str, name: str = "config.yaml") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _load(path=None, *, env=None):
    from notify_hub.config import load_settings  # M1，惰性导入

    return load_settings(path, env=env)


# --------------------------------------------------------------------------- #
# 第 4 段 1–2：合法配置逐项解析 + 相对路径解析
# --------------------------------------------------------------------------- #
def test_load_settings_parses_every_key(tmp_path: Path) -> None:
    path = _write_config(tmp_path, VALID_CONFIG)

    settings = _load(path)
    root = tmp_path.resolve()

    # 顶层标量
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert isinstance(settings.port, int)
    assert settings.log_level == "DEBUG"
    assert settings.default_channel == "webhook"
    assert settings.rules_poll_interval_seconds == 7

    # 路径：绝对化，且父目录 = 配置文件所在目录
    assert isinstance(settings.db_path, Path)
    assert settings.db_path.is_absolute()
    assert settings.db_path == root / "data" / "notify.db"
    assert settings.db_path.parent == root / "data"

    assert isinstance(settings.rules_path, Path)
    assert settings.rules_path.is_absolute()
    assert settings.rules_path == root / "rules.yaml"

    # 提醒参数
    assert settings.reminders.scan_interval_seconds == 60
    assert settings.reminders.first_reminder_after_seconds == 1800
    assert settings.reminders.reminder_interval_seconds == 3600

    # 渠道：顺序、字段与 params 原样保留
    assert len(settings.channels) == 2
    webhook, email = settings.channels
    assert webhook.id == "webhook"
    assert webhook.type == "webhook"
    assert webhook.enabled is True
    assert webhook.params["url_env"] == "NOTIFY_WEBHOOK_URL"
    assert email.id == "email"
    assert email.type == "email"
    assert email.enabled is False
    assert email.params["host"] == "smtp.example.com"
    assert list(email.params["recipients"]) == ["me@example.com"]

    # 凭据映射的**键**是逻辑名；未提供 env 时值为 None
    assert set(webhook.credentials) == {"url"}
    assert set(email.credentials) == {"password"}


def test_relative_paths_resolve_against_config_directory(tmp_path: Path) -> None:
    config_dir = tmp_path / "a"
    path = _write_config(
        config_dir,
        """\
        storage:
          db_path: ./d/x.db
        rules:
          path: ./r.yaml
        """,
    )

    settings = _load(path)

    assert settings.db_path == config_dir.resolve() / "d" / "x.db"
    assert settings.rules_path == config_dir.resolve() / "r.yaml"


# --------------------------------------------------------------------------- #
# 第 4 段 3：环境变量凭据解析（有值 / 无值都不抛）
# --------------------------------------------------------------------------- #
def test_credentials_resolved_from_env(tmp_path: Path) -> None:
    path = _write_config(tmp_path, VALID_CONFIG)

    settings = _load(path, env={"NOTIFY_WEBHOOK_URL": WEBHOOK_URL})
    webhook = settings.channels[0]

    assert webhook.credentials["url"] == WEBHOOK_URL
    assert webhook.credentials_complete is True

    # 另一条渠道该环境变量未设置 -> 声明了凭据但解析不出值
    email = settings.channels[1]
    assert email.credentials["password"] is None
    assert email.credentials_complete is False


def test_missing_env_yields_none_without_raising(tmp_path: Path) -> None:
    path = _write_config(tmp_path, VALID_CONFIG)

    settings = _load(path, env={})  # MUST NOT 抛异常：凭据缺失 != 启动错误

    webhook = settings.channels[0]
    assert webhook.credentials["url"] is None
    assert webhook.credentials_complete is False


# --------------------------------------------------------------------------- #
# 第 4 段 4：错误契约逐条
# --------------------------------------------------------------------------- #
def test_missing_config_file_message_contains_absolute_path(tmp_path: Path) -> None:
    missing = (tmp_path / "nope" / "config.yaml").resolve()

    with pytest.raises(ConfigurationError) as excinfo:
        _load(missing)

    message = str(excinfo.value)
    assert str(missing) in message
    assert missing.is_absolute()


def test_invalid_yaml_syntax_message_locates_file_and_line(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """\
        storage:
          db_path: ./x.db
        rules: {path: ./r.yaml
        """,
    )

    with pytest.raises(ConfigurationError) as excinfo:
        _load(path)

    message = str(excinfo.value)
    assert path.name in message
    assert "line" in message


def test_yaml_valid_but_not_a_mapping(tmp_path: Path) -> None:
    """至少 2 个异常场景 (a)：YAML 合法但结构错 -> ConfigurationError（不是 AttributeError）。"""
    path = _write_config(tmp_path, "- 这不是映射\n")

    with pytest.raises(ConfigurationError) as excinfo:
        _load(path)

    assert str(excinfo.value).strip() != ""


def test_missing_db_path_message_names_the_key(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """\
        rules:
          path: ./rules.yaml
        """,
    )

    with pytest.raises(ConfigurationError) as excinfo:
        _load(path)

    assert "db_path" in str(excinfo.value)


def test_missing_rules_path_message_names_the_key(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """\
        storage:
          db_path: ./data/notify.db
        """,
    )

    with pytest.raises(ConfigurationError) as excinfo:
        _load(path)

    assert "rules" in str(excinfo.value)


def test_default_channel_not_in_channels(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """\
        storage:
          db_path: ./data/notify.db
        rules:
          path: ./rules.yaml
        default_channel: slack
        channels:
          - id: webhook
            type: webhook
            credentials:
              url: NOTIFY_WEBHOOK_URL
        """,
    )

    with pytest.raises(ConfigurationError) as excinfo:
        _load(path)

    message = str(excinfo.value)
    assert "slack" in message          # 该值
    assert "webhook" in message        # 渠道列表


def test_duplicate_channel_id(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """\
        storage:
          db_path: ./data/notify.db
        rules:
          path: ./rules.yaml
        channels:
          - id: dup
            type: webhook
          - id: dup
            type: webhook
        """,
    )

    with pytest.raises(ConfigurationError) as excinfo:
        _load(path)

    assert "dup" in str(excinfo.value)


def test_reminder_interval_smaller_than_scan_interval(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """\
        storage:
          db_path: ./data/notify.db
        rules:
          path: ./rules.yaml
        reminders:
          scan_interval_seconds: 60
          first_reminder_after_seconds: 1800
          reminder_interval_seconds: 10
        """,
    )

    with pytest.raises(ConfigurationError) as excinfo:
        _load(path)

    message = str(excinfo.value)
    assert "reminder_interval_seconds" in message
    assert "scan_interval_seconds" in message
    assert "10" in message
    assert "60" in message


def test_first_reminder_threshold_smaller_than_scan_interval(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """\
        storage:
          db_path: ./data/notify.db
        rules:
          path: ./rules.yaml
        reminders:
          scan_interval_seconds: 60
          first_reminder_after_seconds: 5
          reminder_interval_seconds: 3600
        """,
    )

    with pytest.raises(ConfigurationError) as excinfo:
        _load(path)

    message = str(excinfo.value)
    assert "first_reminder_after_seconds" in message
    assert "scan_interval_seconds" in message
    assert "5" in message
    assert "60" in message


def test_reminder_bounds_equal_to_scan_interval_are_valid(tmp_path: Path) -> None:
    """边界语义（architecture.md M1「边界语义」）：判定式是严格小于，`==` 合法。

    即三个参数同为 60 时必须加载成功（拒绝会误伤「每个扫描周期都可提醒」的合法配置）。
    """
    path = _write_config(
        tmp_path,
        """\
        storage:
          db_path: ./data/notify.db
        rules:
          path: ./rules.yaml
        reminders:
          scan_interval_seconds: 60
          first_reminder_after_seconds: 60
          reminder_interval_seconds: 60
        """,
    )

    settings = _load(path)  # MUST NOT 抛 ConfigurationError

    assert settings.reminders.scan_interval_seconds == 60
    assert settings.reminders.first_reminder_after_seconds == 60
    assert settings.reminders.reminder_interval_seconds == 60


def test_configuration_error_messages_contain_no_credential_value(tmp_path: Path) -> None:
    """错误契约的横向要求：任何 ConfigurationError 消息里不得出现凭据值。"""
    import notify_hub.config as config_mod

    bot_url = "https://oapi.example.com/robot/send?access_token=BOTSECRET"
    smtp_password = "SMTPPASSWORD"
    env = {"NOTIFY_WEBHOOK_URL": bot_url, "NOTIFY_SMTP_PASSWORD": smtp_password}

    cases = {
        "bad-default-channel": VALID_CONFIG.replace("default_channel: webhook", "default_channel: nope"),
        "bad-reminder": VALID_CONFIG.replace("reminder_interval_seconds: 3600",
                                            "reminder_interval_seconds: 1"),
        "bad-yaml": "storage: {db_path: ./x.db\n",
    }
    for name, body in cases.items():
        path = _write_config(tmp_path / name, body)
        with pytest.raises(ConfigurationError) as excinfo:
            config_mod.load_settings(path, env=env)
        message = str(excinfo.value)
        assert "BOTSECRET" not in message, name
        assert "SMTPPASSWORD" not in message, name
        assert bot_url not in message, name
        assert smtp_password not in message, name


# --------------------------------------------------------------------------- #
# 第 4 段 5：credential_values()
# --------------------------------------------------------------------------- #
def test_credential_values_two_distinct_resolved_credentials(tmp_path: Path) -> None:
    """两个渠道各有**互不相同**的已解析凭据 + 1 个未设置的渠道 -> 长度为 2 的集合。"""
    path = _write_config(
        tmp_path,
        """\
        storage:
          db_path: ./data/notify.db
        rules:
          path: ./rules.yaml
        default_channel: webhook
        channels:
          - id: webhook
            type: webhook
            credentials:
              url: NOTIFY_WEBHOOK_URL
          - id: mirror
            type: webhook
            credentials:
              url: NOTIFY_WEBHOOK_URL_SAME
          - id: unset
            type: webhook
            credentials:
              url: NOTIFY_WEBHOOK_URL_UNSET
        """,
    )
    # 刻意使用「非 URL 的不可读串」：URL 形态的值会被 credential_values() 展开而额外贡献条目，
    # 使长度断言与展开行为纠缠。隔离后本用例只验「去重」，展开由 extract_url_secrets 用例验证。
    value_a = "T0KEN_A_VALUE"
    value_b = "T0KEN_B_VALUE"
    settings = _load(
        path,
        env={"NOTIFY_WEBHOOK_URL": value_a, "NOTIFY_WEBHOOK_URL_SAME": value_b},
    )

    from notify_hub.config import credential_values

    values = credential_values(settings)

    assert isinstance(values, tuple)
    assert len(values) == 2                      # 两个已解析凭据；未设置的渠道不贡献值
    assert set(values) == {value_a, value_b}     # 两个值都在，且是这两个值
    assert "" not in values
    assert None not in values


def test_credential_values_deduplicates_across_channels(tmp_path: Path) -> None:
    """裁定要点：两个渠道解析出**同一个**值 -> 全局去重，只保留一份（长度 1）。"""
    path = _write_config(
        tmp_path,
        """\
        storage:
          db_path: ./data/notify.db
        rules:
          path: ./rules.yaml
        default_channel: webhook
        channels:
          - id: webhook
            type: webhook
            credentials:
              url: NOTIFY_WEBHOOK_URL
          - id: mirror
            type: webhook
            credentials:
              url: NOTIFY_WEBHOOK_URL_SAME
        """,
    )
    # 同上：非 URL 不可读串，确保本用例只测「跨渠道全局去重」这一条性质。
    shared = "SHARED_T0KEN_VALUE"
    settings = _load(
        path,
        env={"NOTIFY_WEBHOOK_URL": shared, "NOTIFY_WEBHOOK_URL_SAME": shared},
    )

    from notify_hub.config import credential_values

    values = credential_values(settings)

    assert isinstance(values, tuple)
    assert len(values) == 1
    assert set(values) == {shared}
    assert "" not in values
    assert None not in values


def test_credential_values_empty_when_all_unset(tmp_path: Path) -> None:
    """全部渠道凭据都未设置 -> 空元组。"""
    path = _write_config(
        tmp_path,
        """\
        storage:
          db_path: ./data/notify.db
        rules:
          path: ./rules.yaml
        default_channel: webhook
        channels:
          - id: webhook
            type: webhook
            credentials:
              url: NOTIFY_WEBHOOK_URL
          - id: mirror
            type: webhook
            credentials:
              url: NOTIFY_WEBHOOK_URL_SAME
        """,
    )
    settings = _load(path, env={})

    from notify_hub.config import credential_values

    values = credential_values(settings)

    assert isinstance(values, tuple)
    assert values == ()
    assert len(values) == 0


def test_credential_values_expands_url_embedded_token(tmp_path: Path) -> None:
    """P1 回归点（第 4 段 5 最后两小条）：URL 凭据必须展开出内嵌的裸 token。

    生产装配下 webhook 渠道的凭据值是**完整 URL**，而平台错误信息里回显的是**裸 token**；
    只把整段 URL 当密钥时 ``redact_text`` 的子串匹配漏掉裸 token，明文落进
    ``DeliveryRecord.error_reason`` 并外泄。
    """
    path = _write_config(
        tmp_path,
        """\
        storage:
          db_path: ./data/notify.db
        rules:
          path: ./rules.yaml
        default_channel: webhook
        channels:
          - id: webhook
            type: webhook
            credentials:
              url: NOTIFY_WEBHOOK_URL
        """,
    )
    bare_token = "P1SECRET123"
    url = f"https://h/p?access_token={bare_token}"
    settings = _load(path, env={"NOTIFY_WEBHOOK_URL": url})

    from notify_hub.config import credential_values

    values = credential_values(settings)

    assert url in values            # 完整 URL 仍在结果中
    assert bare_token in values     # 裸 token 也必须成为独立密钥
    assert set(values) == {url, bare_token}


def test_credential_values_url_expansion_respects_min_length_valve(tmp_path: Path) -> None:
    """第 4 段 5 最后一小条：``?key=1`` 的裸值 ``"1"`` 因长度不足**不**进入全局密钥集合。

    否则 ``redact_text`` 会把日志里所有出现的 ``1`` 都打成 ``***``，日志立刻不可读。
    """
    path = _write_config(
        tmp_path,
        """\
        storage:
          db_path: ./data/notify.db
        rules:
          path: ./rules.yaml
        default_channel: webhook
        channels:
          - id: webhook
            type: webhook
            credentials:
              url: NOTIFY_WEBHOOK_URL
        """,
    )
    url = "https://h/p?key=1"
    settings = _load(path, env={"NOTIFY_WEBHOOK_URL": url})

    from notify_hub.config import credential_values

    values = credential_values(settings)

    assert url in values            # 完整 URL 仍在结果中
    assert "1" not in values        # 短值不得被当作全局密钥
    assert set(values) == {url}


# --------------------------------------------------------------------------- #
# 第 4 段「异常场景」(b)：声明了多个环境变量而全部未设置
# --------------------------------------------------------------------------- #
def test_channel_with_all_credentials_unset_is_simply_incomplete(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """\
        storage:
          db_path: ./data/notify.db
        rules:
          path: ./rules.yaml
        channels:
          - id: mail
            type: email
            credentials:
              password: NOTIFY_A
              user: NOTIFY_B
              api_key: NOTIFY_C
        """,
    )

    settings = _load(path, env={})

    channel = settings.channels[0]
    assert channel.credentials_complete is False
    assert set(channel.credentials) == {"password", "user", "api_key"}
    assert all(value is None for value in channel.credentials.values())


# --------------------------------------------------------------------------- #
# 第 2 段默认值 / path 缺省解析（对应 conftest 的 tmp_settings 用法）
# --------------------------------------------------------------------------- #
def test_dataclass_defaults_match_spec() -> None:
    from notify_hub.config import ReminderSettings, Settings

    reminders = ReminderSettings()
    assert reminders.scan_interval_seconds == 60.0
    assert reminders.first_reminder_after_seconds == 1800.0
    assert reminders.reminder_interval_seconds == 3600.0

    fields = Settings.__dataclass_fields__
    assert fields["rules_poll_interval_seconds"].default == 5.0
    assert fields["host"].default == "127.0.0.1"
    assert fields["port"].default == 8000
    assert fields["default_channel"].default is None
    assert fields["channels"].default == ()
    assert fields["log_level"].default == "INFO"


def test_config_path_falls_back_to_env_var(tmp_path: Path, monkeypatch) -> None:
    path = _write_config(tmp_path, VALID_CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTIFY_HUB_CONFIG", str(path))

    settings = _load()

    assert settings.db_path == tmp_path.resolve() / "data" / "notify.db"
