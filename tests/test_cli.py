"""M5 命令行客户端（``notify``）的模块测试。

规格来源：``openspec/changes/archive/2026-09-18-add-notify-hub/architecture.md``
第 6 节「模块 M5：命令行客户端」第 2 段（冻结命令面 / 退出码 0-1-2-3 / 输出契约）
与第 4 段（测试规格第 1–10 条）。

**当前必然是红的**：``src/notify_hub/cli.py`` 尚不存在。因此本文件对 ``notify_hub.cli``
的所有导入都写在函数体内——顶部导入会让 pytest 在**收集阶段**失败，那样连「实现缺失」
这个事实都测不出来。正确的红是：收集成功、每个用例在运行期以
``ModuleNotFoundError: No module named 'notify_hub.cli'`` 失败。

**调用方式**：用 ``typer.testing.CliRunner`` 驱动*冻结的进程入口* ``cli.main``
（``pyproject.toml`` 的 ``[project.scripts] notify = notify_hub.cli:main``），
而不是直接 invoke ``cli.app``。理由：规格第 3 节把错误渲染放在 ``main()``
（「捕获 ``typer.Exit`` 与 ``CliError``；写 stderr 后 ``sys.exit(code)``」），
直接 invoke ``app`` 在那种实现下看不到 stderr、也拿不到退出码 3。
从入口驱动则对两种实现（错误在子命令内渲染 / 错误由 ``main()`` 渲染）都成立，
断言对象始终是「命令 ``notify`` 的退出码 + stdout/stderr」这一冻结契约。
入口被包在一个最小 typer 命令里，只是为了借用 CliRunner 的 stdin/输出隔离。

**网络**：只使用 ``httpx.MockTransport``，不启动服务器、不访问网络。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping, Sequence

import httpx
import pytest
import typer
from typer.testing import CliRunner

DEFAULT_ENDPOINT = "http://127.0.0.1:8000"

# --------------------------------------------------------------------------- #
# 被测模块的惰性访问
# --------------------------------------------------------------------------- #
def _load_cli():
    """惰性导入被测模块。实现缺失时抛 ModuleNotFoundError（运行期，非收集期）。"""
    import importlib

    return importlib.import_module("notify_hub.cli")


@pytest.fixture
def cli():
    """被测的 ``notify_hub.cli`` 模块。"""
    return _load_cli()


@pytest.fixture(autouse=True)
def _isolate_endpoint_env(monkeypatch: pytest.MonkeyPatch):
    """默认不带 ``NOTIFY_HUB_ENDPOINT``，避免宿主机环境串进 endpoint 用例。"""
    monkeypatch.delenv("NOTIFY_HUB_ENDPOINT", raising=False)


# --------------------------------------------------------------------------- #
# 假服务端：挂 MockTransport 的 httpx.Client
# --------------------------------------------------------------------------- #
Responder = Callable[[httpx.Request], httpx.Response]


class _Stub:
    """记录 CLI 发出的请求，并按 ``responder`` 产出响应。"""

    def __init__(self, responder: Responder) -> None:
        self._responder = responder
        self.requests: list[httpx.Request] = []
        self.endpoints: list[Any] = []

    def build_client(self, *args: Any, **kwargs: Any) -> httpx.Client:
        """占据 ``notify_hub.cli.build_client`` 的位置（唯一的测试接缝）。"""
        endpoint = kwargs.get("endpoint", kwargs.get("base_url", args[0] if args else None))
        self.endpoints.append(endpoint)

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


def _raises(exc: BaseException) -> Responder:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


# --------------------------------------------------------------------------- #
# 运行器：CliRunner -> 冻结入口 main()
# --------------------------------------------------------------------------- #
def _entry_app() -> typer.Typer:
    """把 ``cli.main`` 包成一个最小 typer 命令，以便 CliRunner 驱动。"""
    entry = typer.Typer(add_completion=False, pretty_exceptions_enable=False)

    @entry.command(
        name="notify",
        add_help_option=False,
        no_args_is_help=False,
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    )
    def _run(ctx: typer.Context) -> None:
        import sys

        cli_module = _load_cli()
        argv = list(ctx.args)
        saved_argv = sys.argv
        sys.argv = ["notify", *argv]
        try:
            cli_module.main()
        finally:
            sys.argv = saved_argv

    return entry


def _run(
    args: Sequence[str],
    *,
    cli: Any,
    stub: _Stub,
    monkeypatch: pytest.MonkeyPatch,
    input: str | None = None,
    env: Mapping[str, str] | None = None,
):
    monkeypatch.setattr(cli, "build_client", stub.build_client)
    runner = CliRunner()
    return runner.invoke(
        _entry_app(), list(args), input=input, env=env, catch_exceptions=False
    )


def _assert_readable_error(result) -> None:
    """错误契约：所有错误写 stderr，形如 ``错误: <可读原因>``（第 2 段输出契约）。"""
    text = result.stderr.strip()
    assert text, "错误必须写到 stderr"
    assert "错误" in text, f"stderr 缺少冻结的错误前缀: {text!r}"
    assert text.rstrip(":：").strip() != "错误", f"stderr 只有前缀、没有可读原因: {text!r}"


def _assert_no_traceback(result) -> None:
    assert "Traceback" not in result.output, result.output
    assert "Traceback" not in result.stderr, result.stderr


def _todo(
    todo_id: int,
    overdue_seconds: float,
    *,
    source: str,
    title: str,
    status: str = "pending",
) -> dict[str, Any]:
    """一条形状与 M7 ``TodoOut`` 一致的待办。"""
    return {
        "id": todo_id,
        "source": source,
        "category": "uncategorized",
        "title": title,
        "status": status,
        "ack_reason": "caller_declared",
        "preferred_channel": None,
        "created_at": "2024-01-01T00:00:00+00:00",
        "first_notified_at": "2024-01-01T00:00:00+00:00",
        "last_notified_at": "2024-01-01T00:00:00+00:00",
        "reminder_count": 0,
        "completed_at": None,
        "overdue_seconds": overdue_seconds,
    }


# --------------------------------------------------------------------------- #
# 第 4 段第 1 条：成功投递
# --------------------------------------------------------------------------- #
def test_send_message_success_prints_message_id(cli, monkeypatch):
    stub = _Stub(lambda request: _json({"message_id": 42}, status=202))

    result = _run(
        ["--source", "backup", "--title", "备份完成", "--level", "info"],
        cli=cli,
        stub=stub,
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 0, result.stderr
    assert "message_id=42" in result.stdout

    request = stub.request
    assert request.method == "POST"
    assert request.url.path == "/api/v1/messages"
    # endpoint 缺省值（第 2 段解析顺序的最后一级）。
    assert (request.url.host, request.url.port) == ("127.0.0.1", 8000)

    body = stub.json_body
    assert body["source"] == "backup"
    assert body["title"] == "备份完成"
    assert body["level"] == "info"
    assert body["need_ack"] is False


def test_send_message_body_option(cli, monkeypatch):
    stub = _Stub(lambda request: _json({"message_id": 1}, status=202))

    result = _run(
        ["--source", "s", "--title", "t", "--body", "第一行"],
        cli=cli,
        stub=stub,
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 0, result.stderr
    assert stub.json_body["body"] == "第一行"


def test_send_message_need_ack_and_dedup_key(cli, monkeypatch):
    stub = _Stub(lambda request: _json({"message_id": 2, "todo_id": 5}, status=202))

    result = _run(
        [
            "--source", "s",
            "--title", "t",
            "--need-ack",
            "--dedup-key", "backup-2024-01-01",
        ],
        cli=cli,
        stub=stub,
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 0, result.stderr
    body = stub.json_body
    assert body["need_ack"] is True
    assert body["dedup_key"] == "backup-2024-01-01"


# --------------------------------------------------------------------------- #
# 第 4 段第 2 条：--body-stdin
# --------------------------------------------------------------------------- #
def test_send_message_body_from_stdin(cli, monkeypatch):
    stub = _Stub(lambda request: _json({"message_id": 3}, status=202))

    result = _run(
        ["--source", "s", "--title", "t", "--body-stdin"],
        cli=cli,
        stub=stub,
        monkeypatch=monkeypatch,
        input="多行\n正文",
    )

    assert result.exit_code == 0, result.stderr
    assert stub.json_body["body"] == "多行\n正文"


# --------------------------------------------------------------------------- #
# 第 4 段第 3 条：服务端拒绝（HTTP 4xx）-> 退出码 1
# --------------------------------------------------------------------------- #
def test_server_rejection_reports_field_reason(cli, monkeypatch):
    payload = {
        "detail": [
            {
                "loc": ["body", "source"],
                "msg": "String should have at least 1 character",
                "type": "string_too_short",
            }
        ]
    }
    stub = _Stub(lambda request: _json(payload, status=422))

    # --source 是必填选项（第 4 段第 5 条），因此要让*服务端*拒绝，只能给出服务端不接受的取值。
    result = _run(
        ["--source", "", "--title", "无来源"],
        cli=cli,
        stub=stub,
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 1, result.stderr
    assert "source" in result.stderr, result.stderr
    _assert_readable_error(result)


# --------------------------------------------------------------------------- #
# 第 4 段第 4 条：服务不可达 -> 退出码 3
# --------------------------------------------------------------------------- #
def test_service_unreachable_connect_error(cli, monkeypatch):
    stub = _Stub(_raises(httpx.ConnectError("connection refused")))

    result = _run(
        ["--source", "backup", "--title", "测试"],
        cli=cli,
        stub=stub,
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 3, result.stderr
    assert "无法连接" in result.stderr, result.stderr


def test_service_unreachable_timeout(cli, monkeypatch):
    stub = _Stub(_raises(httpx.TimeoutException("timed out")))

    result = _run(
        ["--source", "backup", "--title", "测试"],
        cli=cli,
        stub=stub,
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 3, result.stderr
    assert "无法连接" in result.stderr, result.stderr


# --------------------------------------------------------------------------- #
# 第 4 段第 5 条 + 第 2 段命令面：用法错误 -> 退出码 2
# --------------------------------------------------------------------------- #
def test_missing_source_is_usage_error(cli, monkeypatch):
    stub = _Stub(lambda request: _json({"message_id": 1}, status=202))

    result = _run(
        ["--title", "x"],
        cli=cli,
        stub=stub,
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 2, result.stderr
    assert stub.requests == [], "用法错误不应发出请求"


def test_body_and_body_stdin_conflict_is_usage_error(cli, monkeypatch):
    stub = _Stub(lambda request: _json({"message_id": 1}, status=202))

    result = _run(
        ["--source", "s", "--title", "t", "--body", "x", "--body-stdin"],
        cli=cli,
        stub=stub,
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 2, result.stderr
    assert stub.requests == [], "用法错误不应发出请求"


# --------------------------------------------------------------------------- #
# 第 4 段第 6 条：todo list
# --------------------------------------------------------------------------- #
def test_todo_list_uses_server_order_and_tab_format(cli, monkeypatch):
    # 服务端已按 overdue_seconds 降序排序，CLI 必须保持该顺序。
    todos = [
        _todo(2, 18000.0, source="backup", title="备份失败"),
        _todo(1, 3600.0, source="ci", title="构建失败"),
        _todo(3, 1200.0, source="cron", title="巡检"),
    ]
    stub = _Stub(lambda request: _json({"todos": todos, "total": 3}))

    result = _run(["todo", "list"], cli=cli, stub=stub, monkeypatch=monkeypatch)

    assert result.exit_code == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 3, result.stdout

    fields = [line.split("\t") for line in lines]
    # 输出契约：<todo_id>\t<超时时长>\t<来源>\t<标题>
    assert all(len(row) == 4 for row in fields), result.stdout
    assert [row[0] for row in fields] == ["2", "1", "3"], result.stdout
    assert all(row[1].strip() for row in fields), result.stdout
    assert [row[2] for row in fields] == ["backup", "ci", "cron"]
    assert [row[3] for row in fields] == ["备份失败", "构建失败", "巡检"]

    assert stub.request.method == "GET"
    assert stub.request.url.path == "/api/v1/todos"


def test_todo_list_all_requests_all_status(cli, monkeypatch):
    todos = [
        _todo(2, 18000.0, source="backup", title="备份失败"),
        _todo(1, 3600.0, source="ci", title="构建失败", status="done"),
    ]
    stub = _Stub(lambda request: _json({"todos": todos, "total": 2}))

    result = _run(["todo", "list", "--all"], cli=cli, stub=stub, monkeypatch=monkeypatch)

    assert result.exit_code == 0, result.stderr
    request = stub.request
    assert request.method == "GET"
    assert request.url.path == "/api/v1/todos"
    # --all 必须能拿到已完成待办：服务端默认只返回 pending（M7 端点表 ?status=pending|done|all）。
    assert request.url.params.get("status") == "all", str(request.url)


def test_todo_list_unparsable_response_is_exit_1(cli, monkeypatch):
    stub = _Stub(lambda request: _json({"total": 0}))  # 缺 todos 字段

    result = _run(["todo", "list"], cli=cli, stub=stub, monkeypatch=monkeypatch)

    assert result.exit_code == 1, result.stderr
    _assert_readable_error(result)
    _assert_no_traceback(result)


# --------------------------------------------------------------------------- #
# 第 4 段第 7/8 条：todo done
# --------------------------------------------------------------------------- #
def test_todo_done_success(cli, monkeypatch):
    stub = _Stub(
        lambda request: _json(
            {
                "todo_id": 7,
                "status": "completed",
                "completed_at": "2024-01-01T00:10:00+00:00",
            }
        )
    )

    result = _run(["todo", "done", "7"], cli=cli, stub=stub, monkeypatch=monkeypatch)

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "done 7", result.stdout

    request = stub.request
    assert request.method == "POST"
    assert request.url.path == "/api/v1/todos/7/done"


def test_todo_done_not_found_is_exit_1(cli, monkeypatch):
    stub = _Stub(lambda request: _json({"detail": "待办不存在"}, status=404))

    result = _run(["todo", "done", "999"], cli=cli, stub=stub, monkeypatch=monkeypatch)

    assert result.exit_code == 1, result.stderr
    _assert_readable_error(result)
    _assert_no_traceback(result)


# --------------------------------------------------------------------------- #
# 第 4 段第 9 条：endpoint 优先级
# --------------------------------------------------------------------------- #
def test_endpoint_from_environment(cli, monkeypatch):
    monkeypatch.setenv("NOTIFY_HUB_ENDPOINT", "http://x:9")
    stub = _Stub(lambda request: _json({"message_id": 1}, status=202))

    result = _run(
        ["--source", "s", "--title", "t"],
        cli=cli,
        stub=stub,
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 0, result.stderr
    url = stub.request.url
    assert (url.host, url.port) == ("x", 9), str(url)
    assert url.path == "/api/v1/messages"


def test_endpoint_flag_overrides_environment(cli, monkeypatch):
    monkeypatch.setenv("NOTIFY_HUB_ENDPOINT", "http://x:9")
    stub = _Stub(lambda request: _json({"message_id": 1}, status=202))

    result = _run(
        ["--source", "s", "--title", "t", "--endpoint", "http://y:8"],
        cli=cli,
        stub=stub,
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 0, result.stderr
    url = stub.request.url
    assert (url.host, url.port) == ("y", 8), str(url)
    assert url.path == "/api/v1/messages"


# --------------------------------------------------------------------------- #
# 第 4 段第 10 条：响应格式异常不崩溃
# --------------------------------------------------------------------------- #
def test_message_response_without_message_id_is_exit_1(cli, monkeypatch):
    stub = _Stub(lambda request: _json({}, status=202))

    result = _run(
        ["--source", "s", "--title", "t"],
        cli=cli,
        stub=stub,
        monkeypatch=monkeypatch,
    )

    assert result.exit_code == 1, result.stderr
    _assert_readable_error(result)
    _assert_no_traceback(result)


# --------------------------------------------------------------------------- #
# 第 2 段接口：build_client 接缝与根命令对象
# --------------------------------------------------------------------------- #
def test_build_client_seam_returns_httpx_client(cli):
    client = cli.build_client("http://127.0.0.1:9")
    try:
        assert isinstance(client, httpx.Client)
    finally:
        client.close()


def test_module_exposes_frozen_root_app_and_main(cli):
    assert isinstance(cli.app, typer.Typer)
    assert callable(cli.main)
