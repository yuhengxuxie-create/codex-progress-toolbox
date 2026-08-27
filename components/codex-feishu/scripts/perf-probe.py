"""不连接飞书网络的空闲核心性能探针。"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import time
from pathlib import Path

from progress_wx.codex_store import CodexStore, StorePaths


class _ProcessMemoryCountersEx(ctypes.Structure):
    """Windows ``PROCESS_MEMORY_COUNTERS_EX`` 的最小本地声明。"""

    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("page_fault_count", ctypes.c_ulong),
        ("peak_working_set_size", ctypes.c_size_t),
        ("working_set_size", ctypes.c_size_t),
        ("quota_peak_paged_pool_usage", ctypes.c_size_t),
        ("quota_paged_pool_usage", ctypes.c_size_t),
        ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
        ("quota_non_paged_pool_usage", ctypes.c_size_t),
        ("pagefile_usage", ctypes.c_size_t),
        ("peak_pagefile_usage", ctypes.c_size_t),
        ("private_usage", ctypes.c_size_t),
    ]


def _working_set_bytes() -> int:
    """只调用 Windows 本机 API，不额外引入性能测量依赖。"""

    if os.name != "nt":
        raise RuntimeError("性能探针仅支持本项目目标平台 Windows")
    counters = _ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_ProcessMemoryCountersEx),
        ctypes.c_ulong,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    if not psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo 失败")
    return int(max(counters.working_set_size, counters.peak_working_set_size))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-home", default=str(Path.home() / ".codex"))
    parser.add_argument("--seconds", type=float, default=12)
    parser.add_argument(
        "--include-feishu-sdk",
        action="store_true",
        help="计入官方飞书 SDK 及严格安全策略对象的静态内存开销",
    )
    args = parser.parse_args()
    if args.include_feishu_sdk:
        # 只构造对象、不发起网络连接；真实 WebSocket 必须在配置后用
        # measure-running.ps1 复测。
        from progress_wx.feishu import _official_sdk_factory

        _official_sdk_factory("cli_measure", "not-a-real-secret", "ou_measure", 5)
    store = CodexStore(paths=StorePaths.from_codex_home(args.codex_home))
    ids = [item.thread_id for item in store.select_threads()[:5]]
    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    peak_rss = _working_set_bytes()
    polls = 0
    deadline = wall_start + max(4, args.seconds)
    while time.perf_counter() < deadline:
        for thread_id in ids:
            store.snapshot(thread_id)
        polls += 1
        peak_rss = max(peak_rss, _working_set_bytes())
        time.sleep(min(2, max(0, deadline - time.perf_counter())))
    elapsed = time.perf_counter() - wall_start
    cpu_delta = time.process_time() - cpu_start
    single_core_percent = cpu_delta / elapsed * 100
    total_machine_percent = single_core_percent / max(1, os.cpu_count() or 1)
    result = {
        "elapsed_seconds": round(elapsed, 3),
        "polls": polls,
        "threads_per_poll": len(ids),
        "feishu_sdk_loaded": bool(args.include_feishu_sdk),
        "cpu_single_core_percent": round(single_core_percent, 3),
        "cpu_task_manager_percent": round(total_machine_percent, 3),
        "peak_rss_mb": round(peak_rss / 1024 / 1024, 3),
        "limits": {"cpu_percent": 1.0, "rss_mb": 100.0},
    }
    result["pass"] = total_machine_percent < 1 and peak_rss < 100 * 1024 * 1024
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
