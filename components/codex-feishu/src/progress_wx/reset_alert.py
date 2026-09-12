"""Codex 重置预警：公开来源核验、确定性判级、整点调度与持久投递。"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Mapping, Sequence

from .channel import MessageChannelOfflineError
from .config import ResetAlertConfig
from .feishu import (
    FeishuSendError,
    FeishuSendNotSubmittedError,
    FeishuSendRejectedError,
)
from .state import ResetAlertDelivery, StateStore


LOGGER = logging.getLogger(__name__)

BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")
OPENAI_INCIDENTS_URL = "https://status.openai.com/api/v2/incidents.json"
OPENAI_CODEX_PRICING_URL = "https://learn.chatgpt.com/docs/pricing"
FORECAST_URL = "https://www.willcodexquotareset.com/api/forecast"
X_OEMBED_URL = "https://publish.x.com/oembed"
X_AUTHOR_URL = "https://x.com/thsottiaux"
X_SYNDICATION_URL = (
    "https://syndication.twitter.com/srv/timeline-profile/screen-name/thsottiaux"
)
RULE_VERSION = "reset-alert-rules-v2"
SIGNAL_LOOKBACK_SECONDS = 24 * 3600
DOCS_HASH_PREFIX = "html-v2:"
SOURCE_PAYLOAD_LIMIT = 2 * 1024 * 1024
X_HTML_PAYLOAD_LIMIT = 6 * 1024 * 1024
OEMBED_HTML_LIMIT = 20_000
SOURCE_FRESHNESS_SECONDS = 2 * 3600
WORKER_IDLE_SECONDS = 15.0
EXPECTED_SOURCE_IDS = (
    "forecast",
    "openai_status",
    "openai_codex_docs",
    "x_thsottiaux",
)


class ResetAlertSourceError(RuntimeError):
    """公开来源不可用、过期或结构不可信。"""

    def __init__(self, code: str, *, rate_metadata: Mapping[str, int] | None = None):
        super().__init__(code)
        self.rate_metadata = dict(rate_metadata or {})


def _rate_metadata(headers: Any) -> dict[str, int]:
    """Only parsed waiting hints and numeric rate values; never raw headers."""
    result: dict[str, int] = {}
    def single(name: str) -> str:
        values = {str(value).strip() for key, value in headers.items()
                  if str(key).casefold() == name.casefold()}
        return values.pop() if len(values) == 1 else ''
    for header, key in [('x-rate-limit-limit', 'rate_limit'),
                        ('x-rate-limit-remaining', 'rate_remaining'),
                        ('x-rate-limit-reset', 'rate_reset_at')]:
        raw = single(header)
        if re.fullmatch(r'[0-9]{1,128}', raw):
            result[key] = int(raw)
        elif re.fullmatch(r'[0-9]+', raw) and len(raw) > 128 and key == 'rate_reset_at':
            result['wait_unrepresentable'] = 1
    raw = single('Retry-After')
    if re.fullmatch(r'[0-9]{1,128}', raw):
        result['retry_after_seconds'] = int(raw)
    elif re.fullmatch(r'[0-9]+', raw) and len(raw) > 128:
        result['wait_unrepresentable'] = 1
    elif raw and len(raw) <= 128 and re.fullmatch(
        r'(?:[A-Za-z]{3}, [0-9]{2} [A-Za-z]{3} [0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT|'
        r'[A-Za-z]+, [0-9]{2}-[A-Za-z]{3}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT|'
        r'[A-Za-z]{3} [A-Za-z]{3} [ 0-9][0-9] [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4})', raw):
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            result['retry_after_at'] = int(parsed.timestamp())
        except (TypeError, ValueError, OverflowError):
            pass
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        del req, fp, code, msg, headers, newurl
        return None


class PublicJsonClient:
    """只允许固定 HTTPS 主机、禁重定向、限制体积的 JSON 客户端。"""

    def __init__(self, timeout_seconds: float):
        self.timeout_seconds = float(timeout_seconds)
        self._opener = urllib.request.build_opener(_NoRedirect())
        self.rate_limit_metadata: dict[str, int] = {}

    def get_json(self, url: str, *, allowed_host: str) -> Mapping[str, Any]:
        raw = self._get_bytes(
            url,
            allowed_host=allowed_host,
            accepted_content_types=("application/json", "text/json"),
            size_limit=SOURCE_PAYLOAD_LIMIT,
        )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ResetAlertSourceError("source_json_invalid") from exc
        if not isinstance(payload, Mapping):
            raise ResetAlertSourceError("source_schema_invalid")
        return payload

    def get_html(self, url: str, *, allowed_host: str) -> str:
        raw = self._get_bytes(
            url,
            allowed_host=allowed_host,
            accepted_content_types=("text/html", "application/xhtml+xml"),
            size_limit=X_HTML_PAYLOAD_LIMIT,
        )
        try:
            return raw.decode("utf-8")
        except UnicodeError as exc:
            raise ResetAlertSourceError("source_html_invalid") from exc

    def get_document_html(
        self, url: str, *, allowed_host: str
    ) -> tuple[str, Mapping[str, str]]:
        raw, headers = self._get_bytes_with_metadata(
            url,
            allowed_host=allowed_host,
            accepted_content_types=("text/html",),
            size_limit=SOURCE_PAYLOAD_LIMIT,
        )
        content_type = str(headers.get("content_type") or "")
        charset = ""
        for part in content_type.split(";")[1:]:
            key, separator, value = part.strip().partition("=")
            if separator and key.casefold() == "charset":
                charset = value.strip().strip('"').casefold()
        if charset not in {"utf-8", "utf8"}:
            raise ResetAlertSourceError("source_html_charset_invalid")
        try:
            return raw.decode("utf-8"), headers
        except UnicodeError as exc:
            raise ResetAlertSourceError("source_html_invalid") from exc

    def _get_bytes(
        self,
        url: str,
        *,
        allowed_host: str,
        accepted_content_types: Sequence[str],
        size_limit: int,
    ) -> bytes:
        raw, _headers = self._get_bytes_with_metadata(
            url,
            allowed_host=allowed_host,
            accepted_content_types=accepted_content_types,
            size_limit=size_limit,
        )
        return raw

    def _get_bytes_with_metadata(
        self,
        url: str,
        *,
        allowed_host: str,
        accepted_content_types: Sequence[str],
        size_limit: int,
    ) -> tuple[bytes, Mapping[str, str]]:
        self.rate_limit_metadata = {}
        try:
            parts = urllib.parse.urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise ResetAlertSourceError("source_url_rejected") from exc
        if (
            parts.scheme != "https"
            or (parts.hostname or "").casefold() != allowed_host.casefold()
            or port not in {None, 443}
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
        ):
            raise ResetAlertSourceError("source_url_rejected")
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": accepted_content_types[0],
                "Accept-Encoding": "identity",
                "User-Agent": "FeiShuBOT-CodexResetAlert/1.0",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                if str(response.geturl()) != url:
                    raise ResetAlertSourceError("source_redirect_rejected")
                if int(getattr(response, "status", 200)) != 200:
                    raise ResetAlertSourceError("source_http_status_invalid")
                content_type = str(response.headers.get("Content-Type") or "")
                media_type = content_type.split(";", 1)[0].strip().casefold()
                if media_type not in {
                    item.casefold() for item in accepted_content_types
                }:
                    raise ResetAlertSourceError("source_content_type_invalid")
                content_encoding = str(
                    response.headers.get("Content-Encoding") or ""
                ).strip().casefold()
                if content_encoding not in {"", "identity"}:
                    raise ResetAlertSourceError("source_content_encoding_invalid")
                raw_length = str(response.headers.get("Content-Length") or "").strip()
                if raw_length:
                    try:
                        content_length = int(raw_length)
                    except ValueError as exc:
                        raise ResetAlertSourceError(
                            "source_content_length_invalid"
                        ) from exc
                    if content_length < 0 or content_length > int(size_limit):
                        raise ResetAlertSourceError("source_payload_too_large")
                raw = response.read(int(size_limit) + 1)
                self.rate_limit_metadata = _rate_metadata(response.headers)
                response_metadata = {
                    "etag": str(response.headers.get("ETag") or "")[:300],
                    "last_modified": str(
                        response.headers.get("Last-Modified") or ""
                    )[:300],
                    "content_type": content_type[:300],
                }
        except urllib.error.HTTPError as exc:
            raise ResetAlertSourceError(f"source_http_{int(exc.code)}",
                rate_metadata=_rate_metadata(exc.headers or {})) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ResetAlertSourceError("source_transport_unavailable") from exc
        if len(raw) > int(size_limit):
            raise ResetAlertSourceError("source_payload_too_large")
        return raw, response_metadata


def _parse_time(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.timestamp())


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_CODEX_QUOTA_SECTION_TITLES = (
    "What are the usage limits for my plan?",
    "ChatGPT Voice in Desktop",
    "What happens when you hit usage limits?",
    "How does image generation count toward usage limits?",
    "Where can I see my current usage limits?",
    "What are tokens and credits?",
    "What counts as Code Review usage?",
    "What can I do to make my usage limits last longer?",
)
_CODEX_USAGE_TABLE_HEADER = (
    "Model",
    "Plus",
    "Pro 5x",
    "Pro 20x",
    "Business",
    "API Key",
)
_CODEX_CREDIT_TABLE_HEADER = (
    "Credits per 1M tokens",
    "Input Tokens",
    "Cached input tokens",
    "Output Tokens",
)
_HTML_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_HTML_IGNORED_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg", "form", "button"}
)


def _normalize_visible_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", html.unescape(str(value)))
    text = text.replace("\ufeff", "").replace("\u00ad", "")
    for marker in ("\u200b", "\u200c", "\u200d", "\u2060"):
        text = text.replace(marker, "")
    return " ".join(text.split())


class _CodexPricingHtmlParser(HTMLParser):
    """只保留 Pricing 主文章中八个额度小节的可见结构化正文。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.article_count = 0
        self._in_article = False
        self._article_depth = 0
        self._ignore_depth = 0
        self._heading_tag: str | None = None
        self._heading_parts: list[str] = []
        self._active_section: str | None = None
        self._preamble = True
        self.preamble_parts: list[str] = []
        self.sections = {title: [] for title in _CODEX_QUOTA_SECTION_TITLES}
        self.heading_order: list[str] = []
        self.heading_counts = {title: 0 for title in _CODEX_QUOTA_SECTION_TITLES}
        self.anchor_counts = {"usage-limits": 0, "credits-overview": 0}
        self.tables: dict[str, list[list[tuple[str, ...]]]] = {
            title: [] for title in _CODEX_QUOTA_SECTION_TITLES
        }
        self._table_rows: list[tuple[str, ...]] | None = None
        self._row_cells: list[str] | None = None
        self._cell_parts: list[str] | None = None
        self.invalid = False

    def _bucket(self) -> list[str] | None:
        if self._active_section is not None:
            return self.sections[self._active_section]
        if self._preamble:
            return self.preamble_parts
        return None

    @staticmethod
    def _hidden(attributes: Mapping[str, str]) -> bool:
        classes = set(attributes.get("class", "").casefold().split())
        return (
            "hidden" in attributes
            or attributes.get("aria-hidden", "").casefold() == "true"
            or "hidden" in classes
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.casefold()
        attributes = {
            str(key).casefold(): str(value or "") for key, value in attrs
        }
        is_void = name in _HTML_VOID_TAGS
        if not self._in_article:
            if name == "article" and attributes.get("id") == "mainContent":
                self.article_count += 1
                self._in_article = True
                self._article_depth = 1
            return
        # HTML 的普通元素允许隐式闭合；用所有 start/end 标签推算深度会被
        # 浏览器容错语义打乱。这里只需要知道何时离开目标 article，因此
        # 仅跟踪 article 自身的嵌套层级。
        if name == "article" and not is_void:
            if attributes.get("id") == "mainContent":
                self.article_count += 1
            self._article_depth += 1
        if self._ignore_depth:
            if not is_void:
                self._ignore_depth += 1
            return
        if (
            name in _HTML_IGNORED_TAGS
            or attributes.get("id") == "content-switcher-codex-pricing-plans"
            or self._hidden(attributes)
        ):
            if not is_void:
                self._ignore_depth = 1
            return
        identifier = attributes.get("id", "")
        if identifier in self.anchor_counts:
            self.anchor_counts[identifier] += 1
        if name in {"h1", "h2", "h3"}:
            if self._heading_tag is not None:
                self.invalid = True
            self._heading_tag = name
            self._heading_parts = []
            return
        bucket = self._bucket()
        if bucket is not None and name in {"p", "li", "tr", "th", "td"}:
            bucket.append(f"<{name}>")
        if self._active_section is not None and name == "table":
            if self._table_rows is not None:
                self.invalid = True
            self._table_rows = []
        elif self._table_rows is not None and name == "tr":
            self._row_cells = []
        elif self._row_cells is not None and name in {"th", "td"}:
            self._cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()
        if not self._in_article:
            return
        if self._ignore_depth:
            self._ignore_depth -= 1
            return
        if self._heading_tag == name:
            label = _normalize_visible_text(" ".join(self._heading_parts))
            if name == "h2":
                self._preamble = False
                self._active_section = None
            elif name == "h3":
                self._active_section = (
                    label if label in self.heading_counts else None
                )
                if self._active_section is not None:
                    self.heading_counts[self._active_section] += 1
                    self.heading_order.append(self._active_section)
            self._heading_tag = None
            self._heading_parts = []
        if self._cell_parts is not None and name in {"th", "td"}:
            if self._row_cells is None:
                self.invalid = True
            else:
                self._row_cells.append(
                    _normalize_visible_text(" ".join(self._cell_parts))
                )
            self._cell_parts = None
        elif self._row_cells is not None and name == "tr":
            if self._table_rows is not None and any(self._row_cells):
                self._table_rows.append(tuple(self._row_cells))
            self._row_cells = None
        elif self._table_rows is not None and name == "table":
            if self._active_section is None:
                self.invalid = True
            else:
                self.tables[self._active_section].append(self._table_rows)
            self._table_rows = None
        bucket = self._bucket()
        if bucket is not None and name in {"p", "li", "tr", "th", "td"}:
            bucket.append(f"</{name}>")
        if name == "article":
            self._article_depth -= 1
            if self._article_depth == 0:
                self._in_article = False

    def handle_data(self, data: str) -> None:
        if not self._in_article or self._ignore_depth:
            return
        if self._heading_tag is not None:
            self._heading_parts.append(data)
            return
        bucket = self._bucket()
        if bucket is not None:
            bucket.append(data)
        if self._cell_parts is not None:
            self._cell_parts.append(data)


def _extract_codex_quota_document(markup: str) -> str:
    """严格抽取官方最终 HTML 中稳定、可核验的 Codex 额度正文。"""

    if (
        not isinstance(markup, str)
        or "\x00" in markup
        or len(markup.encode("utf-8")) > SOURCE_PAYLOAD_LIMIT
    ):
        raise ResetAlertSourceError("openai_codex_docs_schema_invalid")
    parser = _CodexPricingHtmlParser()
    try:
        parser.feed(markup)
        parser.close()
    except (AssertionError, ValueError) as exc:
        raise ResetAlertSourceError("openai_codex_docs_schema_invalid") from exc
    if (
        parser.invalid
        or parser.article_count != 1
        or parser._in_article
        or any(count > 1 for count in parser.heading_counts.values())
        or any(count > 1 for count in parser.anchor_counts.values())
        or parser.heading_counts["What are the usage limits for my plan?"] != 1
        or parser.heading_counts["What are tokens and credits?"] != 1
    ):
        raise ResetAlertSourceError("openai_codex_docs_schema_invalid")
    usage_tables = parser.tables["What are the usage limits for my plan?"]
    credit_tables = parser.tables["What are tokens and credits?"]
    if (
        len(usage_tables) != 1
        or len(usage_tables[0]) < 2
        or len(usage_tables[0][0]) < 3
        or usage_tables[0][0][0].casefold() != "model"
        or not any("plus" in cell.casefold() for cell in usage_tables[0][0][1:])
        or len(credit_tables) != 1
        or len(credit_tables[0]) < 2
        or len(credit_tables[0][0]) < 3
        or "credit" not in credit_tables[0][0][0].casefold()
        or not any("input" in cell.casefold() for cell in credit_tables[0][0][1:])
        or not any("output" in cell.casefold() for cell in credit_tables[0][0][1:])
    ):
        raise ResetAlertSourceError("openai_codex_docs_schema_invalid")
    preamble = _normalize_visible_text(" ".join(parser.preamble_parts))
    shared_usage = re.search(
        r"ChatGPT Work and Codex share usage\..{0,500}?usage limits as Codex\.",
        preamble,
        re.IGNORECASE,
    )
    if shared_usage is None:
        raise ResetAlertSourceError("openai_codex_docs_schema_invalid")
    normalized = [
        "openai-codex-docs-html-v2",
        f"Shared usage: {shared_usage.group(0)}",
    ]
    for title in _CODEX_QUOTA_SECTION_TITLES:
        if not parser.heading_counts[title]:
            continue
        body = _normalize_visible_text(" ".join(parser.sections[title]))
        if len(body) < 30:
            raise ResetAlertSourceError("openai_codex_docs_schema_invalid")
        normalized.append(f"{title}: {body}")
    result = "\n".join(normalized)
    if (
        not 1_000 <= len(result) <= 50_000
        or not re.search(r"\bCodex\b", result, re.IGNORECASE)
        or not re.search(
            r"\b(?:usage|quota|rate|credit)\w*\b", result, re.IGNORECASE
        )
    ):
        raise ResetAlertSourceError("openai_codex_docs_schema_invalid")
    return result


def _safe_identifier(value: object, *, limit: int = 160) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit or not re.fullmatch(r"[A-Za-z0-9_.:@-]+", text):
        raise ResetAlertSourceError("source_identifier_invalid")
    return text


class _OEmbedTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._blockquote = 0
        self._paragraph = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        name = tag.casefold()
        if name == "blockquote":
            self._blockquote += 1
        elif name == "p" and self._blockquote:
            self._paragraph += 1

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()
        if name == "p" and self._paragraph:
            self._paragraph -= 1
        elif name == "blockquote" and self._blockquote:
            self._blockquote -= 1

    def handle_data(self, data: str) -> None:
        if self._blockquote and self._paragraph:
            self.parts.append(data)


class _JsonScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._capture = False
        self._parts: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "script":
            return
        attributes = {str(key).casefold(): str(value or "").casefold() for key, value in attrs}
        script_type = attributes.get("type", "")
        self._capture = script_type in {"application/json", "application/ld+json"}
        self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "script":
            return
        if self._capture:
            self.scripts.append("".join(self._parts))
        self._capture = False
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._parts.append(data)


_NESTED_TWEET_KEYS = frozenset(
    {
        "quoted_status",
        "quoted_status_result",
        "retweeted_status",
        "retweeted_status_result",
        "promoted_content",
    }
)


def _direct_reply_refs(value: object, child_id: str) -> set[str]:
    """只读取目标 Tweet 自身的 reply 关系，忽略引用/转推内层关系。"""

    refs: set[str] = set()
    stack: list[object] = [value]
    visited = 0
    while stack:
        current = stack.pop()
        visited += 1
        if visited > 100_000:
            raise ResetAlertSourceError("x_hydration_too_complex")
        if isinstance(current, Mapping):
            current_id = str(
                current.get("rest_id") or current.get("id_str") or ""
            ).strip()
            if current_id and current_id != child_id:
                continue
            for key, child in current.items():
                if key == "reply_to_results" and isinstance(child, Mapping):
                    raw_ref = child.get("__ref")
                    match = re.fullmatch(r"TweetResults:(\d{15,22})", str(raw_ref or ""))
                    if match is not None:
                        refs.add(match.group(1))
                elif key in {"in_reply_to_status_id", "in_reply_to_status_id_str"}:
                    match = re.fullmatch(r"\d{15,22}", str(child or ""))
                    if match is not None:
                        refs.add(match.group(0))
                elif key not in _NESTED_TWEET_KEYS and isinstance(
                    child, (Mapping, list)
                ):
                    stack.append(child)
        elif isinstance(current, list):
            stack.extend(current)
    return refs


def _balanced_js_object(markup: str, start: int) -> str | None:
    if start < 0 or start >= len(markup) or markup[start] != "{":
        return None
    depth = 0
    quote = ""
    escaped = False
    limit = min(len(markup), start + 128_000)
    for index in range(start, limit):
        char = markup[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'", "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return markup[start : index + 1]
            if depth < 0:
                return None
    return None


def _react_hydration_parent_refs(markup: str, child_id: str) -> set[str]:
    refs: set[str] = set()
    rest_pattern = re.compile(rf'rest_id\s*:\s*"{re.escape(child_id)}"')
    assignment_pattern = re.compile(r"\$R\[\d+\]\s*=\s*\{")
    reply_pattern = re.compile(
        r'reply_to_results\s*:\s*\$R\[\d+\]\s*=\s*\{\s*'
        r'__ref\s*:\s*"TweetResults:(\d{15,22})"\s*\}'
    )
    for match in rest_pattern.finditer(markup):
        search_start = max(0, match.start() - 8_192)
        assignments = list(assignment_pattern.finditer(markup, search_start, match.start()))
        if not assignments:
            continue
        object_start = assignments[-1].end() - 1
        scope = _balanced_js_object(markup, object_start)
        if scope is None or match.start() >= object_start + len(scope):
            continue
        if not re.search(r'__typename\s*:\s*"Tweet"', scope):
            continue
        child_offset = match.start() - object_start
        direct_region = scope[child_offset:]
        next_tweet = re.search(
            rf'rest_id\s*:\s*"(?!{re.escape(child_id)}")(\d{{15,22}})"',
            direct_region[len(match.group(0)) :],
        )
        if next_tweet is not None:
            direct_region = direct_region[
                : len(match.group(0)) + next_tweet.start()
            ]
        for reply in reply_pattern.finditer(direct_region):
            refs.add(reply.group(1))
    return refs


def _official_direct_parent_id(markup: str, child_id: str) -> str:
    if not isinstance(markup, str) or not markup or len(markup.encode("utf-8")) > X_HTML_PAYLOAD_LIMIT:
        raise ResetAlertSourceError("x_hydration_invalid")
    parser = _JsonScriptParser()
    try:
        parser.feed(markup)
        parser.close()
    except ValueError as exc:
        raise ResetAlertSourceError("x_hydration_invalid") from exc
    wanted_key = f"TweetResults:{child_id}"
    parents: set[str] = _react_hydration_parent_refs(markup, child_id)
    parsed_any = False
    for raw in parser.scripts:
        try:
            payload = json.loads(html.unescape(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        parsed_any = True
        stack: list[object] = [payload]
        visited = 0
        while stack:
            current = stack.pop()
            visited += 1
            if visited > 100_000:
                raise ResetAlertSourceError("x_hydration_too_complex")
            if isinstance(current, Mapping):
                keyed = current.get(wanted_key)
                if isinstance(keyed, Mapping):
                    parents.update(_direct_reply_refs(keyed, child_id))
                if str(current.get("rest_id") or "") == child_id:
                    parents.update(_direct_reply_refs(current, child_id))
                stack.extend(
                    child for child in current.values() if isinstance(child, (Mapping, list))
                )
            elif isinstance(current, list):
                stack.extend(current)
    if len(parents) != 1:
        raise ResetAlertSourceError("x_reply_relationship_unverified")
    return next(iter(parents))


def _twitter_time(value: object) -> int | None:
    if not isinstance(value, str) or len(value) > 80:
        return None
    parsed = _parse_time(value)
    if parsed is not None:
        return parsed
    try:
        return int(datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y").timestamp())
    except ValueError:
        return None


def _mapping_screen_name(value: Mapping[str, Any]) -> str:
    direct = value.get("screen_name")
    if isinstance(direct, str):
        return direct
    user = value.get("user")
    if isinstance(user, Mapping) and isinstance(user.get("screen_name"), str):
        return str(user["screen_name"])
    legacy = value.get("legacy")
    if isinstance(legacy, Mapping) and isinstance(legacy.get("screen_name"), str):
        return str(legacy["screen_name"])
    return ""


def _syndication_candidates(
    markup: str,
    *,
    window_start: int,
    now: int,
    max_count: int,
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(markup, str) or not markup or len(markup.encode("utf-8")) > X_HTML_PAYLOAD_LIMIT:
        raise ResetAlertSourceError("x_syndication_invalid")
    parser = _JsonScriptParser()
    try:
        parser.feed(markup)
        parser.close()
    except ValueError as exc:
        raise ResetAlertSourceError("x_syndication_invalid") from exc
    payloads: list[object] = []
    for raw in parser.scripts:
        try:
            payloads.append(json.loads(html.unescape(raw)))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    # 部分部署把 timeline JSON 直接写在文档正文中；只接受完整 JSON 对象，
    # 不对任意文本做宽松键值猜测。
    stripped = markup.strip()
    if stripped.startswith(("{", "[")):
        try:
            payloads.append(json.loads(stripped))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    recognized = False
    candidates: dict[str, Mapping[str, Any]] = {}

    def add_candidate(
        *,
        status_id: object,
        created_at: object,
        conversation_id: object,
        parent_id: object = "",
        author: str = "thsottiaux",
        excluded: bool = False,
    ) -> None:
        status = str(status_id or "").strip()
        conversation = str(conversation_id or "").strip()
        published = _twitter_time(created_at)
        if (
            re.fullmatch(r"\d{15,22}", status)
            and published is not None
            and window_start <= published <= now + 300
            and author.casefold() == "thsottiaux"
            and re.fullmatch(r"\d{15,22}", conversation)
            and not excluded
        ):
            parent = str(parent_id or "").strip()
            if parent and not re.fullmatch(r"\d{15,22}", parent):
                parent = ""
            candidates[status] = {
                "guid": status,
                "published_at": published,
                "reply_to_guid": parent,
            }

    stack = list(payloads)
    visited = 0
    while stack:
        current = stack.pop()
        visited += 1
        if visited > 200_000:
            raise ResetAlertSourceError("x_syndication_too_complex")
        if isinstance(current, Mapping):
            if any(key in current for key in ("timeline", "tweets", "entries")):
                recognized = True
            legacy = current.get("legacy")
            tweet = legacy if isinstance(legacy, Mapping) and (
                "id_str" in legacy or "created_at" in legacy
            ) else current
            if isinstance(tweet, Mapping):
                raw_id = tweet.get("id_str") or tweet.get("rest_id") or current.get("rest_id")
                raw_created = tweet.get("created_at")
                conversation_id = tweet.get("conversation_id_str")
                author = _mapping_screen_name(tweet) or _mapping_screen_name(current)
                if raw_id is not None or raw_created is not None:
                    recognized = True
                status_id = str(raw_id or "").strip()
                published = _twitter_time(raw_created)
                # 本人带新增正文的 quote post 仍是独立的本人状态，后续只用
                # 该状态自己的 oEmbed 正文判级；不能因为它引用了别的帖子就
                # 整体丢弃。纯转推和推广内容则不是本人的新增正文。
                excluded = bool(
                    any(
                        container.get(key)
                        for container in (tweet, current)
                        for key in (
                            "retweeted_status",
                            "retweeted_status_result",
                            "retweeted_status_id",
                            "retweeted_status_id_str",
                            "promoted_content",
                        )
                    )
                    or str(tweet.get("full_text") or tweet.get("text") or "")
                    .lstrip()
                    .startswith("RT @")
                )
                add_candidate(
                    status_id=status_id,
                    created_at=raw_created,
                    conversation_id=conversation_id,
                    parent_id=(
                        tweet.get("in_reply_to_status_id_str")
                        or tweet.get("in_reply_to_status_id")
                    ),
                    author=author,
                    excluded=excluded,
                )
            # 被引用、被转推或推广对象可能同样含 Tweet 字段，不能把这些
            # 嵌套对象提升为当前账号自己的 timeline 候选。
            nested_tweet_keys = set(_NESTED_TWEET_KEYS)
            if tweet is legacy:
                # 该 legacy 已作为当前 wrapper 的正文解析；再次下钻会丢失
                # wrapper 上的 retweet/promoted 标记并把同一条误升格为候选。
                nested_tweet_keys.add("legacy")
            stack.extend(
                child
                for key, child in current.items()
                if key not in nested_tweet_keys
                and isinstance(child, (Mapping, list))
            )
        elif isinstance(current, list):
            stack.extend(current)
    # X 目前也会把相同结构放进 React 流式 hydration，而不是 JSON script。
    # 这里只在单个平衡 Tweet 对象内取值；作者最终仍由每条 oEmbed 再核验。
    assignment_pattern = re.compile(r"\$R\[\d+\]\s*=\s*\{")
    tweet_scopes: list[str] = []
    timeline_ids: set[str] = set()
    react_timeline_seen = False
    for assignment in assignment_pattern.finditer(markup):
        scope = _balanced_js_object(markup, assignment.end() - 1)
        if scope is None:
            continue
        if re.search(r'(?:TimelineTweet|TimelineTimelineItem)', scope):
            react_timeline_seen = True
            direct_refs = re.findall(
                r'tweet_results\s*:\s*(?:\$R\[\d+\]\s*=\s*)?\{'
                r'.{0,2048}?__ref\s*:\s*"TweetResults:(\d{15,22})"',
                scope,
                re.DOTALL,
            )
            if len(set(direct_refs)) == 1:
                timeline_ids.add(direct_refs[0])
        if not re.search(r'__typename\s*:\s*"Tweet"', scope):
            continue
        tweet_scopes.append(scope)

    if react_timeline_seen and not timeline_ids and not candidates:
        raise ResetAlertSourceError("x_syndication_schema_invalid")

    # 真实 syndication HTML 的 React hydration 会同时携带 timeline 项和
    # 它们引用/转推的 Tweet 对象。只允许 timeline 项直接引用的 Tweet，
    # 避免把嵌套 quoted Tweet 提升为新候选。
    for scope in tweet_scopes:
        recognized = True
        id_match = re.search(r'(?:rest_id|id_str)\s*:\s*"(\d{15,22})"', scope)
        created_match = re.search(r'created_at\s*:\s*"([^"\\]{1,80})"', scope)
        conversation_match = re.search(
            r'conversation_id_str\s*:\s*"(\d{15,22})"', scope
        )
        parent_match = re.search(
            r'in_reply_to_status_id(?:_str)?\s*:\s*"(\d{15,22})"', scope
        )
        author_match = re.search(r'screen_name\s*:\s*"([^"\\]{1,40})"', scope)
        status_id = id_match.group(1) if id_match else ""
        excluded = bool(
            re.search(
                r'(?:retweeted_status|retweeted_status_result|promoted_content)\s*:',
                scope,
            )
            or re.search(r'(?:full_text|text)\s*:\s*"\s*RT\s+@', scope)
            or not react_timeline_seen
            or (react_timeline_seen and status_id not in timeline_ids)
        )
        if id_match and created_match and conversation_match:
            add_candidate(
                status_id=status_id,
                created_at=created_match.group(1),
                conversation_id=conversation_match.group(1),
                parent_id=parent_match.group(1) if parent_match else "",
                author=(author_match.group(1) if author_match else "thsottiaux"),
                excluded=excluded,
            )
    if not recognized:
        raise ResetAlertSourceError("x_syndication_schema_invalid")
    ordered = sorted(
        candidates.values(), key=lambda item: (-int(item["published_at"]), str(item["guid"]))
    )
    return tuple(ordered[: max(1, int(max_count))])


def _forecast_candidates(
    posts: Sequence[object],
    *,
    window_start: int | None,
    now: int,
    max_count: int,
) -> tuple[Mapping[str, Any], ...]:
    """Turn forecast posts into discovery candidates, never into evidence.

    The forecast service is deliberately treated as an index only.  The
    returned records contain an id/time/optional reply relationship so the
    scanner can ask X oEmbed for the authoritative body and author before a
    signal is created.  In particular, title/context/classifier fields are
    intentionally ignored here.
    """

    def status_id(value: object) -> str:
        text = str(value or "").strip()
        if re.fullmatch(r"\d{15,22}", text):
            return text
        try:
            return _status_id_from_url(text)
        except ResetAlertSourceError:
            return ""

    candidates: dict[str, Mapping[str, Any]] = {}
    for raw in posts:
        if not isinstance(raw, Mapping):
            continue
        guid = status_id(
            raw.get("guid")
            or raw.get("status_id")
            or raw.get("tweet_id")
            or raw.get("id")
            or raw.get("link")
            or raw.get("url")
        )
        published = _twitter_time(
            raw.get("pubDate")
            or raw.get("publishedAt")
            or raw.get("created_at")
            or raw.get("createdAt")
            or raw.get("announcedAt")
        )
        if (
            not guid
            or published is None
            or (window_start is not None and published < int(window_start))
            or published > now + 300
        ):
            continue
        parent = status_id(
            raw.get("replyToGuid")
            or raw.get("reply_to_guid")
            or raw.get("in_reply_to_status_id_str")
            or raw.get("in_reply_to_status_id")
        )
        candidate = {
            "guid": guid,
            "published_at": published,
            "reply_to_guid": parent,
            "discovery_source": "forecast",
        }
        previous = candidates.get(guid)
        if previous is None or int(candidate["published_at"]) > int(
            previous["published_at"]
        ):
            candidates[guid] = candidate
        elif parent and not str(previous.get("reply_to_guid") or ""):
            candidates[guid] = {**previous, "reply_to_guid": parent}
    ordered = sorted(
        candidates.values(),
        key=lambda item: (-int(item["published_at"]), str(item["guid"])),
    )
    return tuple(ordered[: max(1, int(max_count))])


def _merge_discovery_candidates(
    *groups: Iterable[Mapping[str, Any]], max_count: int
) -> tuple[Mapping[str, Any], ...]:
    """Merge forecast and independent X discovery without duplicate ids."""

    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for raw in group:
            if not isinstance(raw, Mapping):
                continue
            guid = str(raw.get("guid") or "").strip()
            if not re.fullmatch(r"\d{15,22}", guid):
                continue
            source = str(raw.get("discovery_source") or "x_syndication")
            if source not in {"forecast", "x_syndication"}:
                source = "x_syndication"
            current = merged.get(guid)
            if current is None:
                current = dict(raw)
                current["guid"] = guid
                current["discovery_sources"] = (source,)
                current["discovery_source"] = source
                merged[guid] = current
                continue
            sources = set(current.get("discovery_sources") or ())
            sources.add(source)
            if int(raw.get("published_at") or 0) > int(
                current.get("published_at") or 0
            ):
                current["published_at"] = int(raw.get("published_at") or 0)
            if not str(current.get("reply_to_guid") or "") and str(
                raw.get("reply_to_guid") or ""
            ):
                current["reply_to_guid"] = str(raw.get("reply_to_guid"))
            current["discovery_sources"] = tuple(sorted(sources))
            current["discovery_source"] = (
                "x_syndication" if "x_syndication" in sources else "forecast"
            )
    ordered = sorted(
        merged.values(),
        key=lambda item: (-int(item.get("published_at") or 0), str(item["guid"])),
    )
    return tuple(ordered[: max(1, int(max_count))])


def _oembed_text(value: object) -> str:
    if not isinstance(value, str):
        raise ResetAlertSourceError("x_oembed_html_invalid")
    markup = value
    lowered = markup.casefold()
    if (
        not markup
        or len(markup) > OEMBED_HTML_LIMIT
        or lowered.count("<blockquote") != 1
        or "<p" not in lowered
        or "<script" in lowered
    ):
        raise ResetAlertSourceError("x_oembed_html_invalid")
    parser = _OEmbedTextParser()
    try:
        parser.feed(markup)
        parser.close()
    except ValueError as exc:
        raise ResetAlertSourceError("x_oembed_html_invalid") from exc
    text = " ".join(html.unescape("".join(parser.parts)).split())
    if not text or len(text) > 4_000:
        raise ResetAlertSourceError("x_oembed_text_invalid")
    return text


def _status_id_from_url(value: object) -> str:
    try:
        parts = urllib.parse.urlsplit(str(value or "").strip())
        port = parts.port
    except ValueError as exc:
        raise ResetAlertSourceError("x_oembed_url_invalid") from exc
    if parts.scheme != "https" or (parts.hostname or "").casefold() not in {
        "x.com",
        "twitter.com",
        "www.x.com",
        "www.twitter.com",
    } or port not in {None, 443} or parts.username is not None or parts.password is not None:
        raise ResetAlertSourceError("x_oembed_url_invalid")
    match = re.fullmatch(r"/[^/]+/status/(\d{15,22})/?", parts.path)
    if match is None or parts.query or parts.fragment:
        raise ResetAlertSourceError("x_oembed_url_invalid")
    return match.group(1)


def _verified_author(value: object) -> bool:
    try:
        parts = urllib.parse.urlsplit(str(value or "").strip())
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and (parts.hostname or "").casefold() == "x.com"
        and parts.path.casefold() in {"/thsottiaux", "/thsottiaux/"}
        and port in {None, 443}
        and parts.username is None
        and parts.password is None
        and not parts.query
        and not parts.fragment
    )


@dataclass(frozen=True, slots=True)
class ResetSignal:
    source_id: str
    item_id: str
    url: str
    published_at: int
    text: str
    kind: str
    official: bool
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def signal_key(self) -> str:
        return _content_hash(
            f"{self.source_id}\0{self.item_id}\0{self.content_hash}"
        )

    @property
    def content_hash(self) -> str:
        return _content_hash(self.text)


@dataclass(frozen=True, slots=True)
class SourceResult:
    source_id: str
    success: bool
    signals: tuple[ResetSignal, ...] = ()
    cursor: Mapping[str, Any] = field(default_factory=dict)
    payload_hash: str = ""
    error_code: str = ""
    candidates: tuple[Mapping[str, Any], ...] = ()
    attempted: bool = True


@dataclass(frozen=True, slots=True)
class ResetAlertDecision:
    level: str
    evidence: str
    window_text: str
    advice: str
    source_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    expires_at: int
    phase: str = "watch"

    @property
    def fingerprint(self) -> str:
        payload = "\0".join(
            (RULE_VERSION, self.phase, *sorted(self.source_ids), *sorted(self.evidence_ids))
        )
        return _content_hash(payload)

    @property
    def event_key(self) -> str:
        return f"reset-alert:{self.phase}:{self.fingerprint[:40]}"


_PRODUCT_RE = re.compile(r"\b(?:codex|chatgpt\s+work)\b", re.IGNORECASE)
_QUOTA_OBJECT_RE = re.compile(
    r"\b(?:usage|rate|message|weekly)\s+limits?\b"
    r"|\b(?:usage\s+)?quotas?\b"
    r"|\b(?:usage|quota)\s+(?:allowance|credits?)\b"
    r"|\bcredits?\s+(?:for|toward)\s+(?:codex|chatgpt\s+work)\b",
    re.IGNORECASE,
)
_RESET_ACTION_RE = re.compile(
    r"\b(reset|replenish|restor|increas|refresh|credit|top[ -]?up)\w*\b",
    re.IGNORECASE,
)
_PAST_ACTION_RE = re.compile(
    r"\b(have|has|had|already|now|just)\s+(?:been\s+)?(?:reset|replenished|restored|increased|refreshed)\b"
    r"|\b(?:was|were)\s+(?:reset|replenished|restored|increased|refreshed)\b"
    r"|\breset\s+(?:is\s+)?complete(?:d)?\b",
    re.IGNORECASE,
)
_PROBLEM_RE = re.compile(
    r"\b(outage|incident|degraded|unavailable|capacity|limit|quota|issue|problem|error)\b",
    re.IGNORECASE,
)
_COMPENSATION_RE = re.compile(
    r"\b(?:compensat\w*|make[- ]?good|credit(?:ed|ing)?\s+back|top[ -]?up|replenish\w*|reset\w*)\b"
    r".{0,80}\b(?:usage|quota|rate\s+limits?|usage\s+limits?)\b"
    r"|\b(?:usage|quota|rate\s+limits?|usage\s+limits?)\b"
    r".{0,80}\b(?:compensat\w*|make[- ]?good|credit(?:ed|ing)?\s+back|top[ -]?up|replenish\w*|reset\w*)\b",
    re.IGNORECASE,
)
_JOKE_RE = re.compile(r"\b(?:joke|joking|just\s+kidding|meme|lol|lmao)\b", re.IGNORECASE)
_NEGATED_RESET_RE = re.compile(
    r"\b(?:no|not|never|won't|will not|don't|doesn't|didn't)\s+"
    r"(?:\w+\s+){0,3}(?:reset|replenish|restor|increas|refresh|credit|compensat|issu|grant|distribut)\w*\b"
    r"|\b(?:whether|might|maybe|perhaps|could)\b.{0,60}\b(?:reset|replenish|restor|increas|refresh|credit|compensat)\w*\b"
    r"|\bnot\s+(?:yet\s+)?available\b", re.I,
)
_UNCERTAIN_STATEMENT_RE = re.compile(
    r"\b(?:might|maybe|perhaps|could|may|whether|if|would|should)\b|[?]", re.I)
_CLEAR_FUTURE_RE = re.compile(
    r"\b(?:will|shall|going\s+to|scheduled\s+to)\b[^.!?;\n]{0,120}"
    r"\b(?:reset|replenish|restore|increase|refresh|credit|top[ -]?up|give|issue|grant|distribut|land)\w*\b", re.I)
_COMPENSATION_INTENT_RE = re.compile(
    r"\b(?:will|shall|going\s+to|working\s+on|planning|intend\s+to)\b[^.!?;\n]{0,100}"
    r"\b(?:compensat\w*|make[- ]?good|replenish\w*|credit\w*|top[ -]?up|reset\w*)\b"
    r"|\bmake\s+(?:this|it)\s+right\b[^.!?;\n]{0,80}\b(?:quota|usage)\s+credits?\b", re.I)


def _uncertain_quota_statement(text: str) -> bool:
    # Eligibility restrictions do not make an otherwise declared grant speculative.
    normalized = re.sub(
        r"\bif\s+you\s+(?:have|are\s+on|create|created)\b[^.!?;\n]{0,100}"
        r"\b(?:paid\s+(?:chatgpt\s+)?plan|account|plus|pro|business)\b[^.!?;\n]*",
        '', text, flags=re.I)
    return bool(_UNCERTAIN_STATEMENT_RE.search(normalized))


def _clear_quota_commitment(text: str) -> bool:
    planned_progressive = any(
        re.search(r"\b(?:we\s+are|we're)\s+(?:resetting|replenishing|restoring|increasing|refreshing|crediting)\b", clause, re.I)
        and _has_quota_object(clause)
        for clause in re.split(r'[.!?;\n]+', text))
    return bool(planned_progressive or _CLEAR_FUTURE_RE.search(text) or (
        re.search(r'\bbanked\s+reset\b', text, re.I)
        and re.search(r"\b(?:lands?\s+(?:by\s+)?(?:end\s+of\s+day|today)|got\s+you\s+covered\s+with)\b", text, re.I)))


def _b1_text_evidence(text: str) -> bool:
    if _JOKE_RE.search(text):
        return False
    # An official document contains FAQ questions; judge its factual clauses,
    # not the question mark elsewhere in that document.
    for clause in re.split(r'(?<=[.!?;\n])\s*', text):
        if (_has_quota_object(clause)
                and (_RESET_ACTION_RE.search(clause) or _PROBLEM_RE.search(clause))
                and not _NEGATED_RESET_RE.search(clause)
                and not _uncertain_quota_statement(clause)
                and not _PAST_ACTION_RE.search(clause)
                and not _AVAILABLE_RESET_RE.search(clause)):
            return True
    return False
_AVAILABLE_RESET_RE = re.compile(
    r"\b(?:have|has|had)\s+(?:(?:already|now|just)\s+)?(?:been\s+)?reset\b"
    r"|\b(?:we(?:'ve)?|we have)\s+(?:(?:already|now|just)\s+)?reset\b"
    r"|\b(?:limits?|quotas?)\s+(?:are\s+)?(?:now\s+)?reset\b"
    r"|\b(?:was|were)\s+(?:just\s+)?reset\b"
    r"|\breset\s+(?:is\s+)?complete(?:d)?\b"
    r"|\b(?:banked\s+resets?|reset\s+(?:credits?|coupons?))\b.{0,100}"
    r"\b(?:available|issued|granted|distributed|landed|added|credited)\b"
    r"|\b(?:issued|granted|distributed|added|credited)\b.{0,100}"
    r"\b(?:banked\s+resets?|reset\s+(?:credits?|coupons?))\b", re.I,
)


def _available_announcement(text: str) -> bool:
    for clause in re.split(r"[.!?;\n]+", text):
        if not _AVAILABLE_RESET_RE.search(clause) or _NEGATED_RESET_RE.search(clause):
            continue
        if not (_QUOTA_OBJECT_RE.search(clause) or re.search(
            r"\bbanked\s+resets?\b|\breset\s+(?:credits?|coupons?|them|it)\b", clause, re.I
        )):
            continue
        if re.search(r"\b(?:will|going to|expected|scheduled|would)\b", clause, re.I):
            continue
        conditional = re.search(r"\bif\b([^,]+),", clause, re.I)
        if re.search(r"\bif\b", clause, re.I) and not (
            conditional and re.search(r"\b(?:paid|plan|plus|pro|business|account)\b", conditional.group(1), re.I)
        ):
            continue
        return True
    return False
_BANKED_ASTRA_RESET_RE = re.compile(
    r"\bbanked\s+reset\w*\b"
    r".*\b(?:every\s+day\s+you\s+do(?:n't| not)\s+have\s+access\s+to\s+astra)\b"
    r".*\bpaid\s+chatgpt\s+plan\b",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class _FutureEvidence:
    when: int
    exact: bool


def _has_quota_object(text: str) -> bool:
    if _BANKED_ASTRA_RESET_RE.search(text) or (
        re.search(r"\b(?:banked\s+resets?|reset\s+(?:credits?|coupons?))\b", text, re.I)
        and re.search(r"\b(?:codex|chatgpt|astra|plus|pro|business)\b", text, re.I)
    ):
        return True
    return bool(_PRODUCT_RE.search(text) and _QUOTA_OBJECT_RE.search(text))


def _future_evidence(
    text: str, *, published_at: int, now: int
) -> _FutureEvidence | None:
    normalized = " ".join(text.casefold().split())
    base = datetime.fromtimestamp(published_at, timezone.utc)
    relative = re.search(
        r"\b(?:in|within)\s*(?:(?:about|approximately)\s+|~\s*)?"
        r"(\d{1,2})\s*hours?\b",
        normalized,
    )
    if relative is not None:
        hours = int(relative.group(1))
        if 1 <= hours <= 48:
            when = published_at + hours * 3600
            return _FutureEvidence(when, True) if when > now else None
    explicit_times = re.findall(
        r"\b20\d{2}-\d{2}-\d{2}[t ]\d{2}:\d{2}(?::\d{2})?(?:z|[+-]\d{2}:?\d{2})\b",
        normalized,
    )
    for raw in explicit_times:
        parsed = _parse_time(raw)
        if parsed is not None and parsed > now:
            return _FutureEvidence(parsed, True)
    if explicit_times:
        return None
    date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", normalized)
    if date_match is not None:
        try:
            day = datetime.strptime(date_match.group(1), "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=BEIJING
            )
        except ValueError:
            day = None
        if day is not None and int(day.timestamp()) > now:
            return _FutureEvidence(int(day.timestamp()), False)
        return None
    published_local = base.astimezone(BEIJING)
    if re.search(r"\b(?:today|end of (?:the )?day|tonight)\b", normalized):
        # Author timezone is not established: a bounded observation window,
        # never a fabricated Beijing execution deadline.
        when = published_at + SIGNAL_LOOKBACK_SECONDS
        return _FutureEvidence(when, False) if when > now else None
    if re.search(r"\btomorrow\b", normalized):
        day = (published_local + timedelta(days=1)).replace(
            hour=23, minute=59, second=59, microsecond=0
        )
        if int(day.timestamp()) > now:
            return _FutureEvidence(int(day.timestamp()), False)
    weekdays = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    for label, weekday in weekdays.items():
        if re.search(rf"\b{label}\b", normalized):
            if re.search(rf"\b(?:last|past|previous)\s+{label}\b", normalized):
                return None
            days = (weekday - published_local.weekday()) % 7
            if days == 0 and re.search(rf"\bnext\s+{label}\b", normalized):
                days = 7
            target = (published_local + timedelta(days=days)).replace(
                hour=23, minute=59, second=59, microsecond=0
            )
            if int(target.timestamp()) > now:
                return _FutureEvidence(int(target.timestamp()), False)
    return None


def _beijing_window(timestamp: int) -> str:
    value = datetime.fromtimestamp(timestamp, BEIJING)
    return value.strftime("预计 %Y-%m-%d %H:%M 前（北京时间）")


def classify_signals(
    signals: Sequence[ResetSignal],
    *,
    forecast_threshold: int,
    now: int,
) -> tuple[ResetAlertDecision, ...]:
    """严格、无模型判级。不能证明的信号安全降级为不告警。"""

    if forecast_threshold < 70:
        raise ValueError("forecast_threshold 不得低于 70")
    decisions: list[ResetAlertDecision] = []
    forecast = next((item for item in signals if item.source_id == 'forecast'
        and now - SOURCE_FRESHNESS_SECONDS <= item.published_at <= now
        and type(item.metadata.get('score')) is int
        and 0 <= item.metadata['score'] <= 100), None)
    forecast_score = forecast.metadata['score'] if forecast else None
    signal_by_identity = {
        (item.source_id, item.item_id): item for item in signals
    }
    for signal in signals:
        if (
            signal.kind == "x_parent_context"
            or not signal.official
            or _JOKE_RE.search(signal.text)
            or not now - SIGNAL_LOOKBACK_SECONDS < signal.published_at <= now
        ):
            continue
        parent_id = str(signal.metadata.get("parent_id") or "").strip()
        parent_signal = signal_by_identity.get(("x_parent_context", parent_id))
        relationship_verified = bool(
            signal.metadata.get("reply_relationship_verified")
        ) and parent_signal is not None
        context_text = (
            f"{parent_signal.text}\n{signal.text}"
            if relationship_verified and parent_signal is not None
            else signal.text
        )
        has_quota_object = _has_quota_object(context_text)
        future = _future_evidence(
            signal.text, published_at=signal.published_at, now=now
        )
        if (
            signal.source_id in {"x_thsottiaux", "openai_status"}
            and has_quota_object
            and _available_announcement(signal.text)
        ):
            decisions.append(ResetAlertDecision(
                level="A", phase="announced_available",
                evidence=f"官方已公告重置/重置券可用；适用范围以原公告为准：{signal.url}",
                window_text="近期官方公告；尚未核实你的个人账户到账情况",
                advice="查看 Codex 额度/重置券，并核对公告中的计划与资格条件",
                source_ids=(signal.source_id,), evidence_ids=(signal.item_id,),
                expires_at=signal.published_at + SIGNAL_LOOKBACK_SECONDS,
            ))
            continue
        if (_PAST_ACTION_RE.search(signal.text) or _NEGATED_RESET_RE.search(signal.text)
                or _uncertain_quota_statement(signal.text)):
            continue
        if (
            signal.source_id in {"x_thsottiaux", "openai_status"}
            and
            has_quota_object
            and
            future is not None
            and future.exact
            and now < future.when <= now + 24 * 3600
            and _RESET_ACTION_RE.search(context_text)
            and _clear_quota_commitment(signal.text)
        ):
            decisions.append(
                ResetAlertDecision(
                    level="A",
                    phase="upcoming",
                    evidence=(
                        "官方来源与已核验直接上下文明确给出 Codex/ChatGPT Work "
                        f"额度动作（证据 ID {signal.item_id}）"
                        if relationship_verified
                        else f"官方来源明确给出 Codex/ChatGPT Work 额度动作（证据 ID {signal.item_id}）"
                    ) + (f"；与预测站低概率 {forecast_score}% 冲突，以官方明确承诺为准"
                         if forecast_score is not None and forecast_score < forecast_threshold else ''),
                    window_text=_beijing_window(future.when),
                    advice="查看原公告并规划使用；重置券到账后需核对适用条件",
                    source_ids=(signal.source_id,),
                    evidence_ids=(
                        (signal.item_id, parent_id)
                        if relationship_verified
                        else (signal.item_id,)
                    ),
                    expires_at=future.when,
                )
            )
            continue
        if (
            signal.source_id == "x_thsottiaux"
            and has_quota_object
            and future is not None
            and _RESET_ACTION_RE.search(context_text)
            and _clear_quota_commitment(signal.text)
            and (relationship_verified or _has_quota_object(signal.text))
        ):
            decisions.append(
                ResetAlertDecision(
                    level="B",
                    phase="upcoming",
                    evidence=f"官方帖子给出未来额度动作，但对象或范围仍有歧义（证据 ID {signal.item_id}）",
                    window_text=(
                        _beijing_window(future.when)
                        if future.exact
                        else "官方提到未来日期，但具体执行时刻仍有歧义（北京时间）"
                    ),
                    advice="继续观察",
                    source_ids=(signal.source_id,),
                    evidence_ids=((signal.item_id, parent_id) if relationship_verified else (signal.item_id,)),
                    expires_at=min(future.when, signal.published_at + SIGNAL_LOOKBACK_SECONDS),
                )
            )
            continue
        if (
            signal.source_id in {"openai_status", "x_thsottiaux"}
            and _PRODUCT_RE.search(signal.text)
            and (has_quota_object or re.search(r"\bcapacity\b", signal.text, re.IGNORECASE))
            and _PROBLEM_RE.search(signal.text)
            and (_COMPENSATION_RE.search(signal.text) or re.search(r'\b(?:quota|usage)\s+credits?\b', signal.text, re.I))
            and _COMPENSATION_INTENT_RE.search(signal.text)
        ):
            decisions.append(
                ResetAlertDecision(
                    level="B",
                    evidence=f"OpenAI 官方确认额度/容量问题并提到补偿或恢复（证据 ID {signal.item_id}）",
                    window_text="执行时间尚未明确（北京时间）",
                    advice="继续观察",
                    source_ids=(signal.source_id,),
                    evidence_ids=(signal.item_id,),
                    expires_at=signal.published_at + SIGNAL_LOOKBACK_SECONDS,
                )
            )

    if forecast is not None:
        try:
            score = int(forecast.metadata.get("score"))
        except (TypeError, ValueError):
            score = -1
        official = next(
            (
                item
                for item in signals
                if item.official
                and now - SIGNAL_LOOKBACK_SECONDS < item.published_at <= now
                and _b1_text_evidence(item.text)
                and (
                    item.source_id == "openai_status"
                    or (
                        item.source_id == "openai_codex_docs"
                        and item.kind == "official_docs_change"
                        and bool(item.metadata.get("content_changed"))
                    )
                    or (
                        item.source_id == "x_thsottiaux"
                        and str(item.metadata.get("discovery_source") or "")
                        == "x_syndication"
                        and item.item_id
                        not in set(forecast.metadata.get("evidence_ids") or ())
                    )
                )
            ),
            None,
        )
        if (score >= forecast_threshold and official is not None
                and now - SOURCE_FRESHNESS_SECONDS <= forecast.published_at <= now):
            decisions.append(
                ResetAlertDecision(
                    level="B",
                    evidence=(
                        f"预测概率 {score}% 且有独立 OpenAI 官方额度信号"
                        f"（证据 ID {official.item_id}）"
                    ),
                    window_text="未来 24 小时内存在较高可能（北京时间）",
                    advice="继续观察",
                    source_ids=("forecast", official.source_id),
                    evidence_ids=(official.item_id,),
                    expires_at=official.published_at + SIGNAL_LOOKBACK_SECONDS,
                )
            )

    # 同一证据指纹只保留最高级别；A 级优先。
    unique: dict[tuple[str, ...], ResetAlertDecision] = {}
    for decision in decisions:
        key = (decision.phase, *sorted(decision.evidence_ids))
        current = unique.get(key)
        if current is None or (decision.level == "A" and current.level != "A"):
            unique[key] = decision
    return tuple(sorted(unique.values(), key=lambda item: (item.level != "A", item.event_key)))


class ResetAlertScanner:
    def __init__(self, config: ResetAlertConfig, *, client: PublicJsonClient | None = None):
        self.config = config
        self.client = client or PublicJsonClient(config.request_timeout_seconds)
        self.verified_cache: dict[str, ResetSignal] = {}
        self.progress: Callable[[], None] = lambda: None
        self.endpoint_states: dict[str, dict[str, Any]] = {}
        self.persist_endpoint: Callable[[str, Mapping[str, Any]], None] = lambda _key, _value: None
        self._scan_now = int(time.time())
        self._scan_monotonic = time.monotonic()
        self._attempted_endpoints: set[str] = set()

    def _endpoint_request(self, endpoint: str, request: Callable[[], Any]) -> Any:
        now = self._scan_now + max(0, int(time.monotonic() - self._scan_monotonic))
        previous = dict(self.endpoint_states.get(endpoint, {}))
        if previous.get('wait_unrepresentable') or int(previous.get('cooldown_until') or 0) > now:
            raise ResetAlertSourceError('source_cooldown_active')
        current = {**previous, 'last_attempt_at': now}
        self.endpoint_states[endpoint] = current
        self.persist_endpoint(endpoint, current)
        self._attempted_endpoints.add(endpoint)
        try:
            value = request()
        except ResetAlertSourceError as exc:
            now = self._scan_now + max(0, int(time.monotonic() - self._scan_monotonic))
            current['last_response_at'] = now
            current['last_error'] = str(exc)
            current['rate_metadata'] = dict(exc.rate_metadata)
            if str(exc) == 'source_http_429':
                count = max(0, int(previous.get('consecutive_429') or 0)) + 1
                hints = exc.rate_metadata
                until = []
                if 'retry_after_seconds' in hints:
                    until.append(now + hints['retry_after_seconds'])
                for name in ['retry_after_at', 'rate_reset_at']:
                    if hints.get(name, 0) > now:
                        until.append(hints[name])
                current.update(consecutive_429=count,
                    cooldown_until=max(until) if until else now + min(43200, 3600 * 2**min(count-1, 4)),
                    cooldown_basis='server' if until or hints.get('wait_unrepresentable') else 'local_backoff',
                    wait_unrepresentable=bool(hints.get('wait_unrepresentable')))
            else:
                current.update(consecutive_429=0, cooldown_until=0, cooldown_basis='', wait_unrepresentable=False)
            self.persist_endpoint(endpoint, current)
            raise
        else:
            now = self._scan_now + max(0, int(time.monotonic() - self._scan_monotonic))
            current.update(last_success_at=now, last_error='', consecutive_429=0,
                           last_response_at=now,
                           cooldown_until=0, cooldown_basis='',
                           wait_unrepresentable=False,
                           rate_metadata=dict(getattr(self.client, 'rate_limit_metadata', {})))
            self.persist_endpoint(endpoint, current)
            return value

    def _forecast(
        self, *, now: int, window_start: int | None = None
    ) -> SourceResult:
        try:
            payload = self.client.get_json(FORECAST_URL, allowed_host="www.willcodexquotareset.com")
            fetched_at = _parse_time(payload.get("fetchedAt"))
            if fetched_at is None or fetched_at > now + 300 or now - fetched_at > SOURCE_FRESHNESS_SECONDS:
                raise ResetAlertSourceError("forecast_stale")
            next_refresh_at = _parse_time(payload.get("nextRefreshAt"))
            source_errors = payload.get("sourceErrors")
            if (
                next_refresh_at is None
                or next_refresh_at < fetched_at - 300
                or next_refresh_at > fetched_at + 6 * 3600
                or not isinstance(source_errors, Mapping)
            ):
                raise ResetAlertSourceError("forecast_schema_invalid")
            degraded = any(bool(value) for value in source_errors.values())
            forecast = payload.get("forecast")
            posts = payload.get("tiboPosts")
            if not isinstance(forecast, Mapping) or not isinstance(posts, list):
                raise ResetAlertSourceError("forecast_schema_invalid")
            raw_score = forecast.get("score")
            if isinstance(raw_score, bool) or not isinstance(raw_score, int):
                raise ResetAlertSourceError("forecast_score_invalid")
            score = raw_score
            if not 0 <= int(score) <= 100:
                raise ResetAlertSourceError("forecast_score_invalid")
            signal = ResetSignal(
                source_id="forecast",
                item_id=f"forecast-{fetched_at}",
                url=FORECAST_URL,
                published_at=fetched_at,
                text=f"forecast score {score}",
                kind="forecast",
                official=False,
                metadata={"score": score},
            )
            evidence_ids: list[str] = []
            for raw in posts[:500]:
                if not isinstance(raw, Mapping):
                    continue
                guid = str(raw.get("guid") or "").strip()
                if re.fullmatch(r"\d{15,22}", guid):
                    evidence_ids.append(guid)
            signal = replace(
                signal,
                metadata={"score": score, "evidence_ids": tuple(evidence_ids)},
            )
            candidates = (
                _forecast_candidates(
                    posts,
                    window_start=window_start,
                    now=now,
                    max_count=500,
                )
                if window_start is not None
                else ()
            )
            digest = _content_hash(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
            return SourceResult(
                "forecast",
                not degraded,
                (() if degraded else (signal,)),
                {"fetched_at": fetched_at},
                digest,
                error_code="forecast_source_errors" if degraded else "",
                candidates=candidates,
            )
        except ResetAlertSourceError as exc:
            return SourceResult("forecast", False, error_code=str(exc))

    def _openai_status(self, *, now: int, window_start: int) -> SourceResult:
        try:
            payload = self.client.get_json(OPENAI_INCIDENTS_URL, allowed_host="status.openai.com")
            incidents = payload.get("incidents")
            if not isinstance(incidents, list):
                raise ResetAlertSourceError("openai_status_schema_invalid")
            signals: list[ResetSignal] = []
            newest = 0
            for incident in incidents:
                if not isinstance(incident, Mapping):
                    continue
                try:
                    incident_id = _safe_identifier(incident.get("id"), limit=120)
                except ResetAlertSourceError:
                    continue
                updates = incident.get("incident_updates")
                if not isinstance(updates, list):
                    continue
                raw_name = incident.get("name")
                name = raw_name.strip() if isinstance(raw_name, str) else ""
                for update in updates:
                    if not isinstance(update, Mapping):
                        continue
                    try:
                        update_id = _safe_identifier(update.get("id"), limit=120)
                    except ResetAlertSourceError:
                        continue
                    published = _parse_time(update.get("created_at") or update.get("updated_at"))
                    if published is None or published < window_start or published > now + 300:
                        continue
                    newest = max(newest, published)
                    raw_body = update.get("body")
                    body = raw_body.strip() if isinstance(raw_body, str) else ""
                    text = " ".join((name, body)).strip()
                    if not text or len(text) > 4_000:
                        continue
                    signals.append(
                        ResetSignal(
                            source_id="openai_status",
                            item_id=f"{incident_id}:{update_id}",
                            url=f"https://status.openai.com/incidents/{incident_id}",
                            published_at=published,
                            text=text,
                            kind="incident_update",
                            official=True,
                            metadata={"incident_id": incident_id},
                        )
                    )
            digest = _content_hash(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
            return SourceResult(
                "openai_status",
                True,
                tuple(signals),
                {"last_item_at": newest or None},
                digest,
            )
        except ResetAlertSourceError as exc:
            return SourceResult("openai_status", False, error_code=str(exc))

    def _openai_codex_docs(
        self, *, now: int, previous_payload_hash: str
    ) -> SourceResult:
        getter = getattr(self.client, "get_document_html", None)
        if not callable(getter):
            return SourceResult(
                "openai_codex_docs",
                False,
                error_code="openai_codex_docs_client_unavailable",
            )
        try:
            document_html, response_metadata = getter(
                OPENAI_CODEX_PRICING_URL, allowed_host="learn.chatgpt.com"
            )
            normalized = _extract_codex_quota_document(document_html)
            digest = DOCS_HASH_PREFIX + _content_hash(normalized)
            signals: tuple[ResetSignal, ...] = ()
            # 首次成功只建立该来源自己的基线；相同正文也不重复成为 B1
            # 的“独立新证据”。只有既有基线之后的额度正文变化才产出信号。
            if previous_payload_hash.startswith(DOCS_HASH_PREFIX) and previous_payload_hash != digest:
                signals = (
                    ResetSignal(
                        source_id="openai_codex_docs",
                        item_id=f"pricing-{digest[:24]}",
                        url=OPENAI_CODEX_PRICING_URL,
                        published_at=now,
                        text=normalized,
                        kind="official_docs_change",
                        official=True,
                        metadata={
                            "content_changed": True,
                            "etag": str(response_metadata.get("etag") or ""),
                            "last_modified": str(
                                response_metadata.get("last_modified") or ""
                            ),
                        },
                    ),
                )
            return SourceResult(
                "openai_codex_docs",
                True,
                signals,
                {
                    "etag": str(response_metadata.get("etag") or ""),
                    "last_modified": str(
                        response_metadata.get("last_modified") or ""
                    ),
                },
                digest,
            )
        except ResetAlertSourceError as exc:
            return SourceResult(
                "openai_codex_docs",
                False,
                error_code=str(exc),
            )

    def _oembed(
        self, status_id: str, *, require_thsottiaux: bool = True
    ) -> tuple[str, Mapping[str, Any]]:
        return self._endpoint_request('oembed', lambda: self._read_oembed(
            status_id, require_thsottiaux=require_thsottiaux))

    def _read_oembed(
        self, status_id: str, *, require_thsottiaux: bool = True
    ) -> tuple[str, Mapping[str, Any]]:
        canonical = f"https://x.com/thsottiaux/status/{status_id}"
        query = urllib.parse.urlencode(
            {"url": canonical, "omit_script": "true", "dnt": "true"}
        )
        payload = self.client.get_json(
            f"{X_OEMBED_URL}?{query}", allowed_host="publish.x.com")
        if _status_id_from_url(payload.get("url")) != status_id:
            raise ResetAlertSourceError("x_oembed_id_mismatch")
        if require_thsottiaux and not _verified_author(payload.get("author_url")):
            raise ResetAlertSourceError("x_oembed_author_mismatch")
        text = _oembed_text(payload.get("html"))
        return text, payload

    def _official_parent_id(self, status_id: str) -> str:
        status_id = _safe_identifier(status_id, limit=22)
        if not status_id.isdigit():
            raise ResetAlertSourceError("x_status_id_invalid")
        url = f"https://x.com/thsottiaux/status/{status_id}"
        return self._endpoint_request('x_parent', lambda: _official_direct_parent_id(
            self.client.get_html(url, allowed_host="x.com"), status_id))

    def _x(self, candidates: Sequence[Mapping[str, Any]]) -> SourceResult:
        signals: list[ResetSignal] = []
        parent_signals: dict[str, ResetSignal] = {}
        errors: list[str] = []
        newest = 0
        requests = 0
        live_verified = 0
        for candidate in candidates:
            self.progress()
            guid = str(candidate.get("guid") or "")
            try:
                guid = _safe_identifier(guid, limit=22)
                if not guid.isdigit():
                    raise ResetAlertSourceError("x_status_id_invalid")
                cached = self.verified_cache.get(guid)
                if cached is not None and not cached.metadata.get("reply_relationship_error"):
                    signals.append(cached)
                    parent = self.verified_cache.get(str(cached.metadata.get("parent_id") or ""))
                    if parent is not None:
                        parent_signals[parent.item_id] = parent
                    newest = max(newest, cached.published_at)
                    continue
                if requests >= self.config.max_candidate_posts:
                    if "x_candidate_backlog" not in errors:
                        errors.append("x_candidate_backlog")
                    continue
                requests += 1
                text, payload = self._oembed(guid)
                published = int(candidate.get("published_at") or 0)
                if published <= 0:
                    raise ResetAlertSourceError("x_published_at_invalid")
                parent_id = str(candidate.get("reply_to_guid") or "").strip()
                parent_verified = False
                parent_author_verified = False
                relationship_error = ""
                if parent_id:
                    if not re.fullmatch(r"\d{15,22}", parent_id):
                        raise ResetAlertSourceError("x_parent_id_invalid")
                    try:
                        official_parent_id = self._official_parent_id(guid)
                        if official_parent_id != parent_id:
                            raise ResetAlertSourceError("x_parent_id_mismatch")
                        parent_text, parent_payload = self._oembed(
                            parent_id, require_thsottiaux=False
                        )
                        parent_author_verified = _verified_author(
                            parent_payload.get("author_url")
                        )
                        parent_signals[parent_id] = ResetSignal(
                            source_id="x_parent_context",
                            item_id=parent_id,
                            url=str(parent_payload.get("url") or ""),
                            # oEmbed 不提供父帖发布时间；0 明确表示未知，不能用
                            # 子帖时间伪造父帖时间。
                            published_at=0,
                            text=parent_text,
                            kind="x_parent_context",
                            official=parent_author_verified,
                            metadata={
                                "author_url": str(
                                    parent_payload.get("author_url") or ""
                                ),
                                "relationship_verified": True,
                            },
                        )
                        parent_verified = True
                    except ResetAlertSourceError as exc:
                        parent_author_verified = False
                        parent_verified = False
                        relationship_error = str(exc)
                        errors.append("x_parent_context:" + relationship_error)
                newest = max(newest, published)
                live_verified += 1
                discovery_source = str(
                    candidate.get("discovery_source") or "x_syndication"
                )
                if discovery_source not in {"forecast", "x_syndication"}:
                    discovery_source = "x_syndication"
                discovery_sources = tuple(
                    str(item)
                    for item in (candidate.get("discovery_sources") or (discovery_source,))
                    if str(item) in {"forecast", "x_syndication"}
                ) or (discovery_source,)
                signals.append(
                    ResetSignal(
                        source_id="x_thsottiaux",
                        item_id=guid,
                        url=f"https://x.com/thsottiaux/status/{guid}",
                        published_at=published,
                        text=text,
                        kind="x_post",
                        official=True,
                        metadata={
                            "parent_id": parent_id,
                            "parent_author_verified": parent_author_verified,
                            "reply_relationship_verified": parent_verified,
                            "reply_relationship_error": relationship_error,
                            "discovery_source": discovery_source,
                            "discovery_sources": discovery_sources,
                            "author_verified": _verified_author(
                                payload.get("author_url")
                            ),
                        },
                    )
                )
            except ResetAlertSourceError as exc:
                errors.append(str(exc))
                if str(exc) in {"source_http_429", "source_cooldown_active"}:
                    break
            # Do not issue another oEmbed after its parent request is rate limited.
            if errors and self.endpoint_states.get('oembed', {}).get('last_error') == 'source_http_429':
                break
        # 无候选是正常空结果；有候选但全部无法官方核验才算 unavailable。
        if candidates and not signals:
            return SourceResult("x_thsottiaux", False, error_code=errors[0] if errors else "x_unavailable")
        signals.extend(parent_signals.values())
        payload_hash = _content_hash("\0".join(item.content_hash for item in signals)) if signals else ""
        return SourceResult(
            "x_thsottiaux",
            not errors,
            tuple(signals),
            {"last_item_at": newest or None, "verified_count": len(signals),
             'live_verified_count': live_verified,
             "candidate_count": len(candidates), "verification_error": errors[0] if errors else ""},
            payload_hash,
            error_code=(errors[0] if errors else ""),
        )

    def scan(
        self,
        *,
        now: int,
        window_starts: Mapping[str, int],
        previous_payload_hashes: Mapping[str, str] | None = None,
    ) -> tuple[SourceResult, ...]:
        self._scan_now = now
        self._scan_monotonic = time.monotonic()
        self._attempted_endpoints = set()
        previous_hashes = previous_payload_hashes or {}
        forecast = self._forecast(
            now=now,
            window_start=int(window_starts["x_thsottiaux"]),
        )
        self.progress()
        status = self._openai_status(
            now=now,
            window_start=int(window_starts["openai_status"]),
        )
        self.progress()
        docs = self._openai_codex_docs(
            now=now,
            previous_payload_hash=str(
                previous_hashes.get("openai_codex_docs") or ""
            ),
        )
        self.progress()
        forecast_candidates = forecast.candidates
        syndication_error = ""
        syndication_candidates: tuple[Mapping[str, Any], ...] = ()
        try:
            syndication_candidates = self._endpoint_request('syndication', lambda: _syndication_candidates(
                self.client.get_html(X_SYNDICATION_URL, allowed_host="syndication.twitter.com"),
                window_start=int(window_starts["x_thsottiaux"]),
                now=now,
                max_count=500,
            ))
        except ResetAlertSourceError as exc:
            syndication_error = str(exc)
        tagged_syndication = tuple(
            {**candidate, "discovery_source": "x_syndication"}
            for candidate in syndication_candidates
        )
        candidates = _merge_discovery_candidates(
            forecast_candidates,
            tagged_syndication,
            max_count=500,
        )
        if candidates:
            x_result = self._x(candidates)
            # A forecast candidate is a usable fallback when the independent
            # syndication request is unavailable.  Keep the source healthy if
            # at least one post was verified by official X oEmbed; retain the
            # syndication error only as a cursor diagnostic.
            if x_result.success and syndication_error:
                x_result = replace(
                    x_result,
                    cursor={
                        **dict(x_result.cursor),
                        "discovery_fallback": "forecast",
                        "syndication_error": syndication_error,
                    },
                )
        elif syndication_error:
            x_result = SourceResult(
                "x_thsottiaux", False, error_code=syndication_error
            )
        else:
            x_result = self._x(())
        x_result = replace(x_result, cursor={
            **dict(x_result.cursor), "syndication_error": syndication_error,
            "discovery_fallback": "forecast" if syndication_error and candidates else "",
            'x_endpoints': {key: dict(value) for key, value in self.endpoint_states.items()},
            'forecast_discovery': {'success': forecast.success, 'error_code': forecast.error_code,
                'candidate_count': len(forecast_candidates), 'checked_at': now},
            'official_verification': {'success': x_result.success,
                'live_verified_count': int(x_result.cursor.get('live_verified_count') or 0),
                'verified_count': len(x_result.signals), 'error_code': x_result.error_code,
                'attempted': 'oembed' in self._attempted_endpoints},
        }, attempted=bool(self._attempted_endpoints))
        if not x_result.attempted:
            x_result = replace(x_result, success=False,
                error_code=x_result.error_code or 'source_cooldown_active')
        self.verified_cache = {
            item.item_id: item for item in (*self.verified_cache.values(), *x_result.signals)
            if item.kind == "x_parent_context" or item.published_at >= now - SIGNAL_LOOKBACK_SECONDS
        }
        return forecast, status, docs, x_result


def schedule_slot(now: int) -> int | None:
    current = datetime.fromtimestamp(now, BEIJING)
    if not 8 <= current.hour <= 23:
        return None
    slot = current.replace(minute=0, second=0, microsecond=0)
    return int(slot.timestamp())


def next_check_at(now: int) -> int:
    current = datetime.fromtimestamp(now, BEIJING)
    if current.hour < 8:
        target = current.replace(hour=8, minute=0, second=0, microsecond=0)
    elif current.hour >= 23:
        target = (current + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    else:
        target = (current + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return int(target.timestamp())


def scan_window_start(now: int, last_success_at: int | None) -> int:
    # The discovery feeds can publish an old item after an empty successful
    # scan. A high-watermark is not proof of complete coverage.
    return now - SIGNAL_LOOKBACK_SECONDS


class ResetAlertWorker:
    """同进程独立 worker；来源或飞书离线均不得终止主服务。"""

    def __init__(
        self,
        *,
        store: StateStore,
        config: ResetAlertConfig,
        send_text: Callable[[str, str], Iterable[str] | str | None],
        is_online: Callable[[], bool],
        stop_event: threading.Event,
        scanner: ResetAlertScanner | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.config = config
        self.send_text = send_text
        self.is_online = is_online
        self.stop_event = stop_event
        self._owns_scanner = scanner is None
        self.scanner = scanner or ResetAlertScanner(config)
        self.clock = clock

    def update_config(self, config: ResetAlertConfig) -> None:
        self.config = config
        if self._owns_scanner:
            # PublicJsonClient 的 timeout 在构造时冻结；热重载时重建默认
            # scanner，确保新的 request_timeout_seconds 真正生效。
            self.scanner = ResetAlertScanner(config)
        else:
            self.scanner.config = config

    def _record_results(
        self,
        results: Sequence[SourceResult],
        *,
        baseline_source_ids: set[str],
        now: int,
        source_rows: Mapping[str, Mapping[str, Any]] | None = None,
        rule_epoch: int | None = None,
    ) -> tuple[ResetSignal, ...]:
        signals: list[ResetSignal] = []
        for result in results:
            old_cursor = json.loads(str((source_rows or {}).get(result.source_id, {}).get("cursor_json") or "{}"))
            cursor = {**old_cursor, **dict(result.cursor)}
            if rule_epoch is not None:
                cursor["rules_v2_started_at"] = rule_epoch
            last_item = max((item.published_at for item in result.signals), default=None)
            self.store.upsert_reset_alert_source(
                result.source_id,
                cursor=cursor,
                success=result.success,
                last_item_at=last_item,
                payload_hash=result.payload_hash,
                error_code=result.error_code,
                mark_baseline=(
                    result.success and result.source_id not in baseline_source_ids
                ),
                now=now,
                attempted=result.attempted,
            )
            for signal in result.signals:
                self.store.record_reset_alert_signal(
                    signal_key=signal.signal_key,
                    source_id=signal.source_id,
                    source_item_id=signal.item_id,
                    source_url=signal.url,
                    published_at=signal.published_at,
                    content_hash=signal.content_hash,
                    signal_kind=signal.kind,
                    is_official=signal.official,
                    payload={"text": signal.text, "metadata": dict(signal.metadata)},
                    observed_at=now,
                )
                signals.append(signal)
        return tuple(signals)

    def scan_due(self, *, now: int | None = None) -> bool:
        timestamp = int(self.clock()) if now is None else int(now)
        if not self.config.enabled:
            return False
        slot = schedule_slot(timestamp)
        if slot is None:
            return False
        rule_epoch = self.store.ensure_reset_alert_rule_epoch(now=timestamp)
        status = self.store.reset_alert_status()
        source_rows = {
            str(item.get("source_id") or ""): item
            for item in status.get("sources", [])
            if str(item.get("source_id") or "")
        }
        baseline_source_ids = {
            source_id
            for source_id, item in source_rows.items()
            if item.get("baseline_completed_at") is not None
        }
        # First upgrade establishes a persisted cutoff for newly supported
        # completed announcements; future promises remain eligible while live.
        cached_signals: list[ResetSignal] = []
        for row in self.store.recent_reset_alert_signals(since=timestamp - SIGNAL_LOOKBACK_SECONDS):
            payload = json.loads(row["payload_json"])
            cached_signals.append(ResetSignal(
                row["source_id"], row["source_item_id"], row["source_url"],
                row["published_at"], payload["text"], row["signal_kind"],
                bool(row["is_official"]), payload.get("metadata", {}),
            ))
        if isinstance(self.scanner, ResetAlertScanner):
            x_cursor = json.loads(str(source_rows.get('x_thsottiaux', {}).get('cursor_json') or '{}'))
            self.scanner.endpoint_states = {
                key: dict(value) for key, value in x_cursor.get('x_endpoints', {}).items()
                if key in {'syndication', 'oembed', 'x_parent'} and isinstance(value, Mapping)
            }
            self.scanner.persist_endpoint = self.store.update_reset_alert_endpoint
            self.scanner.progress = lambda: self.store.mark_reset_alert_worker_heartbeat(now=int(self.clock()))
            self.scanner.verified_cache = {
                item.item_id: item for item in cached_signals
                if (item.source_id == "x_thsottiaux" and item.metadata.get("author_verified"))
                or (item.kind == "x_parent_context" and item.metadata.get("relationship_verified"))
            }
        def source_window_start(source_id: str) -> int:
            source_row = source_rows.get(source_id, {})
            # An earlier empty-success X pass may have advanced
            # last_success_at while leaving no last_item_at.  Re-scan the
            # current Beijing day until a real item is observed; otherwise a
            # post from 00:00--08:00 can be skipped permanently at 09:00.
            return scan_window_start(
                timestamp,
                source_row.get("last_success_at"),
            )

        window_starts = {
            source_id: source_window_start(source_id)
            for source_id in EXPECTED_SOURCE_IDS
        }
        window_start = min(window_starts.values())
        next_at = next_check_at(timestamp)
        if not self.store.begin_reset_alert_run(
            run_slot_at=slot,
            window_start_at=window_start,
            window_end_at=timestamp,
            next_check_at=next_at,
            now=timestamp,
        ):
            return False
        all_success = False
        try:
            results = self.scanner.scan(
                now=timestamp,
                window_starts=window_starts,
                previous_payload_hashes={
                    source_id: str(item.get("payload_hash") or "")
                    for source_id, item in source_rows.items()
                },
            )
            signals = self._record_results(
                results,
                baseline_source_ids=baseline_source_ids,
                now=timestamp,
                source_rows=source_rows,
                rule_epoch=rule_epoch,
            )
            # Retain still-valid independent evidence across partial source recovery.
            signals = tuple({item.signal_key: item for item in (*cached_signals, *signals)}.values())
            all_success = all(item.success for item in results)
            successful_source_ids = {
                item.source_id for item in results if item.success
            }
            # A first healthy run must not replay an old source baseline, but
            # an actually observed, oEmbed-verified X A-level promise that is
            # still live is allowed through once.  The event store's
            # fingerprint keeps later scans exactly-once.
            live_a_ids: set[str] = set()
            if signals:
                for decision in classify_signals(
                    signals,
                    forecast_threshold=self.config.forecast_threshold,
                    now=timestamp,
                ):
                    if not (
                        decision.phase in {"upcoming", "announced_available"}
                        and timestamp < decision.expires_at <= timestamp + 24 * 3600
                    ):
                        continue
                    verified_a = any(
                        signal.item_id in decision.evidence_ids
                        and signal.official
                        and (signal.source_id == "openai_status" or (
                            signal.source_id == "x_thsottiaux"
                            and bool(signal.metadata.get("author_verified"))))
                        for signal in signals
                    )
                    if verified_a:
                        live_a_ids.update(decision.evidence_ids)
            eligible_signals = tuple(
                item
                for item in signals
                if item.source_id in baseline_source_ids
                or (
                    item.kind == "x_parent_context"
                    and "x_thsottiaux" in baseline_source_ids
                )
                or item.item_id in live_a_ids
            )
            if eligible_signals:
                for decision in classify_signals(
                    eligible_signals,
                    forecast_threshold=self.config.forecast_threshold,
                    now=timestamp,
                ):
                    if decision.phase == "announced_available" and not any(
                        signal.item_id in decision.evidence_ids
                        and signal.published_at >= rule_epoch for signal in eligible_signals
                    ):
                        continue
                    self.store.reserve_reset_alert_event(
                        event_key=decision.event_key,
                        level=decision.level,
                        evidence=decision.evidence,
                        window_text=decision.window_text,
                        advice=decision.advice,
                        source_ids=decision.source_ids,
                        fingerprint=decision.fingerprint,
                        expires_at=decision.expires_at,
                        now=timestamp,
                    )
            failures = [item.source_id for item in results if not item.success]
            self.store.finish_reset_alert_run(
                success=all_success,
                bootstrap_completed=set(EXPECTED_SOURCE_IDS).issubset(
                    baseline_source_ids | successful_source_ids
                ),
                next_check_at=next_at,
                error_code=("partial:" + ",".join(failures) if failures else None),
                now=timestamp,
            )
            return True
        except BaseException as exc:
            self.store.finish_reset_alert_run(
                success=False,
                bootstrap_completed=False,
                next_check_at=next_at,
                error_code=type(exc).__name__,
                now=timestamp,
            )
            LOGGER.warning("Codex 重置预警整点检查失败（异常类型=%s）", type(exc).__name__)
            return False

    @staticmethod
    def _backoff(attempt_count: int) -> int:
        return min(3600, 30 * (2 ** max(0, min(int(attempt_count) - 1, 6))))

    def deliver_one(self, *, now: int | None = None) -> bool:
        timestamp = int(self.clock()) if now is None else int(now)
        if schedule_slot(timestamp) is None:
            return False
        if not self.config.enabled or not self.is_online():
            return False
        self.store.expire_reset_alert_deliveries(now=timestamp)
        delivery = self.store.claim_reset_alert_delivery(now=timestamp)
        if delivery is None:
            return False
        delay = self._backoff(delivery.attempt_count)
        if self.stop_event.is_set() or not self.is_online():
            self.store.release_reset_alert_delivery(
                delivery.delivery_id,
                next_attempt_at=timestamp + delay,
                error_code="channel_offline",
                now=timestamp,
            )
            return False
        if not self.store.mark_reset_alert_submitted(delivery.delivery_id, now=timestamp):
            return False
        try:
            result = self.send_text(
                delivery.message_text,
                f"codex-reset-alert:{delivery.event_key}",
            )
            values = (result,) if isinstance(result, str) else tuple(result or ())
            message_ids = tuple(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))
            if not message_ids:
                raise FeishuSendError("预警发送成功但缺少 message_id")
        except FeishuSendNotSubmittedError:
            self.store.release_reset_alert_delivery(
                delivery.delivery_id,
                next_attempt_at=timestamp + delay,
                error_code="channel_not_submitted",
                allow_submitted=True,
                now=timestamp,
            )
            return False
        except FeishuSendRejectedError as exc:
            if exc.retryable:
                self.store.release_reset_alert_delivery(
                    delivery.delivery_id,
                    next_attempt_at=timestamp + delay,
                    error_code="channel_rejected_retryable",
                    rejected=True,
                    allow_submitted=True,
                    now=timestamp,
                )
            else:
                self.store.mark_reset_alert_rejected(
                    delivery.delivery_id,
                    error_code="channel_rejected_permanent",
                    now=timestamp,
                )
            return False
        except MessageChannelOfflineError:
            self.store.mark_reset_alert_uncertain(
                delivery.delivery_id,
                error_code="channel_offline_result_unknown",
                now=timestamp,
            )
            return False
        except FeishuSendError:
            self.store.mark_reset_alert_uncertain(
                delivery.delivery_id,
                error_code="channel_result_unknown",
                now=timestamp,
            )
            return False
        except BaseException as exc:
            self.store.mark_reset_alert_uncertain(
                delivery.delivery_id,
                error_code=type(exc).__name__,
                now=timestamp,
            )
            return False
        return self.store.mark_reset_alert_delivered(
            delivery.delivery_id, message_ids, now=timestamp
        )

    def run(self) -> None:
        started_at = int(self.clock())
        self.store.ensure_reset_alert_rule_epoch(now=started_at)
        self.store.mark_reset_alert_worker_started(now=started_at)
        try:
            self.store.recover_interrupted_reset_alert_run(now=started_at)
            recovery = self.store.recover_interrupted_reset_alerts(now=started_at)
            if any(recovery.values()):
                LOGGER.warning(
                    "恢复上次中断的重置预警投递：未提交已释放=%d，已提交结果未知=%d",
                    recovery["unsubmitted_released"],
                    recovery["submitted_uncertain"],
                )
            while not self.stop_event.is_set():
                try:
                    now = int(self.clock())
                    self.store.mark_reset_alert_worker_heartbeat(now=now)
                    self.deliver_one()
                    self.scan_due(now=now)
                    self.store.mark_reset_alert_worker_heartbeat(now=int(self.clock()))
                    self.deliver_one()
                except BaseException as exc:
                    # 任何单次来源/投递故障只影响本模块，绝不能升级为全局 fatal。
                    LOGGER.warning("Codex 重置预警 worker 已隔离异常（类型=%s）", type(exc).__name__)
                self.stop_event.wait(WORKER_IDLE_SECONDS)
        finally:
            self.store.mark_reset_alert_worker_stopped(now=int(self.clock()))
