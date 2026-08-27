"""使用 Windows 当前用户 DPAPI 保存飞书应用密钥。

本模块不把 App Secret 明文写入配置文件，也不依赖 ``pywin32``。生产模式
通过 Windows 原生 ``CryptProtectData``/``CryptUnprotectData`` 接口加密，
加密范围固定为当前 Windows 用户，其他用户不能直接解密文件。

测试时可以注入 ``protector`` 和 ``unprotector``，这样在非 Windows 环境也
能够验证文件格式、原子写入和业务流程；未注入时，非 Windows 平台会明确
拒绝保存或读取，而不是静默降级为明文。
"""

from __future__ import annotations

from collections.abc import Callable
import ctypes
import ctypes.wintypes as wintypes
import getpass
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
from typing import Any, Final


class SecretStoreError(RuntimeError):
    """密钥存储失败的基类异常。"""


class SecretStorePlatformError(SecretStoreError):
    """当前平台没有可用的 Windows DPAPI。"""


class SecretStoreFormatError(SecretStoreError):
    """密钥文件格式错误或文件已损坏。"""


class SecretStoreCryptoError(SecretStoreError):
    """DPAPI 或测试加密后端处理失败。"""


Protector = Callable[[bytes], bytes]
AclTightener = Callable[[Path], None]

_FILE_MAGIC: Final[bytes] = b"PROGRESS-WX-DPAPI\x01"
_MAX_PROTECTED_BYTES: Final[int] = 1024 * 1024
_CRYPTPROTECT_UI_FORBIDDEN: Final[int] = 0x1


class _DataBlob(ctypes.Structure):
    """Windows DATA_BLOB 结构；仅在调用原生 DPAPI 时使用。"""

    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _is_windows() -> bool:
    """集中判断平台，方便测试通过猴补替换。"""

    return os.name == "nt"


def _local_free(pointer: Any) -> None:
    """释放 CryptUnprotectData/CryptProtectData 分配的缓冲区。"""

    if not pointer:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    local_free(pointer)


def _dpapi_protect(value: bytes) -> bytes:
    """调用 CryptProtectData，以当前 Windows 用户为加密范围。"""

    if not _is_windows():
        raise SecretStorePlatformError("DPAPI 生产后端仅支持 Windows")
    if not value:
        raise SecretStoreCryptoError("不能保护空密钥")

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    protect = crypt32.CryptProtectData
    protect.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    protect.restype = wintypes.BOOL

    source = ctypes.create_string_buffer(value)
    source_blob = _DataBlob(
        len(value), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte))
    )
    result_blob = _DataBlob()
    ok = protect(
        ctypes.byref(source_blob),
        "ProgressChecking(WX) Feishu App Secret",
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(result_blob),
    )
    if not ok:
        error_code = ctypes.get_last_error()
        raise SecretStoreCryptoError(
            f"CryptProtectData 失败，Windows 错误码 {error_code}"
        )
    try:
        if not result_blob.pbData or not result_blob.cbData:
            raise SecretStoreCryptoError("CryptProtectData 返回空数据")
        return ctypes.string_at(result_blob.pbData, result_blob.cbData)
    finally:
        _local_free(result_blob.pbData)


def _dpapi_unprotect(value: bytes) -> bytes:
    """调用 CryptUnprotectData，以当前 Windows 用户解密。"""

    if not _is_windows():
        raise SecretStorePlatformError("DPAPI 生产后端仅支持 Windows")
    if not value:
        raise SecretStoreCryptoError("不能解保护空数据")

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    unprotect = crypt32.CryptUnprotectData
    unprotect.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    unprotect.restype = wintypes.BOOL

    source = ctypes.create_string_buffer(value)
    source_blob = _DataBlob(
        len(value), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte))
    )
    result_blob = _DataBlob()
    description = wintypes.LPWSTR()
    ok = unprotect(
        ctypes.byref(source_blob),
        ctypes.byref(description),
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(result_blob),
    )
    if not ok:
        error_code = ctypes.get_last_error()
        raise SecretStoreCryptoError(
            f"CryptUnprotectData 失败，Windows 错误码 {error_code}"
        )
    try:
        if not result_blob.pbData or not result_blob.cbData:
            raise SecretStoreCryptoError("CryptUnprotectData 返回空数据")
        return ctypes.string_at(result_blob.pbData, result_blob.cbData)
    finally:
        _local_free(result_blob.pbData)
        if description:
            _local_free(ctypes.cast(description, ctypes.c_void_p))


