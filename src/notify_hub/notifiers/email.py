"""内置邮件适配器（architecture.md 6-M4 第 2/4 段）。

通过 SMTP 发送纯文本通知邮件。``smtp_factory`` 是唯一的测试接缝（错误注入用）；
正常路径由测试启动本机 ``aiosmtpd`` 验证。

主题：``[<LEVEL>] <title>``；正文**就是 ``msg.body`` 原样**（4.6 节「正文渲染归属」：
``msg.body`` 已含 ``来源``/``时间``/可选 ``已超时``/空行/原始正文，适配器不得再拼表头）。

本文件由 M4 模块负责。
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage as _EmailMessage
from typing import Callable, Sequence

from notify_hub.config import ChannelSpec
from notify_hub.errors import ConfigurationError
from notify_hub.redact import redact_exception

from .base import ChannelCapabilities, DeliveryResult, NotificationMessage

__all__ = ["EmailNotifier", "build_email_notifier"]

_LOGGER = logging.getLogger("notify_hub.notifiers.email")


class EmailNotifier:
    """通过 SMTP 发送通知邮件。"""

    def __init__(
        self,
        channel_id: str,
        *,
        host: str,
        port: int = 25,
        use_tls: bool = False,
        username: str | None = None,
        password: str | None = None,
        sender: str,
        recipients: Sequence[str],
        timeout: float = 10.0,
        smtp_factory: Callable[[], smtplib.SMTP] | None = None,
        tls_context: ssl.SSLContext | None = None,
        secrets: Sequence[str] = (),
    ) -> None:
        self.channel_id = channel_id
        self._host = host
        self._port = port
        self._use_tls = use_tls
        self._username = username
        self._password = password
        self._sender = sender
        self._recipients = list(recipients)
        self._timeout = timeout
        self._smtp_factory = smtp_factory
        # 审计修复：``smtplib.SMTP.starttls()`` 不传 context 时**不校验证书**（接受任意
        # 中间人证书）。默认用 ``create_default_context()``：CERT_REQUIRED + 校验主机名。
        # 构造期就建好，配置/系统 CA 有问题时由注册表计入 unavailable_reasons（fail-closed）。
        self._tls_context = tls_context if tls_context is not None else (
            ssl.create_default_context() if use_tls else None
        )
        self._secrets = tuple(secrets)

    # ------------------------------------------------------------------ #
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(supports_rich_text=False, supports_headers=False)

    def _new_client(self) -> smtplib.SMTP:
        if self._smtp_factory is not None:
            return self._smtp_factory()
        return smtplib.SMTP(self._host, self._port, timeout=self._timeout)

    def _build_message(self, msg: NotificationMessage) -> _EmailMessage:
        message = _EmailMessage()
        message["Subject"] = f"[{msg.level.value.upper()}] {msg.title}"
        message["From"] = self._sender
        message["To"] = ", ".join(self._recipients)
        message.set_content(msg.body, charset="utf-8")
        return message

    # ------------------------------------------------------------------ #
    def send(self, msg: NotificationMessage) -> DeliveryResult:
        client: smtplib.SMTP | None = None
        try:
            message = self._build_message(msg)
            client = self._new_client()
            if self._use_tls:
                client.starttls(context=self._tls_context)
            if self._username is not None:
                client.login(self._username, self._password or "")
            client.sendmail(self._sender, list(self._recipients), message.as_bytes())
            _LOGGER.info("email 渠道 %s 投递成功", self.channel_id)
            return DeliveryResult.success()
        except Exception as exc:  # noqa: BLE001 - send() MUST NOT 抛异常
            reason = redact_exception(exc, self._secrets)
            _LOGGER.warning("email 渠道 %s 投递失败: %s", self.channel_id, reason)
            return DeliveryResult.failure(reason)
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception:  # noqa: BLE001 - 关闭失败不得影响投递结果
                    try:
                        client.close()
                    except Exception:  # noqa: BLE001
                        pass


def build_email_notifier(
    spec: ChannelSpec, secrets: Sequence[str] = ()
) -> EmailNotifier:
    """按冻结的参数映射表从 :class:`ChannelSpec` 构造 email 适配器。

    必填项缺失时抛异常，由注册表按「适配器构造失败」记入 ``unavailable_reasons``。
    """
    params = spec.params or {}

    host = params.get("host")
    if not host:
        raise ConfigurationError(f"email 渠道 {spec.id} 缺少必填参数: host")

    sender = params.get("sender")
    if not sender:
        raise ConfigurationError(f"email 渠道 {spec.id} 缺少必填参数: sender")

    recipients = params.get("recipients")
    if not recipients or not isinstance(recipients, (list, tuple)):
        raise ConfigurationError(f"email 渠道 {spec.id} 缺少非空参数: recipients")

    return EmailNotifier(
        spec.id,
        host=str(host),
        port=int(params.get("port", 25)),
        use_tls=bool(params.get("use_tls", False)),
        username=spec.credentials.get("username"),
        password=spec.credentials.get("password"),
        sender=str(sender),
        recipients=list(recipients),
        timeout=float(params.get("timeout", 10.0)),
        secrets=tuple(secrets),
    )
