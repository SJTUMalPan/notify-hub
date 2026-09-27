"""M-L 接入大小上限：请求体字节数的 fail-closed 闸门。

见 ``openspec/changes/add-ingest-limits/``（``design.md`` D3、D5）。

契约要点：

- **纯 ASGI 中间件**（不继承 ``BaseHTTPMiddleware``），与 :class:`notify_hub.auth.AuthGuard`
  同一形态：透传分支只做 ``await self.app(scope, receive, send)``。
- 计字节以**实际从 ``receive`` 读到的请求体字节**为准。只看 ``Content-Length`` 会漏掉
  分块传输（无该头）与伪造该头的请求；``Content-Length`` 仅作为**快速路径**先拦一次，
  省掉无谓的读取。
- 做法是「先读完（带上限）再放行」：整份请求体在中间件里读进来（收到即计数，
  一越界立刻回 413 并**根本不调用内层应用**），然后以重放的方式交给内层。
  这样 413 一定发生在解析与写库**之前**，不存在「已写一半」的状态。
- 上限是 1 MiB 量级、端点收的都是小 JSON，因此不做流式转发；
  全量缓冲的代价是「单请求最多多占 max_bytes 内存」，换来确定性。
- 超限回 ``413``（JSON），响应体不回显请求内容，也不含任何凭据。
- 非 HTTP（websocket / lifespan）零开销透传。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

__all__ = ["BodyLimit", "PAYLOAD_TOO_LARGE_MESSAGE"]

#: 413 的 JSON 响应体（``{"detail": ...}``，与 FastAPI 的错误形状一致）。
PAYLOAD_TOO_LARGE_MESSAGE = "请求体超过上限，请减少单次提交的数据量后重试"


@dataclass(frozen=True)
class BodyLimit:
    """请求体上限中间件（纯 ASGI）。

    ``app`` 字段**必须排在第一**：Starlette 的 ``add_middleware`` 以
    ``cls(app, *args, **kwargs)`` 位置传参构造中间件。一切构造一律用关键字
    （``BodyLimit(max_bytes=N)``），禁止位置传参。
    """

    app: Any = None
    max_bytes: int = 1024 * 1024

    async def __call__(
        self, scope: dict, receive: Callable[..., Any], send: Callable[..., Any]
    ) -> None:
        if scope.get("type") != "http":
            await self._forward(scope, receive, send)
            return

        # 快速路径：声明值就已经越界，连读都不用读。
        declared = _content_length(scope.get("headers") or [])
        if declared is not None and declared > self.max_bytes:
            await self._too_large(send)
            return

        body = await self._read_body(receive)
        if body is None:
            await self._too_large(send)
            return

        await self._forward(scope, _replay(body), send)

    async def _read_body(self, receive: Callable[..., Any]) -> bytes | None:
        """读到请求体结束；越界返回 ``None``（调用方回 413）。

        以**实际字节数**判定，因此 ``Content-Length`` 缺失或撒谎都不影响结论。
        """
        body = bytearray()
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                # 客户端提前断开：按已读到的内容放行，让内层按截断的请求体自行报错。
                break
            body.extend(message.get("body") or b"")
            if len(body) > self.max_bytes:
                return None
            if not message.get("more_body"):
                break
        return bytes(body)

    async def _forward(self, scope: dict, receive: Any, send: Any) -> None:
        if self.app is None:
            raise RuntimeError("BodyLimit 未绑定内层应用（app is None），无法透传")
        await self.app(scope, receive, send)

    async def _too_large(self, send: Any) -> None:
        body = json.dumps({"detail": PAYLOAD_TOO_LARGE_MESSAGE}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _replay(body: bytes) -> Callable[[], Any]:
    """把已读完的请求体包装成 ``receive``：第一次给整份 body，之后给 disconnect。"""
    state = {"sent": False}

    async def receive() -> dict:
        if state["sent"]:
            return {"type": "http.disconnect"}
        state["sent"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _content_length(headers: list[tuple[bytes, bytes]]) -> int | None:
    """取 ``Content-Length``（首个合法值）；缺失或非法返回 ``None``。"""
    for name, value in headers:
        if name.lower() != b"content-length":
            continue
        try:
            parsed = int(value.decode("latin-1").strip())
        except (UnicodeDecodeError, ValueError):
            return None
        return parsed if parsed >= 0 else None
    return None