def _current_account_name() -> str:
    """获取当前 Windows 账户名，避免把 ACL 授权给所有用户。"""

    user = str(os.environ.get("USERNAME") or getpass.getuser() or "").strip()
    domain = str(os.environ.get("USERDOMAIN") or "").strip()
    if not user:
        raise SecretStoreError("无法确定当前 Windows 用户，不能收紧密钥文件 ACL")
    return f"{domain}\\{user}" if domain else user


def _tighten_acl_with_icacls(path: Path) -> None:
    """移除继承权限，只给当前 Windows 账户完全控制权限。"""

    if not _is_windows():
        return
    account = _current_account_name()
    try:
        result = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{account}:F",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            shell=False,
        )
    except (OSError, UnicodeError) as exc:
        raise SecretStoreError("调用 icacls 收紧密钥文件 ACL 失败") from exc
    if result.returncode != 0:
        # 不把 stdout/stderr 原样写入日志，避免未来命令输出意外包含密钥。
        raise SecretStoreError(
            f"icacls 收紧密钥文件 ACL 失败，退出码 {result.returncode}"
        )


def _restrict_basic_permissions(path: Path) -> None:
    """在所有平台尽量去掉组/其他用户权限；Windows 仍以 ACL 为准。"""

    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        raise SecretStoreError("无法收紧密钥文件的基础文件权限") from exc


