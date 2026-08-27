"""DPAPI 密钥存储模块测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from progress_wx import secrets as secrets_module
from progress_wx.secrets import (
    DpapiSecretStore,
    SecretStoreCryptoError,
    SecretStoreFormatError,
    SecretStorePlatformError,
)


def _fake_backend() -> tuple[object, object]:
    """返回可逆但不用于生产的测试加密后端。"""

    marker = b"test-protected:"

    def protect(value: bytes) -> bytes:
        return marker + value[::-1]

    def unprotect(value: bytes) -> bytes:
        assert value.startswith(marker)
        return value[len(marker) :][::-1]

    return protect, unprotect


def test_injected_backend_can_round_trip_and_is_atomic(tmp_path: Path) -> None:
    """注入后端可在当前平台测试，且写入结果不是明文。"""

    path = tmp_path / "nested" / "feishu.secret"
    protect, unprotect = _fake_backend()
    acl_calls: list[Path] = []
    store = DpapiSecretStore(
        path,
        protector=protect,
        unprotector=unprotect,
        acl_tightener=acl_calls.append,
    )

    store.save("应用密钥-测试")

    assert path.exists()
    assert path.read_bytes() != "应用密钥-测试".encode("utf-8")
    # ACL 检查器应作用于同目录临时文件；os.replace 后临时文件路径不再存在。
    assert len(acl_calls) == 1
    assert acl_calls[0].parent == path.parent
    assert not acl_calls[0].exists()
    assert store.load() == "应用密钥-测试"
    assert store.load_secret() == "应用密钥-测试"


def test_missing_file_returns_none(tmp_path: Path) -> None:
    """首次配置时没有密钥文件应返回空值。"""

    protect, unprotect = _fake_backend()
    store = DpapiSecretStore(
        tmp_path / "missing.secret",
        protector=protect,
        unprotector=unprotect,
    )
    assert store.load() is None


def test_non_windows_production_backend_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非 Windows 不得静默退化为明文保存。"""

    monkeypatch.setattr(secrets_module.os, "name", "posix")
    store = DpapiSecretStore(tmp_path / "secret")
    with pytest.raises(SecretStorePlatformError):
        store.save("secret")


def test_one_injected_callback_is_rejected(tmp_path: Path) -> None:
    """测试后端必须同时提供加密和解密函数。"""

    protect, _ = _fake_backend()
    with pytest.raises(ValueError):
        DpapiSecretStore(tmp_path / "secret", protector=protect)


def test_tampered_file_is_rejected(tmp_path: Path) -> None:
    """文件头或长度被篡改时不能把内容当成密钥返回。"""

    path = tmp_path / "secret"
    protect, unprotect = _fake_backend()
    store = DpapiSecretStore(path, protector=protect, unprotector=unprotect)
    store.save("secret")
    path.write_bytes(path.read_bytes()[:-1])

    with pytest.raises(SecretStoreFormatError):
        store.load()


def test_empty_secret_is_rejected(tmp_path: Path) -> None:
    """空字符串不能作为飞书 App Secret 保存。"""

    protect, unprotect = _fake_backend()
    store = DpapiSecretStore(tmp_path / "secret", protector=protect, unprotector=unprotect)
    with pytest.raises(ValueError):
        store.save("")


def test_backend_exception_does_not_expose_plaintext(tmp_path: Path) -> None:
    """后端异常即使包含明文，也不能通过业务异常链泄漏。"""

    secret = "不得出现在异常里的 App Secret"

    def leaking_protector(_: bytes) -> bytes:
        raise RuntimeError(secret)

    def unused_unprotector(value: bytes) -> bytes:
        return value

    store = DpapiSecretStore(
        tmp_path / "secret",
        protector=leaking_protector,
        unprotector=unused_unprotector,
    )
    with pytest.raises(SecretStoreCryptoError) as caught:
        store.save(secret)
    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
