"""M7 模块测试 —— 待办查询与完成接口（`tests/test_api_todos.py`）。

覆盖 ``openspec/changes/add-notify-hub/architecture.md`` 第 6 节「模块 M7」第 4 段
验证方法的第 11–12 条。第 1–10、13、14 条在 ``tests/test_ingest.py``。

阶段 A 下本文件必然因实现缺失而为红（fixture 装配失败），收集阶段必须成功。
待办一律通过真实的 ``POST /api/v1/messages`` + ``need_ack=true`` 生成，
并用共享的 ``manual_clock`` 制造不同的 ``first_notified_at``——不直接写库、
不重写 conftest 的装配逻辑。
"""

from __future__ import annotations

from notify_hub.domain import TodoStatus

MSG_URL = "/api/v1/messages"
TODOS_URL = "/api/v1/todos"


def _accept_todo(client, title: str) -> int:
    """投递一条 need_ack 消息，返回其 todo_id。"""
    resp = client.post(MSG_URL, json={"source": "s", "title": title, "need_ack": True})
    assert resp.status_code == 202
    todo_id = resp.json()["todo_id"]
    assert todo_id is not None
    return todo_id


# --------------------------------------------------------------------------- #
# 第 11 条：待办列表的排序与状态过滤
# --------------------------------------------------------------------------- #
def test_todos_list_orders_oldest_first_and_filters_by_status(api_client, manual_clock):
    oldest = _accept_todo(api_client, "t-oldest")
    manual_clock.advance(1200)
    middle = _accept_todo(api_client, "t-middle")
    manual_clock.advance(1200)
    newest = _accept_todo(api_client, "t-newest")
    manual_clock.advance(1200)

    resp = api_client.get(TODOS_URL)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 3
    assert [todo["id"] for todo in payload["todos"]] == [oldest, middle, newest]

    # 排序键是 overdue_seconds 降序；三次受理各间隔 1200 秒。
    overdue = [todo["overdue_seconds"] for todo in payload["todos"]]
    for actual, expected in zip(overdue, (3600.0, 2400.0, 1200.0)):
        assert abs(actual - expected) < 1.0
    assert overdue == sorted(overdue, reverse=True)
    assert all(todo["status"] == "pending" for todo in payload["todos"])

    done_resp = api_client.post(f"{TODOS_URL}/{middle}/done")
    assert done_resp.status_code == 200

    pending = api_client.get(TODOS_URL, params={"status": "pending"}).json()
    assert [todo["id"] for todo in pending["todos"]] == [oldest, newest]

    done = api_client.get(TODOS_URL, params={"status": "done"}).json()
    assert [todo["id"] for todo in done["todos"]] == [middle]
    assert done["todos"][0]["status"] == "done"

    every = api_client.get(TODOS_URL, params={"status": "all"}).json()
    assert sorted(todo["id"] for todo in every["todos"]) == sorted([oldest, middle, newest])


# --------------------------------------------------------------------------- #
# 第 12 条：待办完成（幂等、404、完成后不再出现在默认列表）
# --------------------------------------------------------------------------- #
def test_todo_complete_is_idempotent(api_client, ctx, manual_clock):
    todo_id = _accept_todo(api_client, "t-done")

    first = api_client.post(f"{TODOS_URL}/{todo_id}/done")
    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["todo_id"] == todo_id
    assert first_payload["status"] == "completed"
    assert first_payload["completed_at"] is not None

    manual_clock.advance(600)
    second = api_client.post(f"{TODOS_URL}/{todo_id}/done")
    assert second.status_code == 200
    assert second.json()["status"] == "already_completed"
    assert second.json()["completed_at"] == first_payload["completed_at"]

    missing = api_client.post(f"{TODOS_URL}/999999/done")
    assert missing.status_code == 404

    stored = ctx.todos.get(todo_id)
    assert stored is not None
    assert stored.status == TodoStatus.DONE

    pending = api_client.get(TODOS_URL).json()
    assert todo_id not in [todo["id"] for todo in pending["todos"]]
