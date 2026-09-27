"""安全审计 P1：接入端点的请求体与批量条数上限。

规格：``openspec/changes/add-ingest-limits/specs/message-ingest/spec.md``
（「接入请求体上限」需求）。

审计原话是「批量/请求体没有上限」。真实后果不是「被攻破」，而是**低成本的自我 DoS**：
批量端点的请求体由 FastAPI 先整份解析成 Python 对象，一个几 GB 的数组就能把进程打满；
条数没有上限则会把唯一的投递线程灌爆。因此这里按 fail-closed 立闸门：

* 超限一律 ``413``（不是 ``422``——请求语法没错，是太大）；
* 超限时**一条都不许写库、一条都不许投递**（不做部分受理）；
* 计字节以**实际读到的请求体**为准，分块传输（无 ``Content-Length``）同样受限；
* 两个上限都必须是**正整数**，``0`` 不表示「关闭」（见 design.md D2）。

导入都写在测试函数体内，与 ``tests/`` 其它模块保持一致。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

#: 缺省上限（与 Settings 的缺省值同源，改缺省值时这里会一起被断言逼着改）。
DEFAULT_MAX_BODY_BYTES = 1024 * 1024
DEFAULT_MAX_BATCH_ITEMS = 500


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _settings_with(tmp_settings: Any, **overrides: Any) -> Any:
    """在 conftest 的最小配置上覆写上限。"""
    return replace(tmp_settings, **overrides)


def _app_client(ctx: Any) -> Any:
    """挂在 ``create_app`` 上的客户端：**必须**走生产装配，中间件才在链上。"""
    from fastapi.testclient import TestClient

    from notify_hub.app import create_app

    return TestClient(create_app(ctx.settings, ctx=ctx))


def _message_payload(source: str = "probe", title: str = "上限探针") -> dict[str, Any]:
    return {"source": source, "title": title, "level": "info"}


def _padded_payload(target_bytes: int) -> bytes:
    """构造一个**序列化后恰好** ``target_bytes`` 字节的合法消息体（原始 JSON 字节）。

    刻意返回字节而不是对象：``client.post(json=...)`` 会由 httpx 重新序列化
    （紧凑分隔符），字节数与测试里算出来的不一致，「恰好等于上限」这种边界断言就失去意义。
    直接发字节，测的就真的是中间件看到的那些字节。
    """
    import json

    def encode(body_padding: int) -> bytes:
        payload = {
            "source": "probe",
            "title": "上限探针",
            "level": "info",
            "body": "x" * body_padding,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    skeleton = encode(0)
    padding = target_bytes - len(skeleton)
    assert padding >= 0, f"目标字节数 {target_bytes} 太小，装不下消息骨架（{len(skeleton)}）"
    encoded = encode(padding)
    assert len(encoded) == target_bytes, (len(encoded), target_bytes)
    return encoded


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def test_default_limits_are_present_and_positive(tmp_settings: Any) -> None:
    assert tmp_settings.max_body_bytes == DEFAULT_MAX_BODY_BYTES
    assert tmp_settings.max_batch_items == DEFAULT_MAX_BATCH_ITEMS


def test_limits_are_read_from_the_server_section(tmp_path: Any) -> None:
    import yaml

    from notify_hub.config import load_settings

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "server": {
                    "host": "127.0.0.1",
                    "port": 8000,
                    "log_level": "INFO",
                    "max_body_bytes": 4096,
                    "max_batch_items": 7,
                },
                "storage": {"db_path": "./data/notify.db"},
                "rules": {"path": "./rules.yaml", "poll_interval_seconds": 5},
                "reminders": {"at": "21:00", "timezone": "Asia/Shanghai"},
                "channels": [],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    settings = load_settings(config_path)

    assert settings.max_body_bytes == 4096
    assert settings.max_batch_items == 7


@pytest.mark.parametrize("value", [0, -1, 1.5, "1024", True, None])
def test_non_positive_or_non_integer_limits_are_rejected(
    tmp_path: Any, value: Any
) -> None:
    """``0`` / 负数 / 非整数一律报错——**没有**「0 = 关闭上限」这种语义。"""
    import yaml

    from notify_hub.config import load_settings
    from notify_hub.errors import ConfigurationError

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "server": {
                    "host": "127.0.0.1",
                    "port": 8000,
                    "log_level": "INFO",
                    "max_body_bytes": value,
                },
                "storage": {"db_path": "./data/notify.db"},
                "rules": {"path": "./rules.yaml", "poll_interval_seconds": 5},
                "reminders": {"at": "21:00", "timezone": "Asia/Shanghai"},
                "channels": [],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(config_path)

    assert "max_body_bytes" in str(excinfo.value)


@pytest.mark.parametrize("value", [0, -5, 2.0, "500", False])
def test_batch_limit_must_be_a_positive_integer(tmp_path: Any, value: Any) -> None:
    import yaml

    from notify_hub.config import load_settings
    from notify_hub.errors import ConfigurationError

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "server": {
                    "host": "127.0.0.1",
                    "port": 8000,
                    "log_level": "INFO",
                    "max_batch_items": value,
                },
                "storage": {"db_path": "./data/notify.db"},
                "rules": {"path": "./rules.yaml", "poll_interval_seconds": 5},
                "reminders": {"at": "21:00", "timezone": "Asia/Shanghai"},
                "channels": [],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(config_path)

    assert "max_batch_items" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 请求体上限
# --------------------------------------------------------------------------- #
def test_body_over_the_limit_is_rejected_with_413_and_writes_nothing(
    make_context: Any, tmp_settings: Any
) -> None:
    ctx = make_context(_settings_with(tmp_settings, max_body_bytes=2048))

    with _app_client(ctx) as client:
        response = client.post(
            "/api/v1/messages",
            content=_padded_payload(2049),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413, response.text
    assert ctx.messages.count() == 0, "超限请求不得写入任何消息"


def test_body_exactly_at_the_limit_is_accepted(make_context: Any, tmp_settings: Any) -> None:
    """边界取**包含**：恰好等于上限是合法的（否则「上限」这个词就名不副实）。"""
    ctx = make_context(_settings_with(tmp_settings, max_body_bytes=2048))

    with _app_client(ctx) as client:
        response = client.post(
            "/api/v1/messages",
            content=_padded_payload(2048),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 202, response.text
    assert ctx.messages.count() == 1


def test_body_limit_also_covers_chunked_requests_without_content_length(
    make_context: Any, tmp_settings: Any
) -> None:
    """分块传输没有 ``Content-Length``，只看请求头的实现会在这里漏过去。"""
    ctx = make_context(_settings_with(tmp_settings, max_body_bytes=512))
    payload = _padded_payload(4096)

    def chunks() -> Any:
        for start in range(0, len(payload), 256):
            yield payload[start : start + 256]

    with _app_client(ctx) as client:
        # 正控：先证明这个请求真的没有 Content-Length（确实是分块传输）。
        # 否则 413 可能只是快速路径拦下来的，「读取层计数」这条路径就没被验证到。
        probe = client.build_request(
            "POST",
            "/api/v1/messages",
            content=chunks(),
            headers={"content-type": "application/json"},
        )
        assert "content-length" not in probe.headers, dict(probe.headers)
        assert probe.headers.get("transfer-encoding") == "chunked", dict(probe.headers)

        response = client.post(
            "/api/v1/messages",
            content=chunks(),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413, response.text
    assert ctx.messages.count() == 0


def test_health_endpoint_is_not_affected_by_the_body_limit(
    make_context: Any, tmp_settings: Any
) -> None:
    ctx = make_context(_settings_with(tmp_settings, max_body_bytes=64))

    with _app_client(ctx) as client:
        response = client.get("/healthz")

    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# 批量条数上限
# --------------------------------------------------------------------------- #
def test_batch_over_the_item_limit_is_rejected_as_a_whole(
    make_context: Any, tmp_settings: Any
) -> None:
    ctx = make_context(_settings_with(tmp_settings, max_batch_items=3))
    items = [_message_payload(title=f"批量探针 {index}") for index in range(4)]

    with _app_client(ctx) as client:
        response = client.post("/api/v1/messages/batch", json=items)

    assert response.status_code == 413, response.text
    assert "3" in response.text and "4" in response.text, (
        f"413 说明里应点出上限与本次条数，实际：{response.text}"
    )
    assert ctx.messages.count() == 0, "整批拒绝时一条都不许写库"


def test_batch_exactly_at_the_item_limit_is_accepted(
    make_context: Any, tmp_settings: Any
) -> None:
    ctx = make_context(_settings_with(tmp_settings, max_batch_items=3))
    items = [_message_payload(title=f"批量探针 {index}") for index in range(3)]

    with _app_client(ctx) as client:
        response = client.post("/api/v1/messages/batch", json=items)

    assert response.status_code == 207, response.text
    assert response.json()["accepted_count"] == 3
    assert ctx.messages.count() == 3


def test_batch_item_limit_still_reports_per_item_errors_below_the_limit(
    make_context: Any, tmp_settings: Any
) -> None:
    """守住既有契约：上限之内，「单条非法不影响其它条」的行为不能被我改坏。"""
    ctx = make_context(_settings_with(tmp_settings, max_batch_items=3))
    items = [_message_payload(), {"title": "缺少 source"}, _message_payload(title="第三条")]

    with _app_client(ctx) as client:
        response = client.post("/api/v1/messages/batch", json=items)

    assert response.status_code == 207, response.text
    body = response.json()
    assert body["accepted_count"] == 2
    assert body["rejected_count"] == 1
    assert body["results"][1]["index"] == 1
