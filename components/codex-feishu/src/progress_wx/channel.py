"""与微信、飞书等具体平台无关的消息渠道接口。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from .wechat import QuoteMessage, WechatService


class MessageChannelError(RuntimeError):
    """消息渠道无法安全收发。"""


class MessageChannelOfflineError(MessageChannelError):
    """消息渠道已离线且有限重连耗尽。"""


@dataclass(frozen=True, slots=True)
class ChannelAttachment:
    """由渠道官方 SDK 下载并校验过的本地附件。"""

    path: str
    mime_type: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ChannelReply:
    """平台归一化后的结构化引用回复。"""

    sender_id: str
    content: str
    reply_to_message_id: str = ""
    message_id: str = ""
    chat_id: str = ""
    quote_content: str = ""
    message_hash: str = ""
    attachments: tuple[ChannelAttachment, ...] = ()
    attachment_error: str = ""


def codex_prompt_for_reply(message: ChannelReply) -> str:
    """把文字与可信图片路径组合成 Codex 桌面任务可读取的提示词。"""

    content = message.content
    if not message.attachments:
        return content
    lines: list[str] = []
    if content.strip():
        lines.extend((content, ""))
    lines.append("用户通过飞书发送了以下图片，请直接查看图片并结合当前任务处理：")
    for attachment in message.attachments:
        path = str(Path(attachment.path).resolve())
        lines.append(f"- {path}")
    return "\n".join(lines)


ReplyHandler = Callable[[ChannelReply], None]
ChannelErrorHandler = Callable[[BaseException], None]


@runtime_checkable
class MessageChannel(Protocol):
    """主服务使用的最小消息渠道协议。"""

    def start(self, on_reply: ReplyHandler) -> None:
        """连接渠道并开始接收入站消息。"""

    def send_text(
        self, text: str, *, idempotency_key: str
    ) -> str | tuple[str, ...] | None:
        """发送文本，成功时尽量返回平台一个或多个 message_id。"""

    def send_file(
        self,
        data: bytes,
        *,
        file_name: str,
        idempotency_key: str,
    ) -> str | tuple[str, ...] | None:
        """按原始字节发送文件，成功时返回平台 message_id。"""

    def send_image(
        self,
        data: bytes,
        *,
        idempotency_key: str,
    ) -> str | tuple[str, ...] | None:
        """发送可在聊天中直接预览的图片，成功时返回 message_id。"""

    def is_online(self) -> bool:
        """返回当前是否具备可靠收发能力。"""

    def stop(self) -> None:
        """停止接收并释放连接。"""


class WechatMessageChannel:
    """保留旧微信实现的兼容包装，不让核心服务继续依赖微信类型。"""

    def __init__(self, service: WechatService) -> None:
        self._service = service

    def start(self, on_reply: ReplyHandler) -> None:
        def convert(message: QuoteMessage) -> None:
            on_reply(
                ChannelReply(
                    sender_id=str(message.sender or ""),
                    content=message.content,
                    message_id=str(message.message_id or ""),
                    chat_id=message.chat_name,
                    quote_content=message.quote_content,
                    message_hash=str(message.message_hash or ""),
                )
            )

        self._service.start(convert)

    def send_text(self, text: str, *, idempotency_key: str) -> str | None:
        del idempotency_key  # 微信 UI 后端没有可用的服务端幂等键。
        self._service.send_text(text)
        return None

    def send_file(
        self,
        data: bytes,
        *,
        file_name: str,
        idempotency_key: str,
    ) -> str | None:
        del data, file_name, idempotency_key
        raise MessageChannelError("微信兼容后端不支持原文件发送")

    def send_image(
        self,
        data: bytes,
        *,
        idempotency_key: str,
    ) -> str | None:
        del data, idempotency_key
        raise MessageChannelError("微信兼容后端不支持图片发送")

    def is_online(self) -> bool:
        return self._service.is_online()

    def stop(self) -> None:
        self._service.stop()
