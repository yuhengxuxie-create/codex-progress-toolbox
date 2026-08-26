"""Hardened JSON-over-HTTP client implemented with the Python standard library."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http.client import HTTPResponse as ClientHTTPResponse
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_MAX_RESPONSE_BYTES = 1_048_576
TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_FORBIDDEN_REQUEST_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "transfer-encoding",
        "connection",
        "authorization",
        "x-progress-signature",
        "x-progress-timestamp",
        "x-progress-notify-signature",
    }
)
_HEADER_NAME_CHARACTERS = frozenset(
    "!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
)


class HttpRequestError(RuntimeError):
    """A safe-to-log failure from the outbound HTTP client."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        attempts: int = 1,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts
        self.retryable = retryable


class UnsafeUrlError(HttpRequestError):
    """Raised before making a non-HTTPS or cross-origin request."""


class _CrossOriginRedirectError(Exception):
    pass


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.casefold()
    host = (parts.hostname or "").casefold().rstrip(".")
    try:
        explicit_port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError("URL has an invalid port") from exc
    port = explicit_port or (443 if scheme == "https" else 80)
    return scheme, host, port


def _is_loopback_hostname(hostname: str) -> bool:
    host = hostname.casefold().rstrip(".")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_outbound_url(url: str, *, allow_http_localhost: bool = False) -> str:
    """Require HTTPS, except for an explicitly enabled literal loopback host."""

    if not isinstance(url, str) or not url:
        raise UnsafeUrlError("URL must be a non-empty string")
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError as exc:
        raise UnsafeUrlError("URL is malformed") from exc
    scheme = parts.scheme.casefold()
    if scheme not in {"https", "http"}:
        raise UnsafeUrlError("only HTTPS webhook URLs are allowed")
    if not parts.hostname or parts.username is not None or parts.password is not None:
        raise UnsafeUrlError("URL must contain a host and no embedded credentials")
    if parts.fragment:
        raise UnsafeUrlError("URL fragments are not allowed")
    if scheme == "http" and not (
        allow_http_localhost and _is_loopback_hostname(parts.hostname)
    ):
        raise UnsafeUrlError(
            "HTTP is allowed only for localhost when explicitly enabled"
        )
    return url


