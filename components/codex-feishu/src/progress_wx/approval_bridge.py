"""File-backed bridge between the global Codex permission hook and Feishu."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 256 * 1024
MAX_TEXT_CHARS = 4000


class ApprovalBridgeError(RuntimeError):
    """Approval data failed validation or could not be committed safely."""


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    request_id: str
    session_id: str
    turn_id: str
    cwd: str
    model: str
    tool_name: str
    permission_mode: str
    tool_input: Mapping[str, Any]
    reusable_prefix: tuple[str, ...]
    created_at: int
    expires_at: int
    digest: str


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _secret(path: Path) -> bytes:
    try:
        encoded = path.read_text(encoding="ascii").strip()
        secret = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (OSError, ValueError) as exc:
        raise ApprovalBridgeError("无法读取审批桥签名密钥") from exc
    if len(secret) < 32:
        raise ApprovalBridgeError("审批桥签名密钥长度不足")
    return secret


def _sign(secret: bytes, payload: Mapping[str, Any]) -> str:
    return hmac.new(secret, _canonical(payload), hashlib.sha256).hexdigest()


def _safe_id(value: object) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 32 or any(char not in "0123456789abcdef" for char in text):
        raise ApprovalBridgeError("审批请求 ID 无效")
    return text


def _text(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    result = str(value or "").strip()
    return result[:limit]


def _exact_prefix(tool_input: Mapping[str, Any]) -> tuple[str, ...]:
    raw = tool_input.get("prefix_rule")
    if raw is None:
        raw = tool_input.get("prefixRule")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 64:
        return ()
    values = tuple(str(item) for item in raw)
    if any(not value or len(value) > 1000 or "\x00" in value for value in values):
        return ()
    return values


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _canonical(payload) + b"\n"
    if len(data) > MAX_REQUEST_BYTES:
        raise ApprovalBridgeError("审批桥消息过大")
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        if path.stat().st_size > MAX_REQUEST_BYTES:
            raise ApprovalBridgeError("审批桥消息过大")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ApprovalBridgeError("审批桥消息无法读取") from exc
    if not isinstance(payload, Mapping):
        raise ApprovalBridgeError("审批桥消息不是对象")
    return payload


class ApprovalBridge:
    def __init__(self, root: Path, secret_file: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.secret_file = Path(secret_file).expanduser().resolve()
        self.requests_dir = self.root / "requests"
        self.responses_dir = self.root / "responses"
        self._secret = _secret(self.secret_file)
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)

    def submit(self, event: Mapping[str, Any], *, timeout_seconds: int) -> ApprovalRequest:
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds 必须大于 0")
        tool_input = event.get("tool_input")
        if not isinstance(tool_input, Mapping):
            tool_input = {}
        now = int(time.time())
        request_id = secrets.token_hex(16)
        body: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "session_id": _text(event.get("session_id"), 200),
            "turn_id": _text(event.get("turn_id"), 200),
            "cwd": _text(event.get("cwd")),
            "model": _text(event.get("model"), 200),
            "tool_name": _text(event.get("tool_name"), 200),
            "permission_mode": _text(event.get("permission_mode"), 200),
            "tool_input": dict(tool_input),
            "reusable_prefix": list(_exact_prefix(tool_input)),
            "created_at": now,
            "expires_at": now + int(timeout_seconds),
        }
        digest = hashlib.sha256(_canonical(body)).hexdigest()
        body["digest"] = digest
        body["signature"] = _sign(self._secret, body)
        _atomic_json(self.requests_dir / f"{request_id}.json", body)
        return self._parse_request(body)

    def _parse_request(self, payload: Mapping[str, Any]) -> ApprovalRequest:
        body = dict(payload)
        signature = str(body.pop("signature", ""))
        if not signature or not hmac.compare_digest(signature, _sign(self._secret, body)):
            raise ApprovalBridgeError("审批请求签名无效")
        if body.get("schema_version") != SCHEMA_VERSION:
            raise ApprovalBridgeError("审批请求版本不受支持")
        request_id = _safe_id(body.get("request_id"))
        tool_input = body.get("tool_input")
        if not isinstance(tool_input, Mapping):
            raise ApprovalBridgeError("审批请求 tool_input 无效")
        raw_prefix = body.get("reusable_prefix")
        reusable_prefix = (
            tuple(str(item) for item in raw_prefix)
            if isinstance(raw_prefix, list)
            else ()
        )
        if reusable_prefix != _exact_prefix(tool_input):
            raise ApprovalBridgeError("审批请求复用规则与原始输入不一致")
        digest_body = dict(body)
        digest = str(digest_body.pop("digest", ""))
        if not digest or not hmac.compare_digest(
            digest, hashlib.sha256(_canonical(digest_body)).hexdigest()
        ):
            raise ApprovalBridgeError("审批请求摘要无效")
        try:
            created_at = int(body.get("created_at"))
            expires_at = int(body.get("expires_at"))
        except (TypeError, ValueError) as exc:
            raise ApprovalBridgeError("审批请求时间无效") from exc
        return ApprovalRequest(
            request_id=request_id,
            session_id=_text(body.get("session_id"), 200),
            turn_id=_text(body.get("turn_id"), 200),
            cwd=_text(body.get("cwd")),
            model=_text(body.get("model"), 200),
            tool_name=_text(body.get("tool_name"), 200),
            permission_mode=_text(body.get("permission_mode"), 200),
            tool_input=dict(tool_input),
            reusable_prefix=reusable_prefix,
            created_at=created_at,
            expires_at=expires_at,
            digest=digest,
        )

    def load_request(self, request_id: str) -> ApprovalRequest:
        safe = _safe_id(request_id)
        return self._parse_request(_read_json(self.requests_dir / f"{safe}.json"))

    def pending(self, *, now: int | None = None) -> tuple[ApprovalRequest, ...]:
        timestamp = int(time.time()) if now is None else int(now)
        result: list[ApprovalRequest] = []
        for path in sorted(self.requests_dir.glob("*.json")):
            try:
                request = self.load_request(path.stem)
            except ApprovalBridgeError:
                continue
            if request.expires_at < timestamp:
                continue
            if (self.responses_dir / f"{request.request_id}.json").exists():
                continue
            result.append(request)
        return tuple(result)

    def respond(self, request_id: str, decision: str) -> None:
        request = self.load_request(request_id)
        normalized = str(decision or "").strip().casefold()
        if normalized not in {"allow", "allow_similar", "deny"}:
            raise ApprovalBridgeError("审批决定无效")
        if normalized == "allow_similar" and not request.reusable_prefix:
            raise ApprovalBridgeError("本次请求没有 Codex 明确提供的可复用规则")
        path = self.responses_dir / f"{request.request_id}.json"
        if path.exists():
            existing = dict(_read_json(path))
            signature = str(existing.pop("signature", ""))
            if not signature or not hmac.compare_digest(
                signature, _sign(self._secret, existing)
            ):
                raise ApprovalBridgeError("已有审批响应签名无效")
            if existing.get("decision") == normalized:
                return
            raise ApprovalBridgeError("本次审批已经写入另一项决定")
        body: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "request_id": request.request_id,
            "request_digest": request.digest,
            "decision": normalized,
            "created_at": int(time.time()),
        }
        body["signature"] = _sign(self._secret, body)
        _atomic_json(path, body)

    def wait_for_response(
        self,
        request: ApprovalRequest,
        *,
        poll_seconds: float = 0.25,
    ) -> str | None:
        path = self.responses_dir / f"{request.request_id}.json"
        while time.time() <= request.expires_at:
            if path.exists():
                payload = dict(_read_json(path))
                signature = str(payload.pop("signature", ""))
                if not signature or not hmac.compare_digest(
                    signature, _sign(self._secret, payload)
                ):
                    raise ApprovalBridgeError("审批响应签名无效")
                if (
                    payload.get("schema_version") != SCHEMA_VERSION
                    or payload.get("request_id") != request.request_id
                    or payload.get("request_digest") != request.digest
                ):
                    raise ApprovalBridgeError("审批响应与请求不匹配")
                decision = str(payload.get("decision") or "").casefold()
                if decision not in {"allow", "allow_similar", "deny"}:
                    raise ApprovalBridgeError("审批响应决定无效")
                return decision
            time.sleep(max(0.05, min(1.0, poll_seconds)))
        return None

    def complete(self, request: ApprovalRequest) -> None:
        for path in (
            self.requests_dir / f"{request.request_id}.json",
            self.responses_dir / f"{request.request_id}.json",
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _rule_line(prefix: Sequence[str]) -> str:
    return "prefix_rule(pattern=" + json.dumps(list(prefix), ensure_ascii=False) + ', decision="allow")\n'


def persist_execpolicy_rule(
    rules_file: Path,
    prefix: Sequence[str],
    *,
    codex_command: str | Sequence[str],
) -> bool:
    values = tuple(str(item) for item in prefix)
    if not values or any(not item or "\x00" in item for item in values):
        raise ApprovalBridgeError("可复用规则无效")
    path = Path(rules_file).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    lock_fd: int | None = None
    deadline = time.monotonic() + 15.0
    while lock_fd is None:
        try:
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > 60
            except OSError:
                stale = False
            if stale:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise ApprovalBridgeError("等待 Codex 规则文件锁超时")
            time.sleep(0.05)
    temporary_path: Path | None = None
    try:
        os.write(lock_fd, f"{os.getpid()}\n".encode("ascii"))
        line = _rule_line(values)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if line in existing.splitlines(keepends=True):
            return False
        candidate = existing
        if candidate and not candidate.endswith("\n"):
            candidate += "\n"
        candidate += line
        fd, temporary = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(candidate)
            handle.flush()
            os.fsync(handle.fileno())
        if isinstance(codex_command, str):
            argv = [codex_command]
        else:
            argv = list(codex_command)
        if not argv or not str(argv[0]).strip():
            raise ApprovalBridgeError("Codex 命令为空")
        suffix = Path(str(argv[0])).suffix.casefold()
        if os.name == "nt" and suffix in {".cmd", ".bat"}:
            command_processor = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
            if not command_processor:
                raise ApprovalBridgeError("运行 Codex 规则校验需要 cmd.exe")
            argv = [command_processor, "/d", "/c", *argv]
        completed = subprocess.run(
            [*argv, "execpolicy", "check", "--rules", str(temporary_path), *values],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            raise ApprovalBridgeError("Codex 拒绝了待写入的可复用规则")
        try:
            checked = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ApprovalBridgeError("Codex 规则校验未返回有效 JSON") from exc
        matched = checked.get("matchedRules") if isinstance(checked, Mapping) else None
        exact_match = any(
            isinstance(item, Mapping)
            and isinstance(item.get("prefixRuleMatch"), Mapping)
            and item["prefixRuleMatch"].get("matchedPrefix") == list(values)
            and item["prefixRuleMatch"].get("decision") == "allow"
            for item in (matched if isinstance(matched, list) else [])
        )
        if (
            not isinstance(checked, Mapping)
            or checked.get("decision") != "allow"
            or not exact_match
        ):
            raise ApprovalBridgeError("Codex 未确认待写入规则精确匹配并允许该前缀")
        assert temporary_path is not None
        os.replace(temporary_path, path)
        return True
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        if lock_fd is not None:
            os.close(lock_fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def permission_hook_result(
    event: Mapping[str, Any],
    *,
    bridge: ApprovalBridge,
    timeout_seconds: int,
    rules_file: Path,
    codex_command: str | Sequence[str],
) -> Mapping[str, Any] | None:
    request = bridge.submit(event, timeout_seconds=timeout_seconds)
    try:
        decision = bridge.wait_for_response(request)
        if decision is None:
            return None
        if decision == "allow_similar":
            persist_execpolicy_rule(
                rules_file,
                request.reusable_prefix,
                codex_command=codex_command,
            )
            decision = "allow"
        behavior = "allow" if decision == "allow" else "deny"
        result: dict[str, Any] = {"behavior": behavior}
        if behavior == "deny":
            result["message"] = "用户已在飞书拒绝本次操作。"
        return {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": result,
            }
        }
    finally:
        bridge.complete(request)


__all__ = [
    "ApprovalBridge",
    "ApprovalBridgeError",
    "ApprovalRequest",
    "permission_hook_result",
    "persist_execpolicy_rule",
]
