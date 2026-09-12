"""Offline transport tests using the installed lark-channel SDK data types."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from lark_channel.channel.errors import FeishuChannelError, FeishuChannelErrorCode
from lark_channel.channel.types import MediaSource, SendError, SendResult

from progress_wx import feishu
from progress_wx.feishu import (
    FeishuMessageChannel,
    FeishuSendError,
    FeishuSendNotSubmittedError,
    FeishuSendRejectedError,
)


class SdkMediaMock:
    """The real SDK surface used by the adapter's upload/create boundary."""

    def __init__(self, *, upload_error=None, send_outcomes=None) -> None:
        self.handlers: dict[str, object] = {}
        self.custom_event_handlers: dict[str, object] = {}
        self.ws_client = SimpleNamespace(_conn=None)
        self.upload_error = upload_error
        self.send_outcomes = list(send_outcomes or [])
        self.uploads: list[tuple[MediaSource, str, str | None]] = []
        self.sent: list[tuple[str, object, dict[str, str]]] = []

    def on(self, name: str, handler) -> None:
        self.handlers[name] = handler

    def register_custom_event(self, event_type: str, handler) -> None:
        self.custom_event_handlers[event_type] = handler

    async def connect_until_ready(self, *, timeout: float) -> None:
        del timeout
        self.ws_client._conn = object()

    def connection_snapshot(self):
        return SimpleNamespace(ready=self.ws_client._conn is not None)

    async def upload_media(
        self,
        source: MediaSource,
        *,
        kind: str,
        file_name: str | None = None,
    ) -> str:
        assert isinstance(source, MediaSource)
        self.uploads.append((source, kind, file_name))
        if self.upload_error is not None:
            raise self.upload_error
        return f"{kind}_key_1"

    async def send(self, target: str, message: object, options: dict[str, str]):
        self.sent.append((target, message, options))
        if self.send_outcomes:
            outcome = self.send_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return SendResult.ok(
            message_id=f"om_{len(self.sent)}",
            raw={"code": 0, "data": {"chat_id": "oc_private"}},
        )

    async def disconnect(self) -> None:
        self.ws_client._conn = None


def _start_sdk(mock: SdkMediaMock, **kwargs):
    channel = FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id="ou_owner",
        connect_timeout_seconds=5,
        retry_delays=(0, 0, 0, 0, 0),
        sdk_factory=lambda *_args: mock,
        **kwargs,
    )
    channel.start(lambda _reply: None)
    return channel


def test_upload_rejection_keeps_sdk_raw_context_and_is_typed() -> None:
    mock = SdkMediaMock(
        upload_error=FeishuChannelError(
            FeishuChannelErrorCode.UPLOAD_FAILED,
            "redacted uploader message",
            context={"raw_code": 234006, "raw_msg": "size rejected"},
        )
    )
    channel = _start_sdk(mock)
    try:
        with pytest.raises(FeishuSendRejectedError) as captured:
            channel.send_file(b"payload", file_name="report.bin", idempotency_key="k")
        assert captured.value.code == "upload_failed"
        assert captured.value.raw_code == 234006
        assert captured.value.raw_msg == "size rejected"
        assert captured.value.context["raw_code"] == 234006
        assert captured.value.context["raw_msg"] == "size rejected"
        assert captured.value.retryable is False
        assert mock.sent == []
    finally:
        channel.stop()


def test_upload_rate_limit_is_retryable_and_message_is_not_created() -> None:
    mock = SdkMediaMock(
        upload_error=FeishuChannelError(
            FeishuChannelErrorCode.RATE_LIMITED,
            "redacted rate limit",
            context={"raw_code": 429, "raw_msg": "slow down"},
        )
    )
    channel = _start_sdk(mock)
    try:
        with pytest.raises(FeishuSendRejectedError) as captured:
            channel.send_file(b"payload", file_name="report.bin", idempotency_key="k")
        assert captured.value.code == "rate_limited"
        assert captured.value.retryable is True
        assert captured.value.raw_code == 429
        assert mock.sent == []
    finally:
        channel.stop()


