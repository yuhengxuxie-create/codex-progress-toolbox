"""基于操作系统原语的轻量跨进程互斥。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import tempfile
from typing import BinaryIO


class LockUnavailable(RuntimeError):
    """同一作用域已由另一个进程持有。"""


class InterprocessMutex:
    """Windows 使用命名 Mutex；其它平台使用非阻塞文件锁。"""

    def __init__(self, identity: str, *, global_scope: bool = False) -> None:
        digest = hashlib.sha256(identity.casefold().encode("utf-8")).hexdigest()
        namespace = "Global" if global_scope else "Local"
        self._name = f"{namespace}\\ProgressCheckingWX-{digest}"
        self._digest = digest
        self._handle: int | None = None
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        if self._handle is not None or self._file is not None:
            return
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            ctypes.set_last_error(0)
            handle = kernel32.CreateMutexW(None, False, self._name)
            if not handle:
                raise OSError(ctypes.get_last_error(), "创建 Windows Mutex 失败")
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                raise LockUnavailable("同一作用域已有活动进程")
            # ctypes 在不同 Python/声明组合下可能返回 int 或 c_void_p。
            handle_value = getattr(handle, "value", handle)
            self._handle = int(handle_value)
            return

        # 非 Windows 分支仅用于开发测试；锁文件保留在临时目录，不承载状态。
        import fcntl

        lock_path = Path(tempfile.gettempdir()) / f"progress-checking-wx-{self._digest}.lock"
        handle = lock_path.open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise LockUnavailable("同一作用域已有活动进程") from exc
        self._file = handle

    def close(self) -> None:
        if self._handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = None
        if self._file is not None:
            try:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            finally:
                self._file.close()
                self._file = None

    def __enter__(self) -> "InterprocessMutex":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
