"""Discover explicitly delivered local artifacts without scanning the workspace.

This module deliberately treats a file citation as different from a delivery
request.  It only inspects paths named by the selected assistant item, a
successful same-turn ``resource_link`` tool result, or an explicitly confirmed
tool path supplied by the caller.  It never maps ``sandbox:`` or remote URIs to
local paths by guesswork.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlsplit
from .file_validation import inspect_local_file, read_verified_file, failure_code


DEFAULT_MAX_BYTES = 30_000_000
_HASH_CHUNK_BYTES = 1024 * 1024
_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/])[^<>`\r\n]+")
_UNC_PATH = re.compile(r"(?<![A-Za-z0-9_])\\\\[^<>`\r\n]+")
_URI = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:file|sandbox|remote|https?|artifact):[^\s<>`\r\n]+"
)
_MARKDOWN_LINK = re.compile(
    r"\[([^\]\r\n]{1,512})\]\(\s*(?:<([^>\r\n]{1,4096})>|([^\s)\r\n]{1,4096}))\s*\)"
)
_ANGLE_REFERENCE = re.compile(r"<([^<>\r\n]{2,4096})>")
_CODE_REFERENCE = re.compile(r"`([^`\r\n]{2,4096})`")
_FENCED_CODE = re.compile(r"(?ms)```.*?```")
_LINE_REFERENCE = re.compile(r"(?:[:#]L?\d+(?:[-:]\d+)?)(?:$|[\s),.;!?])", re.I)
_LINE_SUFFIX = re.compile(r"(?:[:#]L?\d+(?:[-:]\d+)?)$", re.I)
_DELIVERY_MARKER = re.compile(
    r"(?i)(?:\b(?:download(?:ed)?|deliver(?:ed|y)?|export(?:ed)?|save(?:d)?|"
    r"attach(?:ed|ment)?|artifact(?:s)?|output(?:s)?|generated|result|send|share)\b|"
    r"下载|交付|导出|保存|附件|成果|产物|输出|已生成|已保存|可下载|请查收)"
)
# A remote URL needs a file/link-specific local cue.  A generic word such as
# ``saved`` in "Settings saved" must not turn a web citation into delivery.
_REMOTE_DELIVERY_MARKER = re.compile(
    r"(?i)(?:\b(?:download(?:ed)?|deliver(?:ed|y)?|export(?:ed)?|attach(?:ed|ment)?|"
    r"artifact(?:s)?|output(?:s)?|generated|result|send|share|file|files|link|path)\b|"
    r"下载|交付|导出|文件|附件|链接|路径|成果|产物|输出|已生成|可下载|请查收)"
)


def _claims_current_delivery_without_reference(text: str) -> bool:
    """Require a current delivery claim, not merely the word 'attachment'.

    This only controls missing-reference notices. Explicit links and structured
    files are processed independently, including mixed recap/new-delivery text.
    """
    text = _FENCED_CODE.sub('', text)
    for clause in re.split(r'[。！？\n；;，,]|(?<=[.!?])\s+', text):
        if not re.search(r'(?i)文件|附件|报告|文档|表格|压缩包|成果|产物|链接|\b(?:file|attachment|report|document|artifact|output|download)\b', clause):
            continue
        if re.search(r'(?i)没有|尚未|未曾|不会|不(?:生成|导出|交付|提供)|无需|无须|\b(?:no|not|never)\b', clause):
            continue
        if re.search(r'(?i)将(?:会|要)?|打算|计划|接下来|稍后|准备(?:生成|导出|创建)|\bwill\b|going to', clause):
            continue
        if (re.search(r'(?i)此前|之前|上次|过去|\bpreviously\b|\bearlier\b', clause)
            and not re.search(r'本次|本轮|现在|新文件|新报告|\b(?:now|new)\b', clause, re.I)):
            continue
        if re.search(
            r'(?i)已(?:经)?(?:生成|导出|保存|制作|写好|打包|准备好)|'
            r'(?:生成|导出|保存|制作|打包)(?:完成|好了|完毕|了)|'
            r'请(?:查收|下载)|(?:下载|附件|文件|成果|链接)(?:如下|见下|在下方)|'
            r'^(?:本次|本轮|现在|以下)?(?:交付|提供)(?:的)?(?:文件|附件|成果)|'
            r'\b(?:here (?:is|are)|attached (?:is|are)|download|'
            r'(?:i|we) (?:have )?(?:generated|exported|attached|saved|created)|'
            r'is ready|has been (?:generated|exported|created))\b', clause):
            return True
    return False
_PATH_FIELDS = (
    "path",
    "uri",
    "url",
    "file",
    "file_path",
    "filePath",
    "savedPath",
    "saved_path",
    "download",
    "download_url",
    "downloadUrl",
)
_STRUCTURED_DELIVERY_FIELDS = (
    "delivery",
    "deliveries",
    "artifact",
    "artifacts",
    "output",
    "outputs",
    "files",
    "file",
    "attachments",
)
_SUCCESS_TOOL_STATUS = "completed"
_SENSITIVE_SUFFIXES = {
    ".env",
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".secret",
    ".secrets",
    ".token",
}
_SENSITIVE_NAMES = {
    "auth.json",
    "credentials.json",
    "secrets.json",
    "id_rsa",
    "id_ed25519",
}
_SENSITIVE_PARTS = {"log", "logs"}


@dataclass(frozen=True, slots=True)
class DiscoveryError:
    """A typed, redacted discovery problem suitable for an outbox record."""

    code: str
    candidate_id: str = ""
    turn_id_hash: str = ""
    item_id_hash: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "candidate_id": self.candidate_id,
            "turn_id_hash": self.turn_id_hash,
            "item_id_hash": self.item_id_hash,
        }


@dataclass(frozen=True, slots=True)
class DeliveredFileCandidate:
    """One explicit delivery, citation, or failed delivery candidate."""

    candidate_id: str
    path: Path | None
    display_name: str
    source_kind: str
    status: str
    delivery_requested: bool
    provenance: Mapping[str, str] = field(default_factory=dict)
    reason: str = ""
    size: int | None = None
    sha256: str = ""
    uri: str = ""

    @property
    def ready(self) -> bool:
        return self.status == "ready" and self.delivery_requested and self.path is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "path": str(self.path) if self.path is not None else None,
            "display_name": self.display_name,
            "source_kind": self.source_kind,
            "status": self.status,
            "delivery_requested": self.delivery_requested,
            "provenance": dict(self.provenance),
            "reason": self.reason,
            "size": self.size,
            "sha256": self.sha256,
            "uri": self.uri,
        }


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """Candidates and typed errors; discovery does not globally raise."""

    candidates: tuple[DeliveredFileCandidate, ...] = ()
    errors: tuple[DiscoveryError, ...] = ()

    @property
    def ready(self) -> tuple[DeliveredFileCandidate, ...]:
        return tuple(candidate for candidate in self.candidates if candidate.ready)

    def as_dict(self) -> dict[str, object]:
        return {
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "errors": [error.as_dict() for error in self.errors],
        }


@dataclass(frozen=True, slots=True)
class _Item:
    payload: Mapping[str, Any]
    item_type: str
    item_id: str
    turn_id: str


@dataclass(frozen=True, slots=True)
class _Reference:
    raw: str
    label: str = ""
    field: str = "text"
    explicit: bool = False


@dataclass(frozen=True, slots=True)
class _ResolvedReference:
    raw: str
    uri: str
    path: Path | None
    display_name: str
    reason: str = ""


def _hash_token(value: object) -> str:
    text = str(value or "")
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _canonical_path(path: Path) -> str:
    value = os.path.normpath(str(path)).replace("/", "\\")
    return value.casefold() if os.name == "nt" else value


def _candidate_id(turn_id: str, event_id: str, uri: str, path: Path | None) -> str:
    identity = "\0".join(
        (
            turn_id,
            event_id,
            _canonical_path(path) if path is not None else uri,
        )
    )
    return "artifact-" + hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()[:32]


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        stat = path.stat(follow_symlinks=False)
    except (OSError, TypeError):
        return True
    return bool(int(getattr(stat, "st_file_attributes", 0)) & 0x400)


def _sensitive_path(path: Path) -> bool:
    parts = [part.casefold() for part in path.parts]
    name = path.name.casefold()
    if name in _SENSITIVE_NAMES or name.startswith(".env"):
        return True
    if path.suffix.casefold() in _SENSITIVE_SUFFIXES:
        return True
    if any(part in _SENSITIVE_PARTS for part in parts):
        return True
    return any(token in name for token in ("secret", "token", "credential", "password", "passwd"))


def _looks_like_reference(value: str) -> bool:
    text = value.strip().strip("<>\"'")
    if not text:
        return False
    lower = text.casefold()
    if lower.startswith(("file:", "sandbox:", "remote:", "artifact:", "http:", "https:")):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("\\\\"):
        return True
    return text.startswith("/") and not text.startswith("//")


def _trim_reference(value: str) -> str:
    text = value.strip().strip("<>\"'")
    while text and text[-1] in ",.;!?)]}":
        text = text[:-1]
    return text


def _resolve_reference(reference: _Reference) -> _ResolvedReference:
    raw = _trim_reference(reference.raw)
    if not raw:
        return _ResolvedReference(reference.raw, "", None, "", "empty_reference")
    lower = raw.casefold()
    path_raw = _LINE_SUFFIX.sub("", raw) if _line_reference(raw) else raw
    if lower.startswith(("sandbox:", "remote:", "artifact:", "http:", "https:")):
        parsed = urlsplit(raw)
        name = Path(unquote(parsed.path)).name or raw[:80]
        return _ResolvedReference(raw, raw, None, name, "unverified_remote_reference")
    if lower.startswith("file:"):
        parsed = urlsplit(path_raw)
        if parsed.query or parsed.fragment:
            return _ResolvedReference(raw, raw, None, "", "file_uri_query_or_fragment")
        decoded = unquote(parsed.path)
        if parsed.netloc and parsed.netloc.casefold() != "localhost":
            decoded = "\\\\" + parsed.netloc + decoded.replace("/", "\\")
        elif re.match(r"^/[A-Za-z]:[\\/]", decoded):
            decoded = decoded[1:]
        path = Path(decoded)
        return _ResolvedReference(raw, raw, path, path.name, "")
    address_field = reference.field in {"markdown_link", "angle_reference", "uri"}
    decoded = unquote(path_raw) if address_field else path_raw
    if decoded != path_raw:
        def drive(value):
            return value[1:] if os.name == "nt" and re.match(r"^/[A-Za-z]:[\\/]", value) else value
        raw_path, decoded_path = Path(drive(path_raw)), Path(drive(decoded))
        if raw_path.is_file() and decoded_path.is_file() and raw_path != decoded_path:
            return _ResolvedReference(raw, raw, None, raw_path.name, "ambiguous_path_encoding")
    # Codex Markdown may prefix a Windows absolute drive path with one slash.
    # This is a syntactic normalization only; never search for similar files.
    if os.name == "nt" and re.match(r"^/[A-Za-z]:[\\/]", decoded):
        decoded = decoded[1:]
    path = Path(decoded)
    if _looks_like_reference(decoded):
        return _ResolvedReference(raw, raw, path, path.name, "")
    return _ResolvedReference(raw, raw, None, raw[:80], "unrecognized_reference")


def _line_reference(value: str) -> bool:
    return bool(_LINE_REFERENCE.search(value))


def _safe_file(path: Path, max_bytes: int) -> tuple[str, int, str]:
    data = read_verified_file(path, max_bytes)
    return path.name, len(data), hashlib.sha256(data).hexdigest()


def _inspect_file(path: Path, max_bytes: int) -> tuple[str, int, dict[str, str]]:
    value = inspect_local_file(path, max_bytes)
    return path.name, value.st_size, {
        "size": str(value.st_size), "mtime_ns": str(value.st_mtime_ns),
        "ino": str(value.st_ino), "dev": str(value.st_dev),
    }


def _normalise_item(item: object) -> _Item | None:
    if isinstance(item, str):
        try:
            item = json.loads(item)
        except (TypeError, json.JSONDecodeError):
            return None
    if not isinstance(item, Mapping):
        return None
    wrapper = item
    payload: Mapping[str, Any] = item
    raw_json = item.get("item_json")
    if isinstance(raw_json, str):
        try:
            parsed = json.loads(raw_json)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, Mapping):
            return None
        payload = parsed
    elif isinstance(raw_json, Mapping):
        payload = raw_json
    item_type = str(wrapper.get("item_type") or payload.get("type") or "")
    item_id = str(wrapper.get("item_id") or payload.get("id") or "")
    turn_id = str(
        wrapper.get("turn_id")
        or wrapper.get("turnId")
        or payload.get("turn_id")
        or payload.get("turnId")
        or ""
    )
    return _Item(payload, item_type, item_id, turn_id)


def _iter_items(items: object) -> Iterable[_Item]:
    if isinstance(items, (Mapping, str)):
        item = _normalise_item(items)
        if item is not None:
            yield item
        return
    if items is None:
        return
    try:
        iterator = iter(items)  # type: ignore[arg-type]
    except TypeError:
        return
    for raw in iterator:
        item = _normalise_item(raw)
        if item is not None:
            yield item


def _structured_references(value: object, field: str = "delivery") -> list[_Reference]:
    references: list[_Reference] = []
    if isinstance(value, str):
        if _looks_like_reference(value):
            references.append(_Reference(value, field=field))
        return references
    if isinstance(value, Mapping):
        for key in _PATH_FIELDS:
            candidate = value.get(key)
            if isinstance(candidate, str) and _looks_like_reference(candidate):
                references.append(_Reference(candidate, field=f"{field}.{key}"))
        for key in _STRUCTURED_DELIVERY_FIELDS:
            if key in value and key != field:
                references.extend(_structured_references(value[key], f"{field}.{key}"))
        return references
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            references.extend(_structured_references(child, f"{field}[{index}]"))
    return references


# Mask entire directives before prose/path discovery, including action prompts.
_DIRECTIVE = re.compile(r':{1,2}([a-z][a-z0-9-]*)(?:\[[^\]\r\n]*\])?\{((?:"[^"\r\n]*"|[^}"\r\n])*)\}')
_DIRECTIVE_ATTR = re.compile(r'\s*([a-zA-Z][a-zA-Z0-9_-]*)="([^"\r\n]*)"')


def _directive_references(text: str) -> tuple[str, list[_Reference]]:
    references: list[_Reference] = []
    def replace(match: re.Match[str]) -> str:
        if match.group(1) == "codex-file-citation":
            body = match.group(2)
            attributes: dict[str, str] = {}
            position = 0
            valid = True
            for attribute in _DIRECTIVE_ATTR.finditer(body):
                if body[position:attribute.start()].strip() or attribute.group(1) in attributes:
                    valid = False
                attributes[attribute.group(1)] = attribute.group(2)
                position = attribute.end()
            valid = valid and not body[position:].strip()
            path = attributes.get("path", "")
            if valid and attributes.get("purpose") == "output" and _looks_like_reference(path):
                references.append(_Reference(path, field="file_directive", explicit=True))
        return " " * len(match.group(0))
    return _DIRECTIVE.sub(replace, text), references


def _text_references(text: str) -> tuple[list[_Reference], bool]:
    text = _FENCED_CODE.sub("", text)
    text, directive_references = _directive_references(text)
    # URI path/query/domain words describe the address, never delivery intent.
    # Keep visible link labels and prose so explicit remote deliverables still
    # produce a truthful unavailable-local-file result.
    def prose(value: str) -> str:
        return _URI.sub('', value)
    def remote_delivery(value: str) -> bool:
        clause=re.split(r'[；;。！？\n]',prose(value))[-1]
        if re.search(r'(?i)(?:^|[：:\s])(?:参考(?:资料|链接)?|来源|引用|references?|sources?|citation)(?:[：:\s\[]|$)',clause):
            return False
        return bool(re.search(r'(?i)下载|交付|导出|附件|已生成|请查收|\b(?:download(?:ed)?|deliver(?:ed|y)?|export(?:ed)?|attach(?:ed|ment)?|generated)\b',clause))
    explicit = bool(directive_references) or bool(_DELIVERY_MARKER.search(prose(text)))
    references: list[_Reference] = list(directive_references)
    occupied: list[tuple[int, int]] = []

    for match in _MARKDOWN_LINK.finditer(text):
        label = match.group(1)
        target = match.group(2) or match.group(3) or ""
        if _looks_like_reference(target):
            line_start = text.rfind("\n", 0, match.start()) + 1
            line = text[line_start : text.find("\n", match.end()) if "\n" in text[match.end() :] else len(text)]
            local_file_link = not target.casefold().startswith(
                ("http:", "https:", "sandbox:", "remote:", "artifact:")
            )
            link_explicit = bool(
                not _line_reference(label)
                and not _line_reference(target)
                and (
                    (remote_delivery(label) if not local_file_link else _DELIVERY_MARKER.search(label))
                    or remote_delivery(line[: match.end() - line_start])
                    or local_file_link
                )
            )
            references.append(_Reference(target, label=label, field="markdown_link", explicit=link_explicit))
            occupied.append(match.span())
            if link_explicit:
                explicit = True
    for expression, field in ((_ANGLE_REFERENCE, "angle_reference"), (_CODE_REFERENCE, "code_reference")):
        for match in expression.finditer(text):
            value = match.group(1)
            if _looks_like_reference(value):
                line_start = text.rfind("\n", 0, match.start()) + 1
                line_end = text.find("\n", match.end())
                line = text[line_start : line_end if line_end >= 0 else len(text)]
                local_explicit = bool(
                    _DELIVERY_MARKER.search(line[: match.end() - line_start])
                    if not value.casefold().startswith(("http:", "https:"))
                    else remote_delivery(line[: match.end() - line_start])
                )
                references.append(_Reference(value, field=field, explicit=local_explicit))
                occupied.append(match.span())
    for expression, field in ((_WINDOWS_PATH, "windows_path"), (_UNC_PATH, "unc_path"), (_URI, "uri")):
        for match in expression.finditer(text):
            value = match.group(0)
            if any(start <= match.start() < end for start, end in occupied):
                continue
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            line = text[line_start : line_end if line_end >= 0 else len(text)]
            local_explicit = bool(
                _DELIVERY_MARKER.search(line[: match.end() - line_start])
                if not value.casefold().startswith(("http:", "https:"))
                else remote_delivery(line[: match.end() - line_start])
            )
            references.append(_Reference(value, field=field, explicit=local_explicit))
    return references, explicit


def _provenance(item: _Item, turn_id: str, *, field: str, source: str = "") -> dict[str, str]:
    result = {
        "turn_id_hash": _hash_token(turn_id),
        "item_id_hash": _hash_token(item.item_id),
        "item_type": item.item_type,
        "field": field,
    }
    if source:
        result["source"] = source
    delivery = item.payload.get("delivery")
    if isinstance(delivery, str) and delivery:
        # The protocol currently uses a string enum (for example ``async``);
        # preserve that bounded enum in provenance instead of dropping it.
        result["delivery"] = delivery
    for key in ("server", "tool", "pluginId"):
        value = item.payload.get(key)
        if isinstance(value, str) and value:
            result[f"{key}_hash"] = _hash_token(value)
    return result


def _make_candidate(
    reference: _Reference,
    *,
    item: _Item,
    turn_id: str,
    source_kind: str,
    delivery_requested: bool,
    max_bytes: int,
    source_reason: str = "",
    source: str = "",
    confirmed: bool = False,
    inspect_content: bool = True,
) -> tuple[DeliveredFileCandidate, DiscoveryError | None]:
    resolved = _resolve_reference(reference)
    event_id = item.item_id or item.payload.get("event_id") or turn_id
    candidate_id = _candidate_id(turn_id, str(event_id), resolved.uri, resolved.path)
    provenance = _provenance(item, turn_id, field=reference.field, source=source)
    if resolved.path is None:
        reason = source_reason or resolved.reason or "unverified_reference"
        candidate = DeliveredFileCandidate(
            candidate_id,
            None,
            resolved.display_name,
            source_kind,
            "unverified" if not reason.startswith("missing") else "missing",
            delivery_requested,
            provenance,
            reason,
            None,
            "",
            resolved.uri,
        )
        return candidate, DiscoveryError(
            reason,
            candidate_id,
            _hash_token(turn_id),
            _hash_token(item.item_id),
        ) if delivery_requested else None

    path = resolved.path
    if _line_reference(resolved.raw) and not confirmed:
        candidate = DeliveredFileCandidate(
            candidate_id,
            path,
            path.name,
            "source_citation",
            "unverified",
            False,
            provenance,
            "line_reference",
            None,
            "",
            resolved.uri,
        )
        return candidate, None
    if not delivery_requested:
        reason = source_reason or "source_citation"
        return DeliveredFileCandidate(
            candidate_id,
            path,
            path.name,
            source_kind,
            "unverified",
            False,
            provenance,
            reason,
            None,
            "",
            resolved.uri,
        ), None
    try:
        if inspect_content:
            _name, size, digest = _safe_file(path, max_bytes)
        else:
            _name, size, identity = _inspect_file(path, max_bytes)
            provenance = dict(provenance)
            provenance["inspection"] = "stat"
            for key, value in identity.items():
                provenance[f"identity_{key}"] = value
            digest = ""
    except FileNotFoundError:
        reason = "missing"
        status = "missing"
        size = None
        digest = ""
    except ValueError as exc:
        reason = str(exc) or "unsafe_path"
        status = "unsafe"
        size = None
        digest = ""
    except OSError as exc:
        reason = failure_code(exc)
        status = "unsafe"
        size = None
        digest = ""
    else:
        reason = source_reason or ("content_verified" if inspect_content else "content_uninspected")
        status = "ready"
    candidate = DeliveredFileCandidate(
        candidate_id,
        path,
        path.name,
        source_kind,
        status,
        True,
        provenance,
        reason,
        size,
        digest,
        resolved.uri,
    )
    error = (
        DiscoveryError(reason, candidate_id, _hash_token(turn_id), _hash_token(item.item_id))
        if status != "ready"
        else None
    )
    return candidate, error


def _merge_candidate(
    candidates: dict[str, DeliveredFileCandidate], candidate: DeliveredFileCandidate
) -> None:
    previous = candidates.get(candidate.candidate_id)
    if previous is None:
        candidates[candidate.candidate_id] = candidate
        return
    # An explicit successful delivery wins over a citation of the same path;
    # otherwise retain the more actionable failure/verified state.
    rank = {
        "ready": 5,
        "missing": 4,
        "unsafe": 4,
        "unverified": 1,
    }
    candidate_rank = (
        candidate.delivery_requested,
        rank.get(candidate.status, 0),
        candidate.provenance.get("inspection") != "stat",
    )
    previous_rank = (
        previous.delivery_requested,
        rank.get(previous.status, 0),
        previous.provenance.get("inspection") != "stat",
    )
    if candidate_rank > previous_rank:
        candidates[candidate.candidate_id] = candidate


def _resource_links(payload: Mapping[str, Any]) -> list[_Reference]:
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return []
    content = result.get("content")
    if not isinstance(content, list):
        return []
    references: list[_Reference] = []
    for index, block in enumerate(content):
        if not isinstance(block, Mapping) or block.get("type") != "resource_link":
            continue
        uri = block.get("uri")
        if isinstance(uri, str) and uri.strip():
            references.append(_Reference(uri, field=f"result.content[{index}].uri"))
    return references


def _confirmed_entries(confirmed_paths: object) -> Iterable[tuple[object, Mapping[str, Any]]]:
    if confirmed_paths is None:
        return
    if isinstance(confirmed_paths, Mapping):
        if any(key in confirmed_paths for key in _PATH_FIELDS):
            yield confirmed_paths, confirmed_paths
            return
        for key, value in confirmed_paths.items():
            if isinstance(value, Mapping):
                yield value, value
            else:
                yield key, {"path": key, "provenance": value}
        return
    if isinstance(confirmed_paths, (str, os.PathLike)):
        yield confirmed_paths, {"path": str(confirmed_paths)}
        return
    try:
        iterator = iter(confirmed_paths)  # type: ignore[arg-type]
    except TypeError:
        return
    for entry in iterator:
        if isinstance(entry, Mapping):
            yield entry, entry
        else:
            yield entry, {"path": entry}


def discover_delivered_files(
    items: object,
    *,
    turn_id: str = "",
    confirmed_paths: object = (),
    final_agent_item_id: str | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    inspect_content: bool = True,
) -> DiscoveryResult:
    """Discover explicit artifacts from selected same-turn structured items.

    ``items`` should contain the selected final assistant item and any
    same-turn successful tool calls.  Passing a whole history is safe: only a
    final ``agentMessage`` can produce text delivery candidates, and tool calls
    carrying a mismatched turn ID are rejected.
    """

    candidates: dict[str, DeliveredFileCandidate] = {}
    errors: list[DiscoveryError] = []
    normalised = tuple(_iter_items(items))

    for item in normalised:
        current_turn = item.turn_id or turn_id
        if turn_id and item.turn_id and item.turn_id != turn_id:
            errors.append(DiscoveryError("wrong_turn", "", _hash_token(turn_id), _hash_token(item.item_id)))
            continue
        if item.item_type == "mcpToolCall" or item.payload.get("type") == "mcpToolCall":
            refs = _resource_links(item.payload)
            if not refs:
                continue
            status = str(item.payload.get("status") or "").casefold()
            tool_result = item.payload.get("result")
            result_is_error = (
                isinstance(tool_result, Mapping) and bool(tool_result.get("isError"))
            )
            success = (
                status == _SUCCESS_TOOL_STATUS
                and not item.payload.get("error")
                and not result_is_error
            )
            for reference in refs:
                candidate, error = _make_candidate(
                    reference,
                    item=item,
                    turn_id=current_turn,
                    source_kind="tool_resource",
                    delivery_requested=True,
                    max_bytes=max_bytes,
                    source="mcp_resource_link",
                    source_reason="" if success else "tool_failed",
                    inspect_content=inspect_content,
                )
                if not success:
                    candidate = DeliveredFileCandidate(
                        candidate.candidate_id,
                        candidate.path,
                        candidate.display_name,
                        candidate.source_kind,
                        "unverified",
                        True,
                        candidate.provenance,
                        "tool_failed",
                        candidate.size,
                        candidate.sha256,
                        candidate.uri,
                    )
                    error = DiscoveryError("tool_failed", candidate.candidate_id, _hash_token(current_turn), _hash_token(item.item_id))
                _merge_candidate(candidates, candidate)
                if error is not None:
                    errors.append(error)
            continue

        item_type = item.item_type or str(item.payload.get("type") or "")
        if item_type == "userMessage":
            for reference in _structured_references(item.payload.get("attachments"), "user_attachment"):
                candidate, _ = _make_candidate(
                    reference,
                    item=item,
                    turn_id=current_turn,
                    source_kind="user_attachment",
                    delivery_requested=False,
                    max_bytes=max_bytes,
                    source_reason="user_attachment_not_auto_delivered",
                    inspect_content=inspect_content,
                )
                _merge_candidate(candidates, candidate)
            continue
        if item_type != "agentMessage":
            continue
        if final_agent_item_id and item.item_id != final_agent_item_id:
            continue
        if item.payload.get("phase") not in {None, "final_answer"}:
            continue

        text = item.payload.get("text")
        text_refs: list[_Reference] = []
        explicit = False
        if isinstance(text, str):
            text_refs, explicit = _text_references(text)
        structured: list[_Reference] = []
        for field in _STRUCTURED_DELIVERY_FIELDS:
            if field in item.payload:
                structured.extend(_structured_references(item.payload.get(field), field))
        references = structured + text_refs
        if explicit and not references and _claims_current_delivery_without_reference(text or ''):
            candidate_id = _candidate_id(current_turn, item.item_id or current_turn, "", None)
            candidate = DeliveredFileCandidate(
                candidate_id,
                None,
                "",
                "explicit_delivery",
                "missing",
                True,
                _provenance(item, current_turn, field="text"),
                "delivery_without_path",
            )
            _merge_candidate(candidates, candidate)
            errors.append(DiscoveryError("delivery_without_path", candidate_id, _hash_token(current_turn), _hash_token(item.item_id)))
        for reference in references:
            delivery = reference.explicit or reference.field.split(".", 1)[0] in _STRUCTURED_DELIVERY_FIELDS
            source_kind = "explicit_delivery" if delivery else "source_citation"
            candidate, error = _make_candidate(
                reference,
                item=item,
                turn_id=current_turn,
                source_kind=source_kind,
                delivery_requested=delivery,
                max_bytes=max_bytes,
                inspect_content=inspect_content,
            )
            # An ordinary remote citation is useful in the assistant text but
            # is not a local artifact candidate.  Keep typed records for
            # explicitly requested remote delivery only.
            if not delivery and candidate.path is None:
                continue
            _merge_candidate(candidates, candidate)
            if error is not None:
                errors.append(error)

    for raw, metadata in _confirmed_entries(confirmed_paths):
        if isinstance(raw, Mapping):
            value = next((raw.get(key) for key in _PATH_FIELDS if isinstance(raw.get(key), str)), None)
        else:
            value = raw
        if not isinstance(value, (str, os.PathLike)):
            continue
        entry_turn = str(metadata.get("turn_id") or metadata.get("turnId") or turn_id)
        if turn_id and entry_turn and entry_turn != turn_id:
            errors.append(DiscoveryError("wrong_turn", "", _hash_token(turn_id), ""))
            continue
        item = _Item(
            {"server": metadata.get("server"), "tool": metadata.get("tool")},
            "confirmedArtifact",
            str(metadata.get("item_id") or metadata.get("itemId") or "confirmed"),
            entry_turn,
        )
        candidate, error = _make_candidate(
            _Reference(str(value), field="confirmed_path"),
            item=item,
            turn_id=entry_turn,
            source_kind="tool_resource",
            delivery_requested=True,
            max_bytes=max_bytes,
            source="confirmed_tool_artifact",
            confirmed=True,
            inspect_content=inspect_content,
        )
        _merge_candidate(candidates, candidate)
        if error is not None:
            errors.append(error)

    unique_errors: list[DiscoveryError] = []
    seen_errors: set[tuple[str, str, str, str]] = set()
    for error in errors:
        key = (error.code, error.candidate_id, error.turn_id_hash, error.item_id_hash)
        if key in seen_errors:
            continue
        seen_errors.add(key)
        unique_errors.append(error)
    return DiscoveryResult(tuple(candidates.values()), tuple(unique_errors))


def make_final_agent_item(
    *,
    item_id: str,
    text: str,
    phase: str = "final_answer",
    delivery: str | None = None,
) -> dict[str, object]:
    """Build the minimal rollout-fallback shape accepted by the parser."""

    item: dict[str, object] = {
        "type": "agentMessage",
        "id": str(item_id),
        "text": str(text),
        "phase": str(phase),
    }
    if delivery is not None:
        item["delivery"] = delivery
    return item


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DeliveredFileCandidate",
    "DiscoveryError",
    "DiscoveryResult",
    "discover_delivered_files",
    "make_final_agent_item",
]
