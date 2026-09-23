"""M-B 模块测试：CLI 令牌支持（``add-public-access`` §7.1）。

依据（**唯一来源**）：``openspec/changes/add-public-access/architecture.md``
§7.1「模块 M-B：CLI 令牌支持」——第 2 段（接口）、第 3 段（内部实现）、
第 4 段（验证方法：主表 + 两条异常场景）。

**当前必然是红的**：``src/notify_hub/cli.py`` 里既没有 ``_resolve_token`` / ``_with_token``，
三个子命令也没有 ``--token`` 选项。因此本文件对 ``notify_hub.cli`` 的导入写在函数体内
（模块本身存在，收集期不会失败），红的形式是运行期 ``AttributeError`` 与「未知选项 -> 退出码 2」，
这正是「实现缺失」的证据而不是测试自身坏掉。

**驱动方式**：照 ``tests/test_cli.py`` 的既有接缝——用 ``typer.testing.CliRunner`` 驱动
冻结入口 ``cli.main``，并 monkeypatch 唯一测试接缝 ``build_client`` 挂
``httpx.MockTransport``。本文件**不修改** ``tests/test_cli.py``。

**只按查询参数断言**：守卫只接受 ``?token=`` 或会话 Cookie，**不读任何请求头**，
因此令牌必须出现在**查询串**里，本文件不对 ``Authorization`` 之类的请求头做任何断言。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Sequence

import httpx
import pytest
import typer
from typer.testing import CliRunner

DEFAULT_ENDPOINT = "http://127.0.0.1:8000"

#: 命令行令牌字面量：形状独特，便于在输出里做「出现 0 次」的断言。
TOKEN = "CLI-SECRET-TOKEN-9f3a2b"
#: 环境变量令牌字面量（与 :data:`TOKEN` 不同，用于验证优先级）。
ENV_TOKEN = "ENV-TOKEN-4c8d1e"


# --------------------------------------------------------------------------- #
# 被测模块的惰性访问
# --------------------------------------------------------------------------- #
def _load_cli():
    """惰性导入被测模块。"""
    import importlib

    return importlib.import_module("notify_hub.cli")


@pytest.fixture
def cli():
    """被测的 ``notify_hub.cli`` 模块。"""
    return _load_cli()


@pytest.fixture(autouse=True)
def _isolate_token_env(monkeypatch: pytest.MonkeyPatch):
    """默认不带 ``NOTIFY_HUB_TOKEN`` / ``NOTIFY_HUB_ENDPOINT``，避免宿主环境串进用例。"""
    monkeypatch.delenv("NOTIFY_HUB_TOKEN", raising=False)
    monkeypatch.delenv("NOTIFY_HUB_ENDPOINT", raising=False)


# --------------------------------------------------------------------------- #
# 假服务端：挂 MockTransport 的 httpx.Client（接缝与 tests/test_cli.py 同构）
# --------------------------------------------------------------------------- #
Responder = Callable[[httpx.Request], httpx.Response]


class _Stub:
    """记录 CLI 发出的请求，并按 ``responder`` 产出响应。"""

    def __init__(self, responder: Responder) -> None:
        self._responder = responder
        self.requests: list[httpx.Request] = []

    def build_client(self, *args: Any, **kwargs: Any) -> httpx.Client:
        """占据 ``notify_hub.cli.build_client`` 的位置（唯一的测试接缝）。"""
        endpoint = kwargs.get("endpoint", kwargs.get("base_url", args[0] if args else None))

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._responder(request)

        return httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url=endpoint or DEFAULT_ENDPOINT,
            timeout=5.0,
        )

    @property
    def request(self) -> httpx.Request:
        assert self.requests, "CLI 未向假服务端发出任何请求"
        return self.requests[-1]

    @property
    def json_body(self) -> Any:
        return json.loads(self.request.content.decode("utf-8"))


def _json(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _unauthorized() -> httpx.Response:
    """守卫未认证时的真实 JSON 分型（§3.2 第 7 步）。"""
    return _json({"detail": "unauthorized"}, status=401)


# --------------------------------------------------------------------------- #
# 运行器：CliRunner -> 冻结入口 main()
# --------------------------------------------------------------------------- #
def _entry_app() -> typer.Typer:
    """把 ``cli.main`` 包成一个最小 typer 命令，以便 CliRunner 驱动（照抄既有测试的写法）。"""
    entry = typer.Typer(add_completion=False, pretty_exceptions_enable=False)

    @entry.command(
        name="notify",
        add_help_option=False,
        no_args_is_help=False,
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    )
    def _run(ctx: typer.Context) -> None:
        cli_module = _load_cli()
        argv = list(ctx.args)
        saved_argv = sys.argv
        sys.argv = ["notify", *argv]
        try:
            cli_module.main()
        finally:
            sys.argv = saved_argv

    return entry


def _invoke(args: Sequence[str], *, cli: Any, monkeypatch: pytest.MonkeyPatch, stub: _Stub | None = None):
    if stub is not None:
        monkeypatch.setattr(cli, "build_client", stub.build_client)
    runner = CliRunner()
    return runner.invoke(_entry_app(), list(args), catch_exceptions=False)


def _all_output(result) -> str:
    return f"{result.stdout}\n{result.stderr}\n{getattr(result, 'output', '')}"


# --------------------------------------------------------------------------- #
# §7.1 第 2/4 段：_resolve_token 解析顺序与空白归一
# --------------------------------------------------------------------------- #
def test_resolve_token_precedence_flag_over_env_over_none(cli, monkeypatch) -> None:
    # 没有环境变量时：无参数 -> None；有参数 -> 参数本身。
    assert cli._resolve_token(None) is None
    assert cli._resolve_token(TOKEN) == TOKEN

    monkeypatch.setenv("NOTIFY_HUB_TOKEN", ENV_TOKEN)
    assert cli._resolve_token(None) == ENV_TOKEN, "无 --token 时必须回落到环境变量"
    assert cli._resolve_token(TOKEN) == TOKEN, "--token 必须优先于环境变量"


def test_resolve_token_blank_values_count_as_absent(cli, monkeypatch) -> None:
    """§7.1 第 2 段：取值 ``strip()`` 后为空则**视同未提供**（继续沿解析顺序回落）。"""
    assert cli._resolve_token("") is None
    assert cli._resolve_token("   ") is None
    assert cli._resolve_token("\t") is None

    # 空白的 --token 视同未提供 → 回落到环境变量。
    monkeypatch.setenv("NOTIFY_HUB_TOKEN", ENV_TOKEN)
    assert cli._resolve_token("   ") == ENV_TOKEN
    assert cli._resolve_token("") == ENV_TOKEN

    # 非空白但带首尾空白的取值：**视为已提供**，且 §7.1 第 2 段规定返回值一律 strip()。
    padded = cli._resolve_token(f"  {TOKEN}  ")
    assert padded == TOKEN


# --------------------------------------------------------------------------- #
# §7.1 第 2/4 段：_with_token 合并语义（不得就地修改入参）
# --------------------------------------------------------------------------- #
def test_with_token_none_returns_input_object_itself(cli) -> None:
    """``token is None`` → 原样返回入参对象本身（不复制、不包装）。"""
    assert cli._with_token(None, None) is None

    params = {"status": "all"}
    assert cli._with_token(params, None) is params, "未提供令牌时必须返回同一个对象（is）"


def test_with_token_returns_new_dict_and_leaves_input_untouched(cli) -> None:
    params = {"status": "all"}
    snapshot = dict(params)

    merged = cli._with_token(params, TOKEN)

    assert isinstance(merged, dict)
    assert merged is not params, "提供令牌时必须返回**新** dict"
    assert merged == {"status": "all", "token": TOKEN}
    assert params == snapshot, "入参 dict 不得被就地修改"
    assert "token" not in params


def test_with_token_with_none_params_yields_token_only_dict(cli) -> None:
    """``notify send`` → ``params=_with_token(None, token)``：无其它参数时也要能带上令牌。"""
    merged = cli._with_token(None, TOKEN)
    assert merged == {"token": TOKEN}


# --------------------------------------------------------------------------- #
# §7.1 第 4 段主表：端到端（查询串）行为
# --------------------------------------------------------------------------- #
def test_send_with_token_puts_token_in_query_and_keeps_body_identical(cli, monkeypatch) -> None:
    stub = _Stub(lambda request: _json({"message_id": 42}, status=202))
    args = ["--source", "backup", "--title", "备份完成"]

    plain = _invoke(args, cli=cli, monkeypatch=monkeypatch, stub=stub)
    assert plain.exit_code == 0, plain.stderr
    body_without_token = stub.json_body

    with_token = _invoke(
        [*args, "--token", TOKEN], cli=cli, monkeypatch=monkeypatch, stub=stub
    )
    assert with_token.exit_code == 0, with_token.stderr

    assert len(stub.requests) == 2
    bare, tokened = stub.requests
    assert bare.url.params.get("token") is None, "无令牌请求不得带 token 参数"
    assert tokened.url.params.get("token") == TOKEN, "令牌必须经查询参数传递（守卫不读请求头）"
    assert tokened.url.path == "/api/v1/messages"
    assert tokened.method == "POST"
    # 请求体与不带令牌时逐字节一致（令牌只进查询串）。
    assert stub.json_body == body_without_token
    assert "token" not in stub.json_body


def test_send_without_token_sends_no_token_anywhere(cli, monkeypatch) -> None:
    """向后兼容契约：未提供令牌时发出的请求与变更前等价。"""
    stub = _Stub(lambda request: _json({"message_id": 1}, status=202))

    result = _invoke(
        ["--source", "s", "--title", "t"], cli=cli, monkeypatch=monkeypatch, stub=stub
    )

    assert result.exit_code == 0, result.stderr
    request = stub.request
    assert "token" not in request.url.params, str(request.url)
    assert "token" not in request.url.query.decode("latin-1"), str(request.url)
    assert "token" not in stub.json_body


def test_todo_list_all_merges_status_and_token(cli, monkeypatch) -> None:
    stub = _Stub(lambda request: _json({"todos": [], "total": 0}))

    result = _invoke(
        ["todo", "list", "--all", "--token", TOKEN],
        cli=cli,
        monkeypatch=monkeypatch,
        stub=stub,
    )

    assert result.exit_code == 0, result.stderr
    request = stub.request
    assert request.url.path == "/api/v1/todos"
    assert request.url.params.get("status") == "all"
    assert request.url.params.get("token") == TOKEN


def test_todo_list_without_all_keeps_status_absent_with_token(cli, monkeypatch) -> None:
    """``--token`` 只做合并，不得改变 ``status`` 参数既有语义（默认不带 status）。"""
    stub = _Stub(lambda request: _json({"todos": [], "total": 0}))

    result = _invoke(
        ["todo", "list", "--token", TOKEN], cli=cli, monkeypatch=monkeypatch, stub=stub
    )

    assert result.exit_code == 0, result.stderr
    request = stub.request
    assert request.url.params.get("token") == TOKEN
    assert request.url.params.get("status") is None


def test_todo_done_sends_token_in_query(cli, monkeypatch) -> None:
    stub = _Stub(
        lambda request: _json(
            {"todo_id": 7, "status": "completed", "completed_at": "2024-01-01T00:10:00+00:00"}
        )
    )

    result = _invoke(
        ["todo", "done", "7", "--token", TOKEN],
        cli=cli,
        monkeypatch=monkeypatch,
        stub=stub,
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "done 7"
    request = stub.request
    assert request.method == "POST"
    assert request.url.path == "/api/v1/todos/7/done"
    assert request.url.params.get("token") == TOKEN


# --------------------------------------------------------------------------- #
# §7.1 第 4 段主表：环境变量
# --------------------------------------------------------------------------- #
def test_env_var_token_is_equivalent_to_flag(cli, monkeypatch) -> None:
    monkeypatch.setenv("NOTIFY_HUB_TOKEN", ENV_TOKEN)
    stub = _Stub(lambda request: _json({"message_id": 1}, status=202))

    result = _invoke(
        ["--source", "s", "--title", "t"], cli=cli, monkeypatch=monkeypatch, stub=stub
    )

    assert result.exit_code == 0, result.stderr
    assert stub.request.url.params.get("token") == ENV_TOKEN


def test_token_flag_overrides_env_var(cli, monkeypatch) -> None:
    monkeypatch.setenv("NOTIFY_HUB_TOKEN", ENV_TOKEN)
    stub = _Stub(lambda request: _json({"message_id": 1}, status=202))

    result = _invoke(
        ["--source", "s", "--title", "t", "--token", TOKEN],
        cli=cli,
        monkeypatch=monkeypatch,
        stub=stub,
    )

    assert result.exit_code == 0, result.stderr
    request = stub.request
    assert request.url.params.get("token") == TOKEN
    assert ENV_TOKEN not in str(request.url), "被覆盖的环境变量令牌不得进入请求"


# --------------------------------------------------------------------------- #
# §7.1 第 4 段：异常场景
# --------------------------------------------------------------------------- #
def test_exception_blank_env_var_token_is_absent_from_request(cli, monkeypatch) -> None:
    """异常场景②：环境变量为**空白串** → 视同未提供，请求不含 ``token``。"""
    monkeypatch.setenv("NOTIFY_HUB_TOKEN", "   ")
    stub = _Stub(lambda request: _json({"message_id": 1}, status=202))

    result = _invoke(
        ["--source", "s", "--title", "t"], cli=cli, monkeypatch=monkeypatch, stub=stub
    )

    assert result.exit_code == 0, result.stderr
    assert "token" not in stub.request.url.params, str(stub.request.url)


def test_exception_401_with_token_still_exit_1_and_no_false_success(cli, monkeypatch) -> None:
    """异常场景①：**已提供令牌**仍返回 401 → 不得谎报成功，退出码 1。

    §7.1 第 2 段：已提供令牌仍 401 时**不要求** ``--token`` 提示文案，
    因此这里只断言退出码与「没有成功输出」。
    """
    stub = _Stub(lambda request: _unauthorized())

    send = _invoke(
        ["--source", "s", "--title", "t", "--token", TOKEN],
        cli=cli,
        monkeypatch=monkeypatch,
        stub=stub,
    )
    assert send.exit_code == 1, send.stderr
    assert "message_id" not in send.stdout, "401 不得被渲染成成功"
    assert send.stderr.strip(), "错误必须写到 stderr"

    done = _invoke(
        ["todo", "done", "7", "--token", TOKEN],
        cli=cli,
        monkeypatch=monkeypatch,
        stub=stub,
    )
    assert done.exit_code == 1, done.stderr
    assert "done 7" not in done.stdout, "401 不得被渲染成成功"


# --------------------------------------------------------------------------- #
# §7.1 第 4 段主表：窄口径 401 提示
# --------------------------------------------------------------------------- #
def test_401_without_token_hints_flag_or_env_on_all_subcommands(cli, monkeypatch) -> None:
    stub = _Stub(lambda request: _unauthorized())

    send = _invoke(
        ["--source", "s", "--title", "t"], cli=cli, monkeypatch=monkeypatch, stub=stub
    )
    assert send.exit_code == 1, send.stderr
    assert "--token" in send.stderr or "NOTIFY_HUB_TOKEN" in send.stderr, (
        f"未提供令牌而 401 时，stderr 必须提示 --token 或 NOTIFY_HUB_TOKEN：{send.stderr!r}"
    )
    # 「除既有原因外」：既有原因（HTTP 状态）必须仍在，提示只能是追加。
    assert "401" in send.stderr, send.stderr

    listing = _invoke(
        ["todo", "list"], cli=cli, monkeypatch=monkeypatch, stub=stub
    )
    assert listing.exit_code == 1, listing.stderr
    assert "--token" in listing.stderr or "NOTIFY_HUB_TOKEN" in listing.stderr, (
        f"todo list 未提供令牌而 401 时同样必须提示：{listing.stderr!r}"
    )


# --------------------------------------------------------------------------- #
# §7.1 第 2 段：CLI 不得在任何输出里打印令牌
# --------------------------------------------------------------------------- #
def test_token_never_appears_in_command_output(cli, monkeypatch) -> None:
    ok = _Stub(lambda request: _json({"message_id": 42}, status=202))
    success = _invoke(
        ["--source", "s", "--title", "t", "--token", TOKEN],
        cli=cli,
        monkeypatch=monkeypatch,
        stub=ok,
    )
    assert success.exit_code == 0, success.stderr
    assert success.stdout.strip() == "message_id=42"
    assert TOKEN not in _all_output(success), "成功输出不得出现令牌明文"

    rejected = _Stub(lambda request: _unauthorized())
    failure = _invoke(
        ["--source", "s", "--title", "t", "--token", TOKEN],
        cli=cli,
        monkeypatch=monkeypatch,
        stub=rejected,
    )
    assert failure.exit_code == 1, failure.stderr
    assert TOKEN not in _all_output(failure), "错误输出不得出现令牌明文"


# --------------------------------------------------------------------------- #
# §7.1 第 3 段：--token 的 help 文案提醒（shell 历史 / 环境变量）
# --------------------------------------------------------------------------- #
def test_token_help_mentions_shell_history_and_env_var(cli, monkeypatch) -> None:
    for argv in (["--help"], ["todo", "list", "--help"], ["todo", "done", "--help"]):
        result = _invoke(argv, cli=cli, monkeypatch=monkeypatch)
        assert result.exit_code == 0, result.stderr
        text = result.stdout
        assert "--token" in text, f"{argv} 的 help 未列出 --token：{text!r}"
        assert "历史" in text, f"{argv} 的 help 未提醒命令行传参会进 shell 历史：{text!r}"
        assert "NOTIFY_HUB_TOKEN" in text, (
            f"{argv} 的 help 未出现环境变量名 NOTIFY_HUB_TOKEN：{text!r}"
        )


# --------------------------------------------------------------------------- #
# 说明（不在断言内）：
# §7.1 第 2 段规定「取值 ``strip()`` 后为空则视同未提供」，并**明确规定返回值一律 strip()**：
# ``_resolve_token(" T ")`` 必须返回 ``"T"``，与 ``_resolve_token("T")`` 完全等价。
# --------------------------------------------------------------------------- #
