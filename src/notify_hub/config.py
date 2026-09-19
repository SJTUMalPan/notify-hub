"""配置加载：把「服务怎么配、凭据从哪来」收敛到一处。

见 ``openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md`` 第 6 节「模块 M1」第 2/3 段。

依赖约束：仅标准库 + ``pyyaml`` + 阶段 0 的 ``errors.ConfigurationError``，以及同属模块 M1 的
``redact.extract_url_secrets``。**不得**导入本项目其它模块。

安全约束：任何 :class:`ConfigurationError` 的消息中 MUST NOT 出现凭据值。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from notify_hub.errors import ConfigurationError
from notify_hub.redact import extract_url_secrets

__all__ = [
    "ChannelSpec",
    "ReminderSettings",
    "Settings",
    "load_settings",
    "credential_values",
]


@dataclass(frozen=True)
class ChannelSpec:
    """单个通知渠道实例的配置。"""

    id: str
    type: str
    enabled: bool = True
    params: Mapping[str, Any] = field(default_factory=dict)
    credentials: Mapping[str, str | None] = field(default_factory=dict)

    @property
    def credentials_complete(self) -> bool:
        """所有声明的凭据都已解析出非空值。

        凭据缺失 = 渠道不可用，**不是**启动错误。
        """
        return all(bool(value) for value in self.credentials.values())


@dataclass(frozen=True)
class ReminderSettings:
    """每日汇总提醒参数（合法关系在 :func:`load_settings` 中校验）。

    ``at`` 是本地时刻 ``HH:MM``，``timezone`` 是 IANA 时区名；二者共同决定每天
    发出汇总的时刻。旧模型的两个间隔键已被**整体移除**，出现即报错。
    """

    at: str = "21:00"
    timezone: str = "Asia/Shanghai"
    scan_interval_seconds: float = 60.0

    @property
    def zone(self) -> ZoneInfo:
        """返回配置时区。加载失败不应发生（``load_settings`` 已校验）。"""
        return ZoneInfo(self.timezone)

    @property
    def trigger_time(self) -> time:
        """返回 ``datetime.time(hh, mm)``，无 tzinfo。"""
        hour, minute = self.at.split(":")
        return time(int(hour), int(minute))


@dataclass(frozen=True)
class Settings:
    """服务运行期配置。路径类字段与数值类字段永不为 None。

    ``auth_token`` 是 add-public-access 引入的访问令牌：``None`` 表示**不启用鉴权**
    （刻意的 fail-open 默认，见该变更 ``design.md`` 的 D4）；非 None 时全路由生效。
    """

    db_path: Path
    rules_path: Path
    rules_poll_interval_seconds: float = 5.0
    host: str = "127.0.0.1"
    port: int = 8000
    default_channel: str | None = None
    channels: tuple[ChannelSpec, ...] = ()
    reminders: ReminderSettings = ReminderSettings()
    log_level: str = "INFO"
    auth_token: str | None = None


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #
#: add-daily-digest：已被整体移除的单项超时提醒键。**只在 ``reminders`` 段内检查**；
#: 出现即报错（键存在即算出现，值为 YAML null 也不例外），不得静默忽略。
_REMOVED_REMINDER_KEYS = (
    "first_reminder_after_seconds",
    "reminder_interval_seconds",
)

#: ``reminders.at`` 的形状：两位小时 + 冒号 + 两位分钟。
_AT_PATTERN = re.compile(r"^\d{2}:\d{2}$")

#: ``reminders.scan_interval_seconds`` 的合法上界（含）。
_SCAN_INTERVAL_MAX = 3600.0


def _as_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigurationError(
            f"{label} 必须是映射（mapping），实际为 {type(value).__name__}"
        )
    return value


def _require(mapping: Mapping[str, Any], key: str, label: str) -> Any:
    value = mapping.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ConfigurationError(f"缺少必填配置键: {label}")
    return value


def _as_float(value: Any, label: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{label} 必须是数字，实际为 {value!r}") from exc


def _as_int(value: Any, label: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{label} 必须是整数，实际为 {value!r}") from exc


def _resolve_path(value: Any, base: Path, label: str) -> Path:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ConfigurationError(f"缺少必填配置键: {label}")
    candidate = Path(str(value)).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _parse_channels(raw: Any, env: Mapping[str, str]) -> tuple[ChannelSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ConfigurationError("channels 必须是列表")

    seen: set[str] = set()
    channels: list[ChannelSpec] = []
    for index, item in enumerate(raw):
        entry = _as_mapping(item, f"channels[{index}]")
        channel_id = entry.get("id")
        if not channel_id or not isinstance(channel_id, str):
            raise ConfigurationError(f"channels[{index}].id 缺失或不是字符串")
        if channel_id in seen:
            raise ConfigurationError(f"channels 中存在重复的渠道 id: {channel_id}")
        seen.add(channel_id)

        channel_type = entry.get("type")
        if not channel_type or not isinstance(channel_type, str):
            raise ConfigurationError(f"channels[{index}].type 缺失或不是字符串")

        params = dict(_as_mapping(entry.get("params"), f"channels[{index}].params"))
        raw_credentials = _as_mapping(
            entry.get("credentials"), f"channels[{index}].credentials"
        )
        credentials: dict[str, str | None] = {}
        for logical_name, env_name in raw_credentials.items():
            if env_name is None:
                credentials[str(logical_name)] = None
                continue
            resolved = env.get(str(env_name))
            credentials[str(logical_name)] = resolved if resolved else None

        channels.append(
            ChannelSpec(
                id=channel_id,
                type=channel_type,
                enabled=bool(entry.get("enabled", True)),
                params=params,
                credentials=credentials,
            )
        )
    return tuple(channels)


def _parse_reminders(raw: Any) -> ReminderSettings:
    section = _as_mapping(raw, "reminders")

    # 旧键检查范围：只在 reminders 段内；判据是「键存在」，与值无关。
    for removed_key in _REMOVED_REMINDER_KEYS:
        if removed_key in section:
            raise ConfigurationError(
                f"reminders.{removed_key} 已移除（每日汇总提醒不再支持该项）；"
                "请改用 reminders.at 与 reminders.timezone"
            )

    at_raw = section.get("at", ReminderSettings.at)
    at_value = str(at_raw)
    if not _AT_PATTERN.match(at_value):
        raise ConfigurationError(
            f"reminders.at 必须是 HH:MM 格式的本地时刻，实际为 {at_raw!r}"
        )
    hour, minute = (int(part) for part in at_value.split(":"))
    if hour > 23 or minute > 59:
        raise ConfigurationError(f"reminders.at 不是合法时刻，实际为 {at_raw!r}")

    timezone_raw = section.get("timezone", ReminderSettings.timezone)
    timezone_value = str(timezone_raw)
    try:
        ZoneInfo(timezone_value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigurationError(
            f"reminders.timezone 不是可加载的 IANA 时区名，实际为 {timezone_raw!r}"
        ) from exc

    scan_interval = _as_float(
        section.get("scan_interval_seconds", ReminderSettings.scan_interval_seconds),
        "reminders.scan_interval_seconds",
    )
    if scan_interval <= 0 or scan_interval > _SCAN_INTERVAL_MAX:
        raise ConfigurationError(
            "reminders.scan_interval_seconds 必须 > 0 且 <= 3600，"
            f"实际为 {scan_interval!r}"
        )

    return ReminderSettings(
        at=at_value,
        timezone=timezone_value,
        scan_interval_seconds=scan_interval,
    )


def _read_config_file(config_path: Path) -> Mapping[str, Any]:
    if not config_path.exists():
        raise ConfigurationError(f"配置文件不存在: {config_path}")
    if not config_path.is_file():
        raise ConfigurationError(f"配置路径不是文件: {config_path}")

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(
            f"无法读取配置文件 {config_path.name}: {type(exc).__name__}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise ConfigurationError(
            f"无法解码配置文件 {config_path.name}: {type(exc).__name__}"
        ) from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark is not None else "?"
        raise ConfigurationError(
            f"{config_path.name}: YAML 解析失败 line {line}"
            f" ({type(exc).__name__})"
        ) from exc

    if data is None:
        raise ConfigurationError(f"配置文件为空: {config_path.name}")
    if not isinstance(data, Mapping):
        raise ConfigurationError(
            f"配置文件 {config_path.name} 的顶层必须是映射（mapping），"
            f"实际为 {type(data).__name__}"
        )
    return data


# --------------------------------------------------------------------------- #
# 公开接口
# --------------------------------------------------------------------------- #
def load_settings(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Settings:
    """从 YAML 加载配置。

    ``path`` 为 None 时取环境变量 ``NOTIFY_HUB_CONFIG``，再退回 ``./config.yaml``。
    ``env`` 为 None 时取 ``os.environ``（测试注入用）。
    """
    env_map: Mapping[str, str] = os.environ if env is None else env

    if path is None:
        path = env_map.get("NOTIFY_HUB_CONFIG") or "./config.yaml"

    config_path = Path(path).expanduser()
    if not config_path.is_absolute():
        config_path = config_path.resolve()
    else:
        config_path = config_path.resolve()
    base_dir = config_path.parent

    data = _read_config_file(config_path)

    server = _as_mapping(data.get("server"), "server")
    storage = _as_mapping(data.get("storage"), "storage")
    rules = _as_mapping(data.get("rules"), "rules")

    db_path = _resolve_path(storage.get("db_path"), base_dir, "storage.db_path")
    rules_path = _resolve_path(rules.get("path"), base_dir, "rules.path")

    channels = _parse_channels(data.get("channels"), env_map)

    default_channel_raw = data.get("default_channel")
    default_channel: str | None = None
    if default_channel_raw is not None:
        default_channel = str(default_channel_raw)
        known_ids = [channel.id for channel in channels]
        if default_channel not in known_ids:
            raise ConfigurationError(
                f"default_channel='{default_channel}' 不在 channels 列表中: {known_ids}"
            )

    reminders = _parse_reminders(data.get("reminders"))

    auth_token = _parse_auth_token(server)

    return Settings(
        db_path=db_path,
        rules_path=rules_path,
        rules_poll_interval_seconds=_as_float(
            rules.get("poll_interval_seconds", 5.0),
            "rules.poll_interval_seconds",
        ),
        host=str(server.get("host", "127.0.0.1")),
        port=_as_int(server.get("port", 8000), "server.port"),
        default_channel=default_channel,
        channels=channels,
        reminders=reminders,
        log_level=str(server.get("log_level", "INFO")),
        auth_token=auth_token,
    )


def _parse_auth_token(server: Mapping[str, Any]) -> str | None:
    """解析 ``server.auth_token``（add-public-access）。

    契约（架构师冻结，见该变更 ``architecture.md`` 第 2 节）：

    - 键缺失、或值为 YAML ``null`` → 返回 ``None``＝不启用鉴权
    - 值必须是**去掉首尾空白后仍非空**的字符串，否则抛 ``ConfigurationError``
    - 返回值是 ``strip()`` 之后的结果（前后空白不参与比对，避免难以排查的「看起来一样却不通过」）
    """
    raw = server.get("auth_token")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ConfigurationError(
            f"server.auth_token 必须是字符串，实际是 {type(raw).__name__}"
            "（缺省或显式 null 表示不启用访问鉴权）"
        )
    token = raw.strip()
    if not token:
        raise ConfigurationError(
            "server.auth_token 不能是空白字符串（缺省或显式 null 表示不启用访问鉴权）"
        )
    return token


def credential_values(settings: Settings) -> tuple[str, ...]:
    """返回用于日志/记录脱敏的全部密钥字面量（含 URL 内嵌凭据成分与访问令牌）。

    对每个渠道的每个已解析凭据值 ``v``：先加入 ``v`` 本身（非空时）；若 ``v`` 形如 URL，
    再按 :func:`redact.extract_url_secrets` **展开**，把内嵌凭据成分也作为独立密钥加入。
    最后并入 ``settings.auth_token``（非空时）。全局去重，剔除 None 与空串；顺序不作保证。

    展开是修复一个已确认 P1 泄漏的关键：webhook 渠道的凭据值往往是「完整 URL」，而平台
    错误信息里回显的是 URL 中的**裸 token**；``redact_text`` 做子串匹配，只登记整段 URL
    时文本里的裸 token 匹配不上，明文便会落进投递记录并外泄。

    并入 ``auth_token`` 是 add-public-access 的 D8：访问令牌出现在请求 URL 的查询串里，
    除了 ``redact_text`` 的 ``token`` 键名规则之外，还需要字面量级别的兜底——令牌一旦以
    其他形式（记录正文、异常消息）出现，也必须命中。
    """
    values: dict[str, None] = {}
    for channel in settings.channels:
        for value in channel.credentials.values():
            if not value:
                continue
            values.setdefault(value, None)
            for embedded in extract_url_secrets(value):
                values.setdefault(embedded, None)

    if settings.auth_token:
        values.setdefault(settings.auth_token, None)

    return tuple(values)
