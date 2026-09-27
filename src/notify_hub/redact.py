"""脱敏原语：把凭据从文本、URL、结构化数据与异常中抹掉。

见 ``openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md`` 第 6 节「模块 M1」第 2 段。

安全约束：本模块的输出对同一输入是确定性的，且 **不包含任何输入 secret 的完整值**。
``redact_url`` 的第 1、2 条规则是**无条件**生效的——即使调用方没有传 ``secrets``，
也必须掩码 userinfo 密码与敏感 query 参数。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "MASK",
    "SENSITIVE_KEYS",
    "redact_text",
    "redact_url",
    "redact_urls_in_text",
    "extract_url_secrets",
    "redact_mapping",
    "redact_exception",
]

MASK = "***"

SENSITIVE_KEYS = (
    "token",
    "password",
    "passwd",
    "secret",
    "sign",
    "key",
    "access_token",
    "api_key",
    "authorization",
)

#: 匹配 query 中的 ``key=value`` 片段，保留原分隔符与顺序（不重新编码）。
_QUERY_PAIR_RE = re.compile(r"(^|&)([^=&]*)=([^&]*)")

#: 路径片段只有在**整段**形如不透明令牌时才可被当作密钥（仅 ``[A-Za-z0-9_-]``）。
_PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+")


def _is_sensitive_key(key: str) -> bool:
    """键名命中敏感集合（大小写不敏感，含后缀匹配，如 ``access_token``）。"""
    lowered = key.lower()
    return any(lowered.endswith(sensitive) for sensitive in SENSITIVE_KEYS)


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """把 ``secrets`` 中每个非空值在 ``text`` 中的所有出现替换为 :data:`MASK`。"""
    if text is None:  # 容忍 None，调用方多为日志路径
        return text
    result = str(text)
    for secret in secrets:
        if not secret:
            continue
        result = result.replace(str(secret), MASK)
    return result


def _mask_userinfo(netloc: str) -> str:
    """规则 1：只掩码 userinfo 的密码部分，保留用户名；无密码则不动。"""
    if "@" not in netloc:
        return netloc
    userinfo, _, host = netloc.rpartition("@")
    if ":" in userinfo:
        user, _, password = userinfo.partition(":")
        if password:
            userinfo = f"{user}:{MASK}"
    return f"{userinfo}@{host}"


def _mask_query(query: str) -> str:
    """规则 2：键名命中 :data:`SENSITIVE_KEYS` 的 query 值替换为 :data:`MASK`。"""

    def _replace(match: re.Match[str]) -> str:
        separator, key, _value = match.group(1), match.group(2), match.group(3)
        if _is_sensitive_key(key):
            return f"{separator}{key}={MASK}"
        return match.group(0)

    return _QUERY_PAIR_RE.sub(_replace, query)


def redact_url(url: str, secrets: Iterable[str] = ()) -> str:
    """掩码 URL 中的凭据，三条规则全部生效（第 1、2 条无条件）。"""
    secrets = tuple(secrets)
    raw = "" if url is None else str(url)

    try:
        parts = urlsplit(raw)
    except ValueError:
        return redact_text(raw, secrets)

    if not parts.scheme and not parts.netloc:
        # 不是 URL（相对路径/纯文本），只做显式密钥子串替换。
        return redact_text(raw, secrets)

    rebuilt = urlunsplit(
        (
            parts.scheme,
            _mask_userinfo(parts.netloc),
            parts.path,
            _mask_query(parts.query),
            parts.fragment,
        )
    )
    # 规则 3：叠加调用方显式给出的密钥子串。
    return redact_text(rebuilt, secrets)


def extract_url_secrets(
    url: str, *, min_length: int = 6, path_min_length: int = 16
) -> tuple[str, ...]:
    """提取 URL 中应被视为**独立密钥**的成分（供 ``credential_values`` 展开用）。

    提取三处：query 中键名命中 :data:`SENSITIVE_KEYS` 的值（大小写不敏感、含后缀匹配）、
    userinfo 的密码部分，以及**路径中每一段**满足「长度 ≥ ``path_min_length`` 且仅由
    ``[A-Za-z0-9_-]`` 组成」的片段（按百分号解码前的原样判断；空段与 ``/`` 不算片段）。
    判定「是否是 URL」以 ``urlsplit(url).scheme`` 非空为准：裸 query 串
    （如 ``"?ACCESS_TOKEN=abcdefgh"``）不是 URL，返回空元组，不抛异常。

    ``min_length=6`` 是刻意的安全阀：``SENSITIVE_KEYS`` 含通用键名 ``key``，若把
    ``?key=1`` 的值 ``"1"`` 收进全局密钥集合，``redact_text`` 会把日志里所有出现的
    ``1`` 都打成 :data:`MASK`。长度不足的成分不进入全局密钥集合（它们仍由
    :func:`redact_url` 在 URL 内部按键名掩码，因此不会暴露）。

    ``path_min_length=16`` 是更高的一道安全阀，理由同上但更严：很多平台把凭据放在
    **URL 路径末段**而不是 query——飞书 ``…/hook/<token>``、Slack ``…/services/T/B/<token>``、
    Discord ``/api/webhooks/<id>/<token>``。这些片段普遍 ≥ 24 字符，而普通路径段
    （``status``、``api``、``hook``、``send``）都很短。若沿用 6 的门槛，
    ``https://h/status`` 会把 ``status`` 收进全局密钥集合，把日志里所有 ``status``
    打成 :data:`MASK`。取 16 在「覆盖真实 token」与「不误伤普通路径」之间取安全的一侧；
    字符集限制同理：只收形如不透明令牌的片段。结果去重、剔除空串。
    """
    raw = "" if url is None else str(url)
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ()
    if not parts.scheme:
        return ()

    found: dict[str, None] = {}

    netloc = parts.netloc
    if "@" in netloc:
        userinfo, _, _host = netloc.rpartition("@")
        if ":" in userinfo:
            _user, _, password = userinfo.partition(":")
            if password and len(password) >= min_length:
                found.setdefault(password, None)

    for match in _QUERY_PAIR_RE.finditer(parts.query):
        key, value = match.group(2), match.group(3)
        if _is_sensitive_key(key) and value and len(value) >= min_length:
            found.setdefault(value, None)

    # 路径末段凭据：平台把 webhook token 放在路径里（飞书/Slack/Discord），
    # 平台错误 msg 回显的是裸 token，必须作为独立密钥才能被 redact_text 匹配。
    for segment in parts.path.split("/"):
        if (
            len(segment) >= path_min_length
            and _PATH_TOKEN_RE.fullmatch(segment)
        ):
            found.setdefault(segment, None)

    return tuple(found)


def _redact_object(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, Mapping):
        return {
            key: (MASK if isinstance(key, str) and _is_sensitive_key(key)
                  else _redact_object(item, secrets))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_object(item, secrets) for item in value]
    if isinstance(value, str):
        return redact_text(value, secrets)
    return value


def redact_mapping(data: Mapping[str, Any], secrets: Iterable[str] = ()) -> dict[str, Any]:
    """递归（dict/list/str）脱敏；键名命中 :data:`SENSITIVE_KEYS` 时整值替换为 :data:`MASK`。"""
    result = _redact_object(data, tuple(secrets))
    if isinstance(result, dict):
        return result
    return {"value": result}


def redact_exception(exc: BaseException, secrets: Iterable[str] = ()) -> str:
    """把异常渲染成 ``'<类型>: <脱敏后的消息>'``。"""
    return f"{type(exc).__name__}: {redact_text(str(exc), secrets)}"


#: 自由文本中形如 URL 的片段（用于对 traceback 这类**非结构化文本**施加 URL 规则）。
_URL_IN_TEXT_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s\"'<>\\]+")

#: 自由文本中的 ``?key=value`` / ``&key=value``（覆盖没有 scheme 的裸 query 串）。
_BARE_QUERY_PAIR_RE = re.compile(r"([?&])([^=&?\s]+)=([^&\s]*)")


def redact_urls_in_text(text: str, secrets: Iterable[str] = ()) -> str:
    """对**任意文本**施加 :func:`redact_url` 的无条件规则（第 1、2 条）。

    专为日志自由文本设计：``logger.exception`` 渲染出的 traceback 里嵌着完整 URL，
    而调用方往往不知道凭据是什么（凭据由环境变量展开），因此这里**不能**只依赖
    ``secrets``——URL 的 userinfo 密码与敏感 query 值必须无条件掩码。
    """
    if text is None:
        return text
    secrets = tuple(secrets)

    def _mask_url(match: re.Match[str]) -> str:
        return redact_url(match.group(0), secrets)

    result = _URL_IN_TEXT_RE.sub(_mask_url, str(text))

    def _mask_pair(match: re.Match[str]) -> str:
        separator, key, _value = match.group(1), match.group(2), match.group(3)
        if _is_sensitive_key(key):
            return f"{separator}{key}={MASK}"
        return match.group(0)

    result = _BARE_QUERY_PAIR_RE.sub(_mask_pair, result)
    return redact_text(result, secrets)
