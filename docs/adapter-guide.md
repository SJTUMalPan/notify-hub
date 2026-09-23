# 新增通知渠道：适配器开发指南

新增一个通知渠道 = 实现一个满足 `Notifier` 协议的适配器类，并在配置里用
`channels[].type` 引用它。**不需要改动接入层（API）与分类器**：投递层只通过协议调用
`send()`，分类器只负责给出 `channel` 偏好。

## 契约

适配器必须提供三个成员，它们共同构成 `notify_hub.notifiers.base.Notifier`
（`runtime_checkable` 协议）：

| 成员 | 类型 | 作用 |
|---|---|---|
| `channel_id` | `str` | 渠道实例 id；注册表按它索引，配置里的 `channel`/`default_channel` 引用它 |
| `capabilities()` | `ChannelCapabilities` | 声明渠道能力，供上层在构建消息前查询 |
| `send(msg)` | `DeliveryResult` | 执行一次投递；**MUST NOT 抛异常** |

### ChannelCapabilities

`ChannelCapabilities` 是不可变值对象，三个字段都有缺省值：

- `supports_rich_text: bool = False`：是否支持富文本。
- `max_body_length: int | None = None`：正文长度上限；`None` 表示不限制。
- `supports_headers: bool = False`：是否支持附加请求头。

### DeliveryResult 与失败语义

`send()` 必须把**所有**渠道侧与网络侧错误转成失败结果返回，而不是抛出：

- 成功：`DeliveryResult.success(receipt=...)`，`ok is True`；`receipt` 是渠道回执标识。
- 失败：`DeliveryResult.failure(reason)`，`ok is False`；`reason` 必须可读且**已脱敏**，
  不得包含凭据值。

上层据此写入投递记录并决定是否回退到其它渠道。`send()` 抛异常会破坏「渠道故障不影响
服务」这一不变量，属于契约违规。

## 注册方式

1. 实现适配器类；建议同时提供 `build_<type>_notifier(spec, secrets)` 工厂。
2. 在 `NOTIFIER_FACTORIES`（`notify_hub.notifiers.registry`）中注册工厂，key 即配置里的
   `channels[].type`。
3. 也可以直接把已构造的实例注册进 `NotifierRegistry`：`registry.register(instance)`，
   按 `instance.channel_id` 索引；重复 id 覆盖并记录 warning。
4. 配置示例：

```yaml
channels:
  - id: dummy
    type: dummy
    enabled: true
    params: {}
    credentials: {}
```

渠道不可用（未启用、未知类型、凭据缺失、工厂抛异常）只会被记入
`NotifierRegistry.unavailable_reasons()`，**不会**让服务启动失败。

## 平台专属的嵌套载荷

通用 `webhook` 适配器只能发**平铺 JSON**：`field_map` 仅仅把消息字段名映射成载荷里的
**顶层**键名，既不能嵌套，也不能改变结构。但不少平台要求**嵌套**的请求体——飞书、
钉钉、企业微信的机器人协议都是如此（例如飞书要
`{"msg_type": "text", "content": {"text": "..."}}`，加签字段还要放在 body 顶层）。
平铺载荷表达不了这种结构，硬塞只会得到一个验签或参数错误。

**正确做法是按上面的契约新写一个专属适配器，而不是继续给 `webhook` 加开关。**
本仓库内的现成范例是 `notify_hub.notifiers.feishu`（注册名 `feishu`）：

- `FeishuNotifier` 组装嵌套请求体，`content.text` 是一段已渲染好的多行文本；
- 凭据 `secret` **可选**：提供即启用加签，`timestamp` 与 `sign` 放在 JSON body 顶层
  （`timestamp` 单位是**秒**），不是 URL query；
- `send()` **绝不抛异常**：HTTP 非 2xx、响应体 `code != 0`、网络异常一律转成
  `DeliveryResult.failure(reason)`，且 `reason` 已脱敏，不含 webhook URL 或密钥；
- `capabilities()` 如实声明：首版只支持纯文本、`max_body_length=20000`（飞书请求体上限）、
  不支持附加请求头。

```python
from notify_hub.notifiers.registry import NOTIFIER_FACTORIES
from notify_hub.notifiers.feishu import build_feishu_notifier


NOTIFIER_FACTORIES["feishu"] = build_feishu_notifier
```

配置侧对应的写法见 [configuration.md](configuration.md) 的 `feishu` 一节：不用加签时
**只声明 `url`**，不要写 `secret: null`。

## 完整的 dummy 适配器示例

下面的代码块可以直接 `exec` 运行：它定义一个最小适配器、满足 `Notifier` 协议、
`send()` 返回 `DeliveryResult`，并且能注册进 `NotifierRegistry`。

```python dummy-adapter
from notify_hub.notifiers.base import (
    ChannelCapabilities,
    DeliveryResult,
    NotificationMessage,
)


class DummyNotifier:
    """最小可用适配器：把消息记录在内存里，永不抛异常。"""

    def __init__(self, channel_id: str = "dummy") -> None:
        self.channel_id = channel_id
        self.sent = []

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            supports_rich_text=False,
            max_body_length=None,
            supports_headers=False,
        )

    def send(self, msg: NotificationMessage) -> DeliveryResult:
        try:
            self.sent.append(msg)
        except Exception as exc:  # noqa: BLE001 —— 失败必须转成结果，不得上抛
            return DeliveryResult.failure(
                f"dummy 适配器记录消息失败: {type(exc).__name__}"
            )
        return DeliveryResult.success(receipt=f"dummy:{msg.source}:{len(self.sent)}")
```

接入时把上面的类包进一个工厂，并注册到 `NOTIFIER_FACTORIES["dummy"]`：

```python
from notify_hub.notifiers.registry import NOTIFIER_FACTORIES


def build_dummy_notifier(spec, secrets):
    return DummyNotifier(channel_id=spec.id)


NOTIFIER_FACTORIES["dummy"] = build_dummy_notifier
```

之后在配置里声明 `type: dummy` 即可被路由到；分类规则里写 `channel: dummy` 可以让命中
该规则的消息优先走这个渠道。
