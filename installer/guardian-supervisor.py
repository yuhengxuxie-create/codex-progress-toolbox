"""One bounded Windows recovery check. No network client or business DB access."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid


def current_user_sid() -> str:
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not 0 < size.value <= 65536:
            raise RuntimeError("invalid_token_size")
        buffer = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        value = wintypes.LPWSTR()
        if not security.ConvertSidToStringSidW(sid, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return value.value
        finally:
            kernel.LocalFree(ctypes.cast(value, ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)


def read_json(path: Path) -> dict:
    if path.stat().st_size > 65536:
        raise ValueError("state_too_large")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("invalid_object")
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def evaluate(status: dict, state: dict, now: float) -> tuple[str, dict]:
    """Return a decision without mutation of guardian intent or its database."""
    result = dict(state)
    result.update(schema_version=1, last_check_at=now)
    if status.get("schema_version") != 1 or not status.get("available"):
        result["last_error_code"] = "status_unavailable"
        return "unavailable", result
    intent = status.get("desired_state")
    if intent not in ("running", "stopped", "maintenance", "exited"):
        result["last_error_code"] = "invalid_intent"
        return "unavailable", result
    if intent in ("maintenance", "exited"):
        result.update(suspect_since=None, last_error_code="intent_suppressed")
        return "suppressed", result
    guardian = status.get("guardian")
    if not isinstance(guardian, dict):
        result["last_error_code"] = "invalid_guardian_status"
        return "unavailable", result
    heartbeat = guardian.get("heartbeat_at")
    if heartbeat is not None and (not isinstance(heartbeat, (int, float)) or isinstance(heartbeat, bool)):
        result["last_error_code"] = "invalid_heartbeat"
        return "unavailable", result
    previous = state.get("last_check_at", now)
    if now < previous - 10 or (heartbeat is not None and heartbeat > now + 10):
        result["last_error_code"] = "clock_skew"
        return "unavailable", result
    if guardian.get("running") is True and guardian.get("healthy") is True and heartbeat is not None and now - heartbeat <= 15:
        since = state.get("healthy_since") or now
        result.update(healthy_since=since, suspect_since=None, last_error_code=None)
        if now - since >= 120 and not state.get("circuit_open"):
            result.update(attempts=[], next_attempt_at=0)
        return "healthy", result
    result["healthy_since"] = None
    if state.get("circuit_open"):
        result["last_error_code"] = "recovery_circuit_open"
        return "circuit_open", result
    # Startup and scheduling jitter must not be mistaken for a hung instance.
    since = state.get("suspect_since")
    result["suspect_since"] = now if since is None else since
    observation_seconds = 90 if guardian.get("running") is True else 15
    if since is None or now - since < observation_seconds:
        return "observing", result
    attempts = [stamp for stamp in state.get("attempts", []) if now - 900 <= stamp <= now + 10]
    if len(attempts) >= 3:
        result.update(circuit_open=True, last_error_code="recovery_circuit_open")
        return "circuit_open", result
    if now < state.get("next_attempt_at", 0):
        return "backoff", result
    running = guardian.get("running")
    if running is True and heartbeat is not None and now - heartbeat < 15:
        return "observing", result
    if running not in (True, False):
        result["last_error_code"] = "invalid_process_state"
        return "unavailable", result
    pid, created = guardian.get("pid"), guardian.get("creation_time")
    if pid is None and created is None and running is False:
        pid, created = 0, 0
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in (pid, created)):
        result["last_error_code"] = "invalid_process_identity"
        return "unavailable", result
    if running and (pid == 0 or created == 0):
        result["last_error_code"] = "missing_process_identity"
        return "unavailable", result
    attempts.append(now)
    result.update(attempts=attempts, next_attempt_at=now + (60, 180, 600)[len(attempts)-1],
                  expected_pid=pid, expected_creation_time=created, suspect_since=None)
    return "unresponsive" if running else "crashed", result


def run_cli(config: dict, *arguments: str, timeout: float) -> dict:
    command = [config["python"], "-B", str(Path(config["backend"]) / "progress-wx.py"),
               "--config", str(Path(config["backend"]) / "config.yaml"), *arguments]
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if os.fstat(output.fileno()).st_size > 65536:
                    raise RuntimeError("cli_output_too_large")
                if time.monotonic() >= deadline:
                    raise TimeoutError("cli_timeout")
                time.sleep(0.05)
            if process.returncode:
                raise RuntimeError("cli_exit_" + str(process.returncode))
            output.seek(0)
            payload = output.read(65537)
            if len(payload) > 65536:
                raise RuntimeError("cli_output_too_large")
        finally:
            if process.poll() is None:
                process.kill()  # Only this supervisor's own short-lived CLI child.
                process.wait(timeout=5)
    data = json.loads(payload.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise RuntimeError("cli_invalid_json")
    return data


def log_event(directory: Path, event: str, state: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "windows-supervisor.jsonl"
    if path.exists() and path.stat().st_size > 1048576:
        older = directory / "windows-supervisor.2.jsonl"
        previous = directory / "windows-supervisor.1.jsonl"
        if previous.exists():
            os.replace(previous, older)
        os.replace(path, previous)
    # Never log CLI stdout/stderr, credentials, paths, task IDs or user content.
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"at": time.time(), "event": event,
                                "attempt_count": len(state.get("attempts", [])),
                                "error_code": state.get("last_error_code")}) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve(strict=True)
    if not any((root / marker).is_file() for marker in (".codex-feishu-ecosystem-root", ".codex-feishu-guardian-root")):
        return 2
    config = read_json(root / ".ecosystem/guardian-supervision.json")
    if config.get("owner_sid") != current_user_sid():
        return 2
    state_path = Path(config["state_file"]).resolve()
    for path in (Path(config["python"]).resolve(), Path(config["backend"]).resolve(), state_path):
        if not path.is_relative_to(root):
            return 2
    if config.get("enabled") is not True:
        return 0
    # Scheduler IgnoreNew plus an OS byte-range lock also protects manual runs.
    import msvcrt
    lock_path = root / ".ecosystem/guardian-supervisor.lock"
    with lock_path.open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0"); lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return 0
        try:
            state = read_json(state_path) if state_path.exists() else {"schema_version": 1, "attempts": []}
            try:
                status = run_cli(config, "guardian-status", "--json", timeout=5)
                action, state = evaluate(status, state, time.time())
                # Persist the attempt before recovery, even if the machine loses power.
                write_json(state_path, state)
                if action in ("crashed", "unresponsive"):
                    try:
                        run_cli(config, "guardian-recover", "--expected-pid", str(state["expected_pid"]),
                                "--expected-creation-time", str(state["expected_creation_time"]),
                                "--reason", action, timeout=35)
                    except Exception as error:
                        state["last_error_code"] = "recovery_" + type(error).__name__
                        write_json(state_path, state)
                if action != "healthy" or state.get("last_error_code"):
                    log_event(state_path.parent, action, state)
            except Exception as error:
                state.update(last_check_at=time.time(), last_error_code="check_" + type(error).__name__)
                write_json(state_path, state)
                log_event(state_path.parent, "check_failed", state)
        finally:
            lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
