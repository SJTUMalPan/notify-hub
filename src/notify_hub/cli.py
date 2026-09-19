"""M5：命令行客户端 ``notify``。

规格来源：``openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md`` 第 6 节。
本模块是一个**独立客户端**：只依赖 ``typer`` / ``httpx`` / 标准库，不导入本项目其它模块，
不做任何客户端侧的请求体校验（click 的必填与枚举约束除外），原样交给服务端裁决。
"""

from __future__ import annotations

import enum
import os
import sys
from typing import Any, Optional

import httpx
import typer

DEFAULT_ENDPOINT = "http://127.0.0.1:8000"

MESSAGES_PATH = "/api/v1/messages"
TODOS_PATH = "/api/v1/todos"

#: ``--token`` 的 help 文案（冻结：必须同时出现 ``NOTIFY_HUB_TOKEN`` 与「历史」字样）。
TOKEN_HELP = (
    "访问令牌（命令行传参会留在 shell 历史中，长期使用建议改用环境变量 NOTIFY_HUB_TOKEN）"
)


class CliError(Exception):
    """带冻结退出码的客户端错误。``main()`` 负责渲染到 stderr 并退出。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Level(str, enum.Enum):
    info = "info"
    warning = "warning"
    error = "error"


def build_client(endpoint: str) -> httpx.Client:
    """唯一的测试接缝：测试 monkeypatch 本函数返回挂 MockTransport 的 Client。"""
    return httpx.Client(base_url=endpoint, timeout=10.0)


app = typer.Typer(
    add_completion=False,
    pretty_exceptions_enable=False,
    no_args_is_help=False,
)
todo_app = typer.Typer(
    add_completion=False,
    pretty_exceptions_enable=False,
    no_args_is_help=False,
)
app.add_typer(todo_app, name="todo")


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _emit_error(message: str) -> None:
    print(f"错误: {message}", file=sys.stderr)


def _resolve_endpoint(endpoint: Optional[str]) -> str:
    """解析顺序：命令行参数 > 环境变量 > 默认回环地址。"""
    return endpoint or os.environ.get("NOTIFY_HUB_ENDPOINT") or DEFAULT_ENDPOINT


def _resolve_token(token: Optional[str]) -> Optional[str]:
    """解析顺序：``--token`` 参数 > 环境变量 ``NOTIFY_HUB_TOKEN`` > ``None``。

    取值一律 ``strip()``：去空白后为空视同**未提供**（继续沿解析顺序回落）；
    否则返回 strip 之后的值。令牌会被拿去与守卫做常量时间比较，因此这里比
    ``_resolve_endpoint`` 更严，不保留首尾空白。
    """
    for candidate in (token, os.environ.get("NOTIFY_HUB_TOKEN")):
        if candidate is None:
            continue
        stripped = candidate.strip()
        if stripped:
            return stripped
    return None


def _with_token(
    params: Optional[dict[str, Any]], token: Optional[str]
) -> Optional[dict[str, Any]]:
    """把令牌并入查询参数。

    ``token is None`` → 原样返回入参对象本身（不复制、不包装）；
    否则返回**新** dict，含原有全部键并加上 ``"token"``。任何情况下都不就地修改入参。
    """
    if token is None:
        return params
    merged = dict(params) if params is not None else {}
    merged["token"] = token
    return merged


def _describe_rejection(resp: httpx.Response) -> str:
    """把服务端错误响应压成一行可读原因（含字段级原因）。"""
    payload: Any = None
    try:
        payload = resp.json()
    except Exception:
        payload = None

    reason = ""
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, list) and detail:
            parts = []
            for item in detail:
                if isinstance(item, dict):
                    loc = item.get("loc")
                    field = ""
                    if isinstance(loc, (list, tuple)):
                        field = ".".join(
                            str(part) for part in loc if str(part) != "body"
                        )
                    msg = str(item.get("msg", "")).strip()
                    if field and msg:
                        parts.append(f"{field}: {msg}")
                    elif field:
                        parts.append(field)
                    elif msg:
                        parts.append(msg)
                else:
                    parts.append(str(item))
            reason = "; ".join(part for part in parts if part)
        elif isinstance(detail, str):
            reason = detail
        else:
            for key in ("message", "error", "title"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    reason = value
                    break
    elif isinstance(payload, str):
        reason = payload

    head = f"服务端返回 HTTP {resp.status_code}"
    return f"{head}: {reason}" if reason else head


def _request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    token_missing: bool = False,
    **kwargs: Any,
) -> httpx.Response:
    """发请求并统一映射网络/HTTP 失败到冻结退出码。

    ``token_missing`` 仅在「本次未提供令牌」时置真：此时若服务端返回 401，
    在既有原因之外追加一行 ``--token`` / ``NOTIFY_HUB_TOKEN`` 提示（窄口径）。
    未提供令牌且非 401 时错误信息与变更前逐字一致。
    """
    try:
        resp = client.request(method, url, **kwargs)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise CliError(3, f"无法连接服务端: {exc}") from exc
    except httpx.TransportError as exc:
        raise CliError(3, f"无法连接服务端: {exc}") from exc
    if resp.status_code >= 400:
        message = _describe_rejection(resp)
        if resp.status_code == 401 and token_missing:
            message = (
                f"{message}（本次未提供访问令牌，可用 --token 或环境变量 "
                "NOTIFY_HUB_TOKEN 指定）"
            )
        raise CliError(1, message)
    return resp


def _json_object(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except Exception as exc:
        raise CliError(1, f"服务端返回格式无法识别: {exc}") from exc
    if not isinstance(data, dict):
        raise CliError(1, "服务端返回格式无法识别: 期望 JSON 对象")
    return data


def _format_duration(seconds: Any) -> str:
    """客户端自带的超时时长格式化（``Xd Yh Zm``），不依赖其它模块。"""
    try:
        total = int(max(0.0, float(seconds)))
    except (TypeError, ValueError) as exc:
        raise CliError(1, f"服务端返回格式无法识别: overdue_seconds 非法 ({seconds!r})") from exc
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    return f"{days}d {hours}h {minutes}m"


# --------------------------------------------------------------------------- #
# 根命令：投递
# --------------------------------------------------------------------------- #
@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    source: Optional[str] = typer.Option(None, "--source", help="消息来源"),
    title: Optional[str] = typer.Option(None, "--title", help="消息标题"),
    body: Optional[str] = typer.Option(None, "--body", help="消息正文"),
    body_stdin: bool = typer.Option(False, "--body-stdin", help="从 stdin 读取正文"),
    level: Level = typer.Option(Level.info, "--level", help="消息级别"),
    need_ack: bool = typer.Option(False, "--need-ack", help="是否需要确认"),
    dedup_key: Optional[str] = typer.Option(None, "--dedup-key", help="去重键"),
    endpoint: Optional[str] = typer.Option(None, "--endpoint", help="服务端地址"),
    token: Optional[str] = typer.Option(None, "--token", help=TOKEN_HELP),
) -> None:
    if ctx.invoked_subcommand is not None:
        return

    # 只做 click 层面的「必填/互斥」约束；正文内容一律交给服务端裁决。
    if source is None:
        raise typer.BadParameter("缺少必填选项 --source", param_hint="--source")
    if body is not None and body_stdin:
        raise typer.BadParameter(
            "--body 与 --body-stdin 不能同时使用", param_hint="--body/--body-stdin"
        )

    if body_stdin:
        content = sys.stdin.read()
    else:
        content = body

    payload: dict[str, Any] = {
        "source": source,
        "level": level.value,
        "need_ack": need_ack,
    }
    if title is not None:
        payload["title"] = title
    if content is not None:
        payload["body"] = content
    if dedup_key is not None:
        payload["dedup_key"] = dedup_key

    client = build_client(_resolve_endpoint(endpoint))
    resolved_token = _resolve_token(token)
    params = _with_token(None, resolved_token)
    try:
        resp = _request(
            client,
            "POST",
            MESSAGES_PATH,
            token_missing=resolved_token is None,
            params=params,
            json=payload,
        )
    finally:
        client.close()

    data = _json_object(resp)
    if "message_id" not in data:
        raise CliError(1, "服务端返回格式无法识别: 响应缺少 message_id")
    typer.echo(f"message_id={data['message_id']}")


# --------------------------------------------------------------------------- #
# todo 子命令
# --------------------------------------------------------------------------- #
@todo_app.command("list")
def todo_list(
    all_: bool = typer.Option(False, "--all", help="包含已完成待办"),
    endpoint: Optional[str] = typer.Option(None, "--endpoint", help="服务端地址"),
    token: Optional[str] = typer.Option(None, "--token", help=TOKEN_HELP),
) -> None:
    resolved_token = _resolve_token(token)
    params = _with_token({"status": "all"} if all_ else None, resolved_token)

    client = build_client(_resolve_endpoint(endpoint))
    try:
        resp = _request(
            client,
            "GET",
            TODOS_PATH,
            token_missing=resolved_token is None,
            params=params,
        )
    finally:
        client.close()

    data = _json_object(resp)
    todos = data.get("todos")
    if not isinstance(todos, list):
        raise CliError(1, "服务端返回格式无法识别: 响应缺少 todos 数组")

    # 顺序完全保持服务端给出的顺序（排序是服务端职责，客户端不得重排）。
    for todo in todos:
        if not isinstance(todo, dict):
            raise CliError(1, "服务端返回格式无法识别: todos 元素不是对象")
        for field in ("id", "overdue_seconds", "source", "title"):
            if field not in todo:
                raise CliError(
                    1, f"服务端返回格式无法识别: todo 缺少字段 {field}"
                )
        row = [
            str(todo["id"]),
            _format_duration(todo["overdue_seconds"]),
            str(todo["source"]),
            str(todo["title"]),
        ]
        if all_:
            row.insert(0, str(todo.get("status", "")))
        typer.echo("\t".join(row))


@todo_app.command("done")
def todo_done(
    todo_id: int = typer.Argument(..., help="待办 ID"),
    endpoint: Optional[str] = typer.Option(None, "--endpoint", help="服务端地址"),
    token: Optional[str] = typer.Option(None, "--token", help=TOKEN_HELP),
) -> None:
    resolved_token = _resolve_token(token)
    client = build_client(_resolve_endpoint(endpoint))
    try:
        resp = _request(
            client,
            "POST",
            f"{TODOS_PATH}/{todo_id}/done",
            token_missing=resolved_token is None,
            params=_with_token(None, resolved_token),
        )
    finally:
        client.close()

    _json_object(resp)
    typer.echo(f"done {todo_id}")


# --------------------------------------------------------------------------- #
# 进程入口
# --------------------------------------------------------------------------- #
def main() -> None:
    """console script 入口：渲染错误到 stderr 并返回冻结退出码。"""
    try:
        app(args=sys.argv[1:], standalone_mode=False)
    except CliError as exc:
        _emit_error(exc.message)
        sys.exit(exc.code)
    except typer.Exit as exc:
        sys.exit(exc.exit_code)
    except typer.Abort:
        _emit_error("已中止")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - typer/click 用法错误
        code = getattr(exc, "exit_code", None)
        if code is None:
            _emit_error(f"客户端内部错误: {exc}")
            sys.exit(1)
        formatter = getattr(exc, "format_message", None)
        message = formatter() if callable(formatter) else str(exc)
        _emit_error(message)
        sys.exit(code)


if __name__ == "__main__":
    main()