class DpapiSecretStore:
    """以单个文件保存一个当前用户 DPAPI 密钥。

    ``protector``/``unprotector`` 只用于测试依赖注入；生产调用不传它们，
    由本模块自动调用 Windows DPAPI。文件写入采用同目录临时文件加
    ``os.replace``，因此不会出现半写入的密钥文件。
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        protector: Protector | None = None,
        unprotector: Protector | None = None,
        acl_tightener: AclTightener | None = None,
    ) -> None:
        if (protector is None) != (unprotector is None):
            raise ValueError("protector 和 unprotector 必须同时提供")
        self.path = Path(path).expanduser()
        self._protector = protector
        self._unprotector = unprotector
        self._acl_tightener = acl_tightener
        self._lock = threading.RLock()

    @property
    def using_injected_backend(self) -> bool:
        """返回是否使用了测试注入后端。"""

        return self._protector is not None

    def _protect(self, value: bytes) -> bytes:
        """选择注入后端或 Windows 原生 DPAPI。"""

        if self._protector is not None:
            result = self._protector(value)
        else:
            result = _dpapi_protect(value)
        if not isinstance(result, bytes) or not result:
            raise SecretStoreCryptoError("加密后端必须返回非空 bytes")
        return result

    def _unprotect(self, value: bytes) -> bytes:
        """选择注入后端或 Windows 原生 DPAPI。"""

        if self._unprotector is not None:
            result = self._unprotector(value)
        else:
            result = _dpapi_unprotect(value)
        if not isinstance(result, bytes) or not result:
            raise SecretStoreCryptoError("解密后端必须返回非空 bytes")
        return result

    def _ensure_platform(self) -> None:
        """生产模式禁止在非 Windows 上伪装成 DPAPI。"""

        if not _is_windows() and not self.using_injected_backend:
            raise SecretStorePlatformError(
                "生产密钥存储需要 Windows 当前用户 DPAPI；"
                "非 Windows 仅允许注入测试后端"
            )

    def _apply_acl(self, path: Path) -> None:
        """应用显式 ACL；测试可注入检查器，生产默认使用 icacls。"""

        _restrict_basic_permissions(path)
        if self._acl_tightener is not None:
            self._acl_tightener(path)
        elif _is_windows():
            _tighten_acl_with_icacls(path)

    def save(self, secret: str) -> None:
        """加密并原子保存 App Secret。"""

        if not isinstance(secret, str) or not secret:
            raise ValueError("secret 必须是非空字符串")
        self._ensure_platform()
        try:
            plain = secret.encode("utf-8")
            protected = self._protect(plain)
        except Exception:
            # 加密后端异常可能来自第三方注入实现；不要保留异常链，避免
            # 该实现把明文密钥放入异常文本后被日志/traceback 记录。
            crypto_error = SecretStoreCryptoError("加密 App Secret 失败")
        else:
            crypto_error = None
        if crypto_error is not None:
            raise crypto_error
        if len(protected) > _MAX_PROTECTED_BYTES:
            raise SecretStoreCryptoError("加密密钥数据超过允许大小")
        payload = _FILE_MAGIC + len(protected).to_bytes(4, "big") + protected

        with self._lock:
            parent = self.path.parent
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise SecretStoreError("无法创建密钥文件目录") from exc
            temporary: Path | None = None
            try:
                fd, temporary_name = tempfile.mkstemp(
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    dir=str(parent),
                )
                temporary = Path(temporary_name)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._apply_acl(temporary)
                # Windows 保留源文件的安全描述符，os.replace 后目标继续使用
                # 临时文件已经收紧的 ACL；这样不会出现短暂的宽松目标文件。
                os.replace(temporary, self.path)
                temporary = None
            except SecretStoreError:
                raise
            except OSError as exc:
                raise SecretStoreError("原子写入密钥文件失败") from exc
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        # 原始异常更重要；清理失败不覆盖它。
                        pass

    def load(self) -> str | None:
        """读取并解密 App Secret；文件不存在时返回 ``None``。"""

        with self._lock:
            try:
                raw = self.path.read_bytes()
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise SecretStoreError("无法读取密钥文件") from exc
        self._ensure_platform()
        if len(raw) < len(_FILE_MAGIC) + 4 or not raw.startswith(_FILE_MAGIC):
            raise SecretStoreFormatError("密钥文件头无效")
        length_start = len(_FILE_MAGIC)
        protected_length = int.from_bytes(
            raw[length_start : length_start + 4], "big"
        )
        protected = raw[length_start + 4 :]
        if (
            protected_length <= 0
            or protected_length > _MAX_PROTECTED_BYTES
            or protected_length != len(protected)
        ):
            raise SecretStoreFormatError("密钥文件长度无效")
        try:
            plain = self._unprotect(protected)
            secret = plain.decode("utf-8")
        except Exception:
            # 解密边界同样不向上暴露后端异常文本；密文、DPAPI 错误详情和
            # 第三方实现的异常均不应进入业务日志。
            crypto_error = SecretStoreCryptoError("解密 App Secret 失败")
        else:
            crypto_error = None
        if crypto_error is not None:
            raise crypto_error
        if not secret:
            raise SecretStoreFormatError("解密后得到空密钥")
        return secret

    # 下面两个名称便于调用方表达“读取/保存密钥”，不复制实现。
    def save_secret(self, secret: str) -> None:
        """``save`` 的语义别名。"""

        self.save(secret)

    def load_secret(self) -> str | None:
        """``load`` 的语义别名。"""

        return self.load()


def save_secret(
    path: str | os.PathLike[str],
    secret: str,
    *,
    protector: Protector | None = None,
    unprotector: Protector | None = None,
    acl_tightener: AclTightener | None = None,
) -> None:
    """使用一次性存储对象保存密钥。"""

    DpapiSecretStore(
        path,
        protector=protector,
        unprotector=unprotector,
        acl_tightener=acl_tightener,
    ).save(secret)


def load_secret(
    path: str | os.PathLike[str],
    *,
    protector: Protector | None = None,
    unprotector: Protector | None = None,
) -> str | None:
    """使用一次性存储对象读取密钥。"""

    return DpapiSecretStore(
        path,
        protector=protector,
        unprotector=unprotector,
    ).load()


__all__ = [
    "DpapiSecretStore",
    "SecretStoreCryptoError",
    "SecretStoreError",
    "SecretStoreFormatError",
    "SecretStorePlatformError",
    "load_secret",
    "save_secret",
]
