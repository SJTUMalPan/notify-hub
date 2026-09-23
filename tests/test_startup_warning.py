"""P2 补救（§7.1 末尾第一组）：未配置令牌时的启动告警。

依据（**唯一来源**）：``openspec/changes/add-public-access/architecture.md``
§7 表格第 2 行与 §7.1 末尾——``create_app`` 在 ``auth_token is None`` 时打出一条含
「未配置」字样的 **WARNING**；配置了令牌时**不得**出现该告警。对应 ``design.md`` D4
承诺的「启动时输出显著告警」。

**实现已存在**（``src/notify_hub/app.py``），本文件应当**全绿**。
若失败，是架构师实现的问题——**报告，不要改期望值去迁就代码**。

**装配方式**：用真实 ``create_app``（告警就在那里发），``Settings`` 由 conftest 的
``tmp_settings`` 经 ``dataclasses.replace`` 派生（§2 明文允许的造法），并只把
``db_path`` 指到 ``tmp_path``，避免污染仓库目录。告警发生在 ``create_app`` 调用期，
因此本文件**不**启动服务（不需要 TestClient，也不起后台线程）。
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

import pytest

#: 判定「该告警」的冻结字样（§7.1 末尾：「含『未配置』字样的 WARNING」）。
MISSING_TOKEN_MARKER = "未配置"

#: 负对照用的哨兵：证明 caplog 在这个用例里真的能收到 ``notify_hub`` logger 的 WARNING。
_CAPTURE_SENTINEL = "CAPTURE-SENTINEL-2f6c"


def _create_app(settings, tmp_path: Path, auth_token):
    """按给定令牌构造真实应用；返回 ``(app, ctx)``。"""
    from notify_hub.app import create_app  # 惰性（与仓库既有约定一致）

    derived = dataclasses.replace(
        settings,
        db_path=tmp_path / f"startup-warning-{auth_token or 'none'}.db",
        auth_token=auth_token,
    )
    app = create_app(derived)
    return app, app.state.ctx


def _missing_token_warnings(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [
        record
        for record in records
        if MISSING_TOKEN_MARKER in record.getMessage()
    ]


@pytest.fixture
def warn_capture(caplog: pytest.LogCaptureFixture):
    """在 ``notify_hub`` logger 上捕获 WARNING（不依赖宿主的根 logger 配置）。"""
    caplog.set_level(logging.WARNING, logger="notify_hub")
    return caplog


def test_create_app_warns_when_auth_token_is_missing(tmp_path, tmp_settings, warn_capture) -> None:
    assert tmp_settings.auth_token is None, "前置条件：tmp_settings 本身不含令牌"

    app, ctx = _create_app(tmp_settings, tmp_path, auth_token=None)
    try:
        warnings = _missing_token_warnings(warn_capture.records)
        assert warnings, (
            "auth_token 为 None 时 create_app 必须打出一条含 "
            f"{MISSING_TOKEN_MARKER!r} 的 WARNING；实际记录："
            f"{[(r.levelname, r.getMessage()) for r in warn_capture.records]!r}"
        )
        assert any(record.levelno == logging.WARNING for record in warnings), (
            f"该告警必须是 WARNING 级别："
            f"{[(r.levelname, r.getMessage()) for r in warnings]!r}"
        )
    finally:
        ctx.db.dispose()


def test_create_app_does_not_warn_when_auth_token_is_configured(
    tmp_path, tmp_settings, warn_capture
) -> None:
    token = "STARTUP-WARNING-TOKEN-7b1e"
    app, ctx = _create_app(tmp_settings, tmp_path, auth_token=token)
    try:
        assert ctx.settings.auth_token == token, "前置条件：令牌已配置到上下文"

        # 负对照：同一个 capture 必须能收到 notify_hub 的 WARNING，否则下面的
        # 「没有该告警」是空断言（capture 坏了也会通过）。
        logging.getLogger("notify_hub").warning(_CAPTURE_SENTINEL)
        assert any(
            _CAPTURE_SENTINEL in record.getMessage() for record in warn_capture.records
        ), "caplog 未捕获到哨兵 WARNING，本用例的负断言不成立"

        found = _missing_token_warnings(warn_capture.records)
        assert found == [], (
            "配置了令牌时不得出现「未配置」告警；"
            f"实际：{[(r.levelname, r.getMessage()) for r in found]!r}"
        )
    finally:
        ctx.db.dispose()