def test_uploaded_then_unknown_freezes_and_retry_reuses_file_key_and_uuid() -> None:
    mock = SdkMediaMock(
        send_outcomes=[
            RuntimeError("message response unknown"),
            SendResult.ok(message_id="om_retry"),
        ]
    )
    channel = _start_sdk(mock)
    try:
        with pytest.raises(FeishuSendError) as captured:
            channel.send_file(
                b"payload",
                file_name="report.tar.gz",
                idempotency_key="stable-file",
            )
        assert captured.value.uploaded is True
        assert captured.value.media_key == "file_key_1"
        assert len(mock.uploads) == 1
        assert mock.uploads[0][2] == "report.tar.gz"
        first_message = mock.sent[0][1]
        assert first_message["file"]["source"] == MediaSource(
            kind="key", key="file_key_1"
        )
        assert first_message["file"]["file_name"] == "report.tar.gz"

        assert (
            channel.send_file(
                b"payload",
                file_name="report.tar.gz",
                idempotency_key="stable-file",
            )
            == "om_retry"
        )
        assert len(mock.uploads) == 1
        assert mock.sent[0][2]["uuid"] == mock.sent[1][2]["uuid"]
        second_message = mock.sent[1][1]
        assert second_message["file"]["source"] == MediaSource(
            kind="key", key="file_key_1"
        )
        assert second_message["file"]["file_name"] == "report.tar.gz"
    finally:
        channel.stop()


def test_persistent_media_key_hooks_allow_restart_reuse_without_upload() -> None:
    stored: dict[tuple[str, str, str, str], str] = {}
    writes: list[tuple[str, str, str, str, str]] = []

    def lookup(kind: str, key: str, digest: str, name: str) -> str | None:
        return stored.get((kind, key, digest, name))

    def store(kind: str, key: str, digest: str, name: str, media_key: str) -> None:
        stored[(kind, key, digest, name)] = media_key
        writes.append((kind, key, digest, name, media_key))

    first = SdkMediaMock()
    channel = _start_sdk(first, media_key_lookup=lookup, media_key_store=store)
    try:
        assert channel.send_file(
            b"payload", file_name="report.txt", idempotency_key="persisted"
        ) == "om_1"
    finally:
        channel.stop()
    assert len(first.uploads) == 1
    assert len(writes) == 1

    second = SdkMediaMock()
    restarted = _start_sdk(second, media_key_lookup=lookup, media_key_store=store)
    try:
        assert restarted.send_file(
            b"payload", file_name="report.txt", idempotency_key="persisted"
        ) == "om_1"
    finally:
        restarted.stop()
    assert second.uploads == []
    second_message = second.sent[0][1]
    assert second_message["file"]["source"] == MediaSource(
        kind="key", key="file_key_1"
    )
    assert second_message["file"]["file_name"] == "report.txt"


def test_local_file_boundaries_are_typed_before_network_submission() -> None:
    channel = FeishuMessageChannel(
        app_id="cli_test",
        app_secret="secret",
        target_open_id="ou_owner",
        sdk_factory=lambda *_args: SdkMediaMock(),
    )
    with pytest.raises(FeishuSendRejectedError) as empty:
        channel.send_file(b"", file_name="empty.txt", idempotency_key="empty")
    assert empty.value.raw_code == 234010
    with pytest.raises(FeishuSendRejectedError) as oversized:
        channel.send_file(
            b"x" * (feishu.FEISHU_FILE_MAX_BYTES + 1),
            file_name="large.bin",
            idempotency_key="large",
        )
    assert oversized.value.raw_code == 234006
    with pytest.raises(FeishuSendRejectedError) as image_oversized:
        channel.send_image(
            b"x" * (feishu.FEISHU_IMAGE_MAX_BYTES + 1),
            idempotency_key="large-image",
        )
    assert image_oversized.value.raw_code == 234006


def test_not_connected_sdk_failure_is_not_submitted() -> None:
    mock = SdkMediaMock(
        upload_error=FeishuChannelError(
            FeishuChannelErrorCode.NOT_CONNECTED,
            "not connected",
        )
    )
    channel = _start_sdk(mock)
    try:
        with pytest.raises(FeishuSendNotSubmittedError):
            channel.send_file(b"payload", file_name="a.txt", idempotency_key="k")
    finally:
        channel.stop()