def _validate_header(name: str, value: str) -> tuple[str, str]:
    header_name = str(name).strip()
    header_value = str(value).strip()
    if not header_name:
        raise HttpRequestError("custom header name cannot be empty")
    if "\r" in header_name or "\n" in header_name:
        raise HttpRequestError("custom header name contains a newline")
    if "\r" in header_value or "\n" in header_value:
        raise HttpRequestError(f"custom header {header_name!r} contains a newline")
    if any(character not in _HEADER_NAME_CHARACTERS for character in header_name):
        raise HttpRequestError(f"invalid custom header name: {header_name!r}")
    if any(ord(character) < 32 or ord(character) == 127 for character in header_value):
        raise HttpRequestError(f"custom header {header_name!r} contains a control character")
    try:
        header_value.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise HttpRequestError(
            f"custom header {header_name!r} is not representable as an HTTP header"
        ) from exc
    if header_name.casefold() in _FORBIDDEN_REQUEST_HEADERS:
        raise HttpRequestError(f"custom header {header_name!r} is reserved")
    return header_name, header_value


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    """Let urllib follow redirects only when the normalized origin is unchanged."""

    def redirect_request(
        self,
        req: Request,
        fp: ClientHTTPResponse,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> Request | None:
        resolved = urljoin(req.full_url, newurl)
        if _origin(req.full_url) != _origin(resolved):
            raise _CrossOriginRedirectError("cross-origin redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, resolved)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes
    attempts: int = 1

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpRequestError("HTTP response did not contain valid UTF-8 JSON") from exc


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    raw_value = headers.get("Retry-After") or headers.get("retry-after")
    if not raw_value:
        return None
    try:
        return max(0.0, min(float(raw_value), 10.0))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw_value)
            now = parsedate_to_datetime(
                time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
            )
            return max(0.0, min((retry_at - now).total_seconds(), 10.0))
        except (TypeError, ValueError, OverflowError):
            return None


class JsonHttpClient:
    """Bounded JSON POST client with finite transient-error retries."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        max_attempts: int = 3,
        allow_http_localhost: bool = False,
        initial_backoff_seconds: float = 0.25,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        if initial_backoff_seconds < 0:
            raise ValueError("initial_backoff_seconds cannot be negative")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self.timeout_seconds = float(timeout_seconds)
        self.max_attempts = int(max_attempts)
        self.allow_http_localhost = bool(allow_http_localhost)
        self.initial_backoff_seconds = float(initial_backoff_seconds)
        self.max_response_bytes = int(max_response_bytes)
        self._sleep = sleep
        self._clock = clock
        self._opener = build_opener(_SameOriginRedirectHandler())

    def _headers(
        self,
        body: bytes,
        custom_headers: Mapping[str, str] | None,
        *,
        auth_type: str,
        bearer_token: str,
        hmac_secret: str,
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "codex-progress-notify/1.2",
        }
        for raw_name, raw_value in (custom_headers or {}).items():
            name, value = _validate_header(raw_name, raw_value)
            headers[name] = value

        normalized_auth = auth_type.strip().casefold() or "none"
        if normalized_auth == "hmac":
            normalized_auth = "hmac-sha256"
        if normalized_auth == "bearer":
            if not bearer_token:
                raise HttpRequestError("bearer authentication has no token")
            if "\r" in bearer_token or "\n" in bearer_token:
                raise HttpRequestError("bearer token contains a newline")
            try:
                bearer_token.encode("latin-1")
            except UnicodeEncodeError as exc:
                raise HttpRequestError("bearer token is not a valid HTTP header value") from exc
            headers["Authorization"] = f"Bearer {bearer_token}"
        elif normalized_auth == "hmac-sha256":
            if not hmac_secret:
                raise HttpRequestError("HMAC authentication has no secret")
            timestamp = str(int(self._clock()))
            signed_content = timestamp.encode("ascii") + b"." + body
            digest = hmac.new(
                hmac_secret.encode("utf-8"), signed_content, hashlib.sha256
            ).hexdigest()
            headers["X-Progress-Timestamp"] = timestamp
            headers["X-Progress-Signature"] = f"sha256={digest}"
        elif normalized_auth != "none":
            raise HttpRequestError(f"unsupported authentication type: {auth_type!r}")
        return headers

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
        auth_type: str = "none",
        bearer_token: str = "",
        hmac_secret: str = "",
    ) -> HttpResponse:
        validated_url = validate_outbound_url(
            url, allow_http_localhost=self.allow_http_localhost
        )
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        request_headers = self._headers(
            body,
            headers,
            auth_type=auth_type,
            bearer_token=bearer_token,
            hmac_secret=hmac_secret,
        )

        last_status: int | None = None
        last_retryable = False
        for attempt in range(1, self.max_attempts + 1):
            request = Request(
                validated_url,
                data=body,
                headers=request_headers,
                method="POST",
            )
            retry_after: float | None = None
            try:
                with self._opener.open(request, timeout=self.timeout_seconds) as response:
                    response_body = response.read(self.max_response_bytes + 1)
                    if len(response_body) > self.max_response_bytes:
                        raise HttpRequestError(
                            "HTTP response exceeded the configured size limit",
                            status_code=response.status,
                            attempts=attempt,
                        )
                    return HttpResponse(
                        status_code=response.status,
                        headers=dict(response.headers.items()),
                        body=response_body,
                        attempts=attempt,
                    )
            except _CrossOriginRedirectError as exc:
                raise UnsafeUrlError(
                    "cross-origin HTTP redirect was refused", attempts=attempt
                ) from exc
            except HTTPError as exc:
                last_status = exc.code
                last_retryable = exc.code in TRANSIENT_STATUS_CODES
                retry_after = _retry_after_seconds(dict(exc.headers.items()))
                try:
                    exc.read(self.max_response_bytes + 1)
                except OSError:
                    pass
                finally:
                    exc.close()
                if not last_retryable or attempt >= self.max_attempts:
                    raise HttpRequestError(
                        f"HTTP endpoint returned status {exc.code}",
                        status_code=exc.code,
                        attempts=attempt,
                        retryable=last_retryable,
                    ) from exc
            except (URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
                last_retryable = True
                if attempt >= self.max_attempts:
                    raise HttpRequestError(
                        "HTTP request failed after bounded retries",
                        status_code=last_status,
                        attempts=attempt,
                        retryable=True,
                    ) from exc

            delay = (
                retry_after
                if retry_after is not None
                else self.initial_backoff_seconds * (2 ** (attempt - 1))
            )
            if delay > 0:
                self._sleep(min(delay, 10.0))

        raise HttpRequestError(
            "HTTP request failed",
            status_code=last_status,
            attempts=self.max_attempts,
            retryable=last_retryable,
        )


def post_json(
    url: str,
    payload: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float = 10.0,
    max_attempts: int = 3,
    allow_http_localhost: bool = False,
) -> HttpResponse:
    """Convenience entry point for callers that do not need auth options."""

    client = JsonHttpClient(
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        allow_http_localhost=allow_http_localhost,
    )
    return client.post_json(url, payload, headers=headers)
