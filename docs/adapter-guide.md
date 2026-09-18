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
