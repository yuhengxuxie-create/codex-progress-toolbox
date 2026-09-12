"""Contract tests through the installed lark-channel public SDK surface.

Only the SDK's lowest HTTP ``acreate`` calls are replaced.  The tests still
exercise ``FeishuChannel`` -> resolver -> ``LarkClientDriver`` and the real
sender/coercion path.
"""

from __future__ import annotations

import asyncio
import json
import socket
from types import SimpleNamespace

import pytest

from lark_channel.channel import (
    FeishuChannel,
    MediaSource,
    OutboundConfig,
    RetryConfig,
)
from lark_channel.channel.driver import LarkClientDriver


@pytest.fixture(autouse=True)
def _network_is_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect
    real_create_connection = socket.create_connection

    def guarded_connect(sock, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) and address else address
        if host in {"127.0.0.1", "::1", "localhost"}:
            return real_connect(sock, address, *args, **kwargs)
        raise AssertionError("SDK media contract test attempted network I/O")

    def guarded_create_connection(address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) and address else address
        if host in {"127.0.0.1", "::1", "localhost"}:
            return real_create_connection(address, *args, **kwargs)
        raise AssertionError("SDK media contract test attempted network I/O")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)


def _ok_response(**data: str) -> SimpleNamespace:
    return SimpleNamespace(code=0, msg="ok", data=SimpleNamespace(**data))


def _channel(*, one_shot: bool = False) -> FeishuChannel:
    outbound = None
    if one_shot:
        outbound = OutboundConfig(retry=RetryConfig(max_attempts=1, base_delay_ms=0))
    return FeishuChannel(
        app_id="cli_sdk_contract",
        app_secret="secret",
        outbound=outbound,
    )


def test_public_upload_media_reaches_real_driver_multipart_for_image_and_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _channel()
    image_requests: list[object] = []
    file_requests: list[object] = []

    async def image_acreate(request):
        image_requests.append(request)
        return _ok_response(image_key="img_sdk_contract")

    async def file_acreate(request):
        file_requests.append(request)
        return _ok_response(file_key="file_sdk_contract")

    monkeypatch.setattr(channel.client.im.v1.image, "acreate", image_acreate)
    monkeypatch.setattr(channel.client.im.v1.file, "acreate", file_acreate)

    image_bytes = b"\x89PNG\r\n\x1a\ncontract-image"
    file_bytes = b"contract-file-stream"
    image_key = asyncio.run(
        channel.upload_media(
            MediaSource(kind="buffer", buffer=image_bytes),
            kind="image",
            file_name="original-preview.png",
        )
    )
    file_key = asyncio.run(
        channel.upload_media(
            MediaSource(kind="buffer", buffer=file_bytes),
            kind="file",
            file_name="original-report.bin",
        )
    )

    assert image_key == "img_sdk_contract"
    assert file_key == "file_sdk_contract"
    assert isinstance(channel._sender._driver.create_message.__self__, LarkClientDriver)

    assert len(image_requests) == 1
    image_body = image_requests[0].request_body
    assert image_body.image_type == "message"
    assert image_body.image.name == "original-preview.png"
    assert image_body.image.getvalue() == image_bytes

    assert len(file_requests) == 1
    file_body = file_requests[0].request_body
    assert file_body.file_type == "stream"
    assert file_body.file_name == "original-report.bin"
    assert file_body.file.name == "original-report.bin"
    assert file_body.file.getvalue() == file_bytes


def test_cached_file_key_uses_real_send_path_and_unknown_retry_does_not_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The second call below is an explicit caller retry after an uncertain
    # result.  Disable the SDK's internal retry loop so this test does not
    # accidentally claim queue-level recovery semantics.
    channel = _channel(one_shot=True)
    upload_requests: list[object] = []
    message_requests: list[object] = []

    async def file_acreate(request):
        upload_requests.append(request)
        return _ok_response(file_key="file_cached_contract")

    message_results = iter(
        (
            RuntimeError("message result unknown"),
            _ok_response(message_id="om_cached_contract"),
        )
    )

    async def message_acreate(request):
        message_requests.append(request)
        outcome = next(message_results)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(channel.client.im.v1.file, "acreate", file_acreate)
    monkeypatch.setattr(channel.client.im.v1.message, "acreate", message_acreate)

    payload = b"cache-before-message"
    file_key = asyncio.run(
        channel.upload_media(
            MediaSource(kind="buffer", buffer=payload),
            kind="file",
            file_name="cached.bin",
        )
    )
    assert file_key == "file_cached_contract"

    def send_cached_file():
        return asyncio.run(
            channel.send(
                "oc_contract",
                {"file": {"source": file_key, "file_name": "cached.bin"}},
                {"uuid": "stable-contract-uuid"},
            )
        )

    first = send_cached_file()
    second = send_cached_file()

    assert first.success is False
    assert first.error is not None
    assert second.success is True
    assert second.message_id == "om_cached_contract"
    assert len(upload_requests) == 1
    assert len(message_requests) == 2

    for request in message_requests:
        body = request.request_body
        assert body.msg_type == "file"
        assert json.loads(body.content) == {"file_key": "file_cached_contract"}
        assert body.uuid == "stable-contract-uuid"
