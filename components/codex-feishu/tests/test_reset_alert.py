from __future__ import annotations

import json
import sqlite3
import threading
import urllib.parse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping

import pytest

from progress_wx import cli
from progress_wx import service as service_module
from progress_wx.channel import MessageChannelOfflineError
from progress_wx.config import ConfigError, ResetAlertConfig
from progress_wx.feishu import (
    FeishuSendError,
    FeishuSendNotSubmittedError,
    FeishuSendRejectedError,
)
from progress_wx.reset_alert import (
    BEIJING,
    EXPECTED_SOURCE_IDS,
    FORECAST_URL,
    OPENAI_CODEX_PRICING_URL,
    OPENAI_INCIDENTS_URL,
    PublicJsonClient,
    ResetAlertScanner,
    ResetAlertSourceError,
    ResetAlertWorker,
    ResetSignal,
    SourceResult,
    _extract_codex_quota_document,
    _merge_discovery_candidates,
    _official_direct_parent_id,
    _syndication_candidates,
    X_OEMBED_URL,
    X_SYNDICATION_URL,
    classify_signals,
    next_check_at,
    scan_window_start,
    schedule_slot,
)
from progress_wx.state import StateStore


def _ts(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp())


NOW = _ts("2026-08-31T12:00:00+08:00")


class _HttpResponse:
    def __init__(
        self,
        data: bytes,
        *,
        url: str = OPENAI_CODEX_PRICING_URL,
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
        content_encoding: str = "identity",
        content_length: str | None = None,
    ) -> None:
        self._data = data
        self._url = url
        self.status = status
        self.headers = {
            "Content-Type": content_type,
            "Content-Encoding": content_encoding,
            "Content-Length": (
                str(len(data)) if content_length is None else content_length
            ),
            "ETag": "fixture-etag",
            "Last-Modified": "fixture-time",
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def geturl(self) -> str:
        return self._url

    def read(self, limit: int) -> bytes:
        return self._data[:limit]


class _HttpOpener:
    def __init__(self, response: _HttpResponse) -> None:
        self.response = response

    def open(self, _request, *, timeout: float):
        assert timeout > 0
        return self.response


def _document_client(response: _HttpResponse) -> PublicJsonClient:
    client = PublicJsonClient(3)
    client._opener = _HttpOpener(response)  # type: ignore[assignment]
    return client


def test_document_client_accepts_only_exact_bounded_utf8_html() -> None:
    client = _document_client(_HttpResponse(b"<html></html>"))
    markup, metadata = client.get_document_html(
        OPENAI_CODEX_PRICING_URL, allowed_host="learn.chatgpt.com"
    )
    assert markup == "<html></html>"
    assert metadata == {
        "etag": "fixture-etag",
        "last_modified": "fixture-time",
        "content_type": "text/html; charset=utf-8",
    }


@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        (
            _HttpResponse(
                b"<html></html>",
                url="https://learn.chatgpt.com/docs/other",
            ),
            "source_redirect_rejected",
        ),
        (_HttpResponse(b"<html></html>", status=201), "source_http_status_invalid"),
        (
            _HttpResponse(
                b"<html></html>",
                content_type="text/htmlfoo; charset=utf-8",
            ),
            "source_content_type_invalid",
        ),
        (
            _HttpResponse(b"<html></html>", content_type="text/plain; charset=utf-8"),
            "source_content_type_invalid",
        ),
        (
            _HttpResponse(b"<html></html>", content_encoding="gzip"),
            "source_content_encoding_invalid",
        ),
        (
            _HttpResponse(b"<html></html>", content_length="invalid"),
            "source_content_length_invalid",
        ),
        (
            _HttpResponse(b"<html></html>", content_length=str(2 * 1024 * 1024 + 1)),
            "source_payload_too_large",
        ),
        (
            _HttpResponse(b"<html></html>", content_type="text/html; charset=latin-1"),
            "source_html_charset_invalid",
        ),
        (
            _HttpResponse(b"\xff", content_type="text/html; charset=utf-8"),
            "source_html_invalid",
        ),
    ],
)
def test_document_client_fails_closed_on_transport_metadata(
    response: _HttpResponse, error_code: str
) -> None:
    with pytest.raises(ResetAlertSourceError, match=error_code):
        _document_client(response).get_document_html(
            OPENAI_CODEX_PRICING_URL, allowed_host="learn.chatgpt.com"
        )


def test_document_client_rejects_actual_chunked_payload_over_limit() -> None:
    response = _HttpResponse(
        b"x" * (2 * 1024 * 1024 + 1),
        content_length="",
    )
    with pytest.raises(ResetAlertSourceError, match="source_payload_too_large"):
        _document_client(response).get_document_html(
            OPENAI_CODEX_PRICING_URL, allowed_host="learn.chatgpt.com"
        )


def _signal(
    text: str,
    *,
    source: str = "openai_status",
    item: str = "item-1",
    official: bool = True,
    kind: str = "incident_update",
    published_at: int = NOW - 60,
    metadata: dict[str, object] | None = None,
) -> ResetSignal:
    return ResetSignal(
        source,
        item,
        f"https://example.test/{item}",
        published_at,
        text,
        kind,
        official,
        metadata or {},
    )


@pytest.mark.parametrize(
    "text",
    [
        "Codex reset tomorrow",
        "Codex model release Monday",
        "Just a joke: Codex usage limits reset within 3 hours lol",
        "Codex service has now been restored",
    ],
)
def test_classifier_rejects_bare_reset_release_joke_and_past(text: str) -> None:
    assert classify_signals((_signal(text),), forecast_threshold=70, now=NOW) == ()


def test_classifier_a_requires_explicit_quota_object_and_exact_24h_time() -> None:
    decisions = classify_signals(
        (_signal("Codex usage limits will reset within 6 hours"),),
        forecast_threshold=70,
        now=NOW,
    )
    assert [item.level for item in decisions] == ["A"]
    assert decisions[0].expires_at == NOW - 60 + 6 * 3600
    assert classify_signals(
        (_signal("Codex usage limits will reset tomorrow"),),
        forecast_threshold=70,
        now=NOW,
    ) == ()


def test_banked_astra_reset_phrase_is_strict_a_with_approximate_hours() -> None:
    text = (
        "We will give one banked reset for every day you don't have access to Astra "
        "on your paid ChatGPT plan, starting today. First one will land in ~ 3 hours."
    )
    signal = _signal(
        text,
        source="x_thsottiaux",
        item="2095651088502591861",
        kind="x_post",
        metadata={"author_verified": True, "discovery_source": "forecast"},
    )
    decisions = classify_signals((signal,), forecast_threshold=70, now=NOW)
    assert [item.level for item in decisions] == ["A"]
    assert decisions[0].expires_at == NOW - 60 + 3 * 3600
    assert classify_signals(
        (
            replace(
                signal,
                text="Astra is launching and a reset may happen tomorrow for paid users",
            ),
        ),
        forecast_threshold=70,
        now=NOW,
    ) == ()


def test_low_forecast_does_not_cancel_verified_a_and_duplicate_is_one_event() -> None:
    text = (
        "We will give one banked reset for every day you don't have access to Astra "
        "on your paid ChatGPT plan, starting today. First one will land in about 3 hours."
    )
    signal = _signal(
        text,
        source="x_thsottiaux",
        item="2095651088502591861",
        kind="x_post",
        metadata={"author_verified": True, "discovery_source": "forecast"},
    )
    forecast = _signal(
        "forecast score 12",
        source="forecast",
        item="forecast-1",
        official=False,
        kind="forecast",
        metadata={"score": 12, "evidence_ids": (signal.item_id,)},
    )
    decisions = classify_signals(
        (forecast, signal, signal), forecast_threshold=70, now=NOW
    )
    assert len(decisions) == 1
    assert decisions[0].level == "A"


def test_old_banked_astra_reset_is_not_alertable() -> None:
    signal = _signal(
        "We will give one banked reset for every day you don't have access to Astra "
        "on your paid ChatGPT plan, starting today. First one will land in ~ 3 hours.",
        source="x_thsottiaux",
        item="2095651088502591861",
        kind="x_post",
        published_at=NOW - 4 * 3600,
        metadata={"author_verified": True, "discovery_source": "forecast"},
    )
    assert classify_signals((signal,), forecast_threshold=70, now=NOW) == ()


def test_verified_reply_context_can_supply_quota_object_for_a_or_b2() -> None:
    parent = _signal(
        "Codex usage limits are the subject of this update",
        source="x_parent_context",
        item="111111111111111",
        official=False,
        kind="x_parent_context",
        published_at=0,
    )
    exact_child = _signal(
        "We will reset them within 4 hours",
        source="x_thsottiaux",
        item="222222222222222",
        kind="x_post",
        metadata={
            "parent_id": parent.item_id,
            "reply_relationship_verified": True,
            "discovery_source": "forecast",
        },
    )
    ambiguous_child = replace(exact_child, item_id="333333333333333", text="We will reset them tomorrow")
    exact = classify_signals(
        (exact_child, parent), forecast_threshold=70, now=NOW
    )
    ambiguous = classify_signals(
        (ambiguous_child, parent), forecast_threshold=70, now=NOW
    )
    assert [item.level for item in exact] == ["A"]
    assert exact[0].evidence_ids == (exact_child.item_id, parent.item_id)
    assert [item.level for item in ambiguous] == ["B"]
    assert classify_signals(
        (replace(exact_child, metadata={"parent_id": parent.item_id}), parent),
        forecast_threshold=70,
        now=NOW,
    ) == ()


def test_b1_requires_independent_official_quota_signal() -> None:
    forecast = _signal(
        "forecast score 70",
        source="forecast",
        item="forecast-1",
        official=False,
        kind="forecast",
        metadata={"score": 70},
    )
    status = _signal("Codex usage limits incident is under investigation")
    same_chain_x = _signal(
        "Codex usage limits incident is under investigation",
        source="x_thsottiaux",
        item="444444444444444",
        kind="x_post",
        metadata={"discovery_source": "forecast"},
    )
    assert [item.level for item in classify_signals((forecast, status), forecast_threshold=70, now=NOW)] == ["B"]
    assert classify_signals((forecast, same_chain_x), forecast_threshold=70, now=NOW) == ()
    assert classify_signals((replace(forecast, metadata={"score": 69}), status), forecast_threshold=70, now=NOW) == ()


def test_b3_requires_quota_compensation_not_generic_service_restore() -> None:
    positive = _signal(
        "Codex capacity incident confirmed; we will replenish affected usage limits"
    )
    generic = _signal("Codex capacity incident mitigated; service has been restored")
    assert [item.level for item in classify_signals((positive,), forecast_threshold=70, now=NOW)] == ["B"]
    assert classify_signals((generic,), forecast_threshold=70, now=NOW) == ()


def test_forecast_threshold_cannot_be_lowered() -> None:
    with pytest.raises(ValueError, match="70"):
        classify_signals((), forecast_threshold=69, now=NOW)


def test_official_react_hydration_parent_is_bound_to_target_child() -> None:
    child = "2092316228497063958"
    parent = "2092256496063033418"
    other = "2092000000000000000"
    markup = f'''<html><script>
      $R[1]={{__typename:"Tweet",rest_id:"{other}",reply_to_results:$R[2]={{__ref:"TweetResults:2092111111111111111"}}}};
      $R[3]={{__typename:"TweetResults",rest_id:"{child}"}};
      $R[4]={{__typename:"Tweet",rest_id:"{child}",reply_to_results:$R[5]={{__ref:"TweetResults:{parent}"}}}};
      window.noise="{parent}";
    </script></html>'''
    assert _official_direct_parent_id(markup, child) == parent
    with pytest.raises(ResetAlertSourceError, match="unverified"):
        _official_direct_parent_id(
            f'<html><script>window.noise="{parent}";$R[1]={{__typename:"Tweet",rest_id:"{child}"}}</script></html>',
            child,
        )
    with pytest.raises(ResetAlertSourceError, match="unverified"):
        _official_direct_parent_id(
            markup.replace(parent, "2092333333333333333", 1)
            + f'$R[9]={{__typename:"Tweet",rest_id:"{child}",reply_to_results:$R[10]={{__ref:"TweetResults:{parent}"}}}}',
            child,
        )


def test_json_hydration_compatibility_is_scoped_to_child() -> None:
    child = "2092316228497063958"
    parent = "2092256496063033418"
    payload = {
        f"TweetResults:{child}": {
            "rest_id": child,
            "reply_to_results": {"__ref": f"TweetResults:{parent}"},
        },
        "timeline": {
            "rest_id": "2092000000000000000",
            "reply_to_results": {"__ref": "TweetResults:2092111111111111111"},
        },
    }
    markup = '<script type="application/json">' + json.dumps(payload) + "</script>"
    assert _official_direct_parent_id(markup, child) == parent


def test_reply_parent_ignores_nested_quoted_tweet_relationships() -> None:
    child = "2092316228497063958"
    parent = "2092256496063033418"
    nested_parent = "2092111111111111111"
    payload = {
        f"TweetResults:{child}": {
            "rest_id": child,
            "reply_to_results": {"__ref": f"TweetResults:{parent}"},
            "quoted_status_result": {
                "result": {
                    "rest_id": "2092000000000000000",
                    "reply_to_results": {
                        "__ref": f"TweetResults:{nested_parent}"
                    },
                }
            },
        }
    }
    markup = '<script type="application/json">' + json.dumps(payload) + "</script>"
    assert _official_direct_parent_id(markup, child) == parent
    del payload[f"TweetResults:{child}"]["reply_to_results"]
    markup = '<script type="application/json">' + json.dumps(payload) + "</script>"
    with pytest.raises(ResetAlertSourceError, match="unverified"):
        _official_direct_parent_id(markup, child)


class _FixtureClient:
    def __init__(self, payloads: dict[str, object]):
        self.payloads = payloads

    def get_json(self, url: str, *, allowed_host: str):
        del allowed_host
        value = self.payloads[url]
        if isinstance(value, BaseException):
            raise value
        return value

    def get_html(self, url: str, *, allowed_host: str) -> str:
        del allowed_host
        value = self.payloads[url]
        if isinstance(value, BaseException):
            raise value
        return str(value)

    def get_document_html(
        self, url: str, *, allowed_host: str
    ) -> tuple[str, dict[str, str]]:
        del allowed_host
        value = self.payloads[url]
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, tuple):
            return value
        return str(value), {"etag": "", "last_modified": ""}


class _XFixtureClient(_FixtureClient):
    def __init__(
        self,
        payloads: dict[str, object],
        oembeds: dict[str, Mapping[str, object]],
    ) -> None:
        super().__init__(payloads)
        self.oembeds = oembeds
        self.oembed_calls: list[str] = []

    def get_json(self, url: str, *, allowed_host: str):
        if url.startswith(X_OEMBED_URL + "?"):
            assert allowed_host == "publish.x.com"
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            canonical = str((query.get("url") or [""])[0])
            status_id = canonical.rsplit("/", 1)[-1]
            self.oembed_calls.append(status_id)
            value = self.oembeds[status_id]
            if isinstance(value, BaseException):
                raise value
            return value
        return super().get_json(url, allowed_host=allowed_host)


def _oembed_payload(
    status_id: str,
    text: str,
    *,
    author_url: str = "https://x.com/thsottiaux",
    response_status_id: str | None = None,
) -> dict[str, object]:
    actual_id = response_status_id or status_id
    return {
        "url": f"https://x.com/thsottiaux/status/{actual_id}",
        "author_url": author_url,
        "html": f'<blockquote class="twitter-tweet"><p>{text}</p></blockquote>',
    }


def _forecast_payload(score: object = 50) -> dict[str, object]:
    return {
        "fetchedAt": datetime.fromtimestamp(NOW - 30, timezone.utc).isoformat(),
        "nextRefreshAt": datetime.fromtimestamp(NOW + 3600, timezone.utc).isoformat(),
        "sourceErrors": {},
        "forecast": {"score": score},
        "tiboPosts": [],
    }


@pytest.mark.parametrize("score", [True, 70.5, "70", None])
def test_forecast_score_is_strict_integer(score: object) -> None:
    scanner = ResetAlertScanner(
        ResetAlertConfig(),
        client=_FixtureClient({"https://www.willcodexquotareset.com/api/forecast": _forecast_payload(score)}),
    )
    result = scanner._forecast(now=NOW)
    assert result.success is False
    assert result.error_code == "forecast_score_invalid"


def test_forecast_only_records_provenance_and_source_errors_fail_closed() -> None:
    payload = _forecast_payload(50)
    payload["tiboPosts"] = [
        {"guid": "2092316228497063958", "pubDate": datetime.fromtimestamp(NOW - 7200, timezone.utc).isoformat()},
        {"guid": "2092316228497063959", "pubDate": datetime.fromtimestamp(NOW - 1800, timezone.utc).isoformat()},
    ]
    scanner = ResetAlertScanner(
        ResetAlertConfig(),
        client=_FixtureClient({"https://www.willcodexquotareset.com/api/forecast": payload}),
    )
    result = scanner._forecast(now=NOW)
    assert result.candidates == ()
    assert result.signals[0].metadata["evidence_ids"] == (
        "2092316228497063958",
        "2092316228497063959",
    )
    payload["sourceErrors"] = {"x": "timeout"}
    assert scanner._forecast(now=NOW).error_code == "forecast_source_errors"


def _x_scan_scanner(
    *,
    forecast_payload: dict[str, object],
    syndication: object,
    oembeds: dict[str, Mapping[str, object]],
) -> tuple[ResetAlertScanner, _XFixtureClient]:
    client = _XFixtureClient(
        {
            FORECAST_URL: forecast_payload,
            X_SYNDICATION_URL: syndication,
        },
        oembeds,
    )
    scanner = ResetAlertScanner(ResetAlertConfig(), client=client)
    scanner._openai_status = lambda **_kwargs: SourceResult("openai_status", True)  # type: ignore[method-assign]
    scanner._openai_codex_docs = lambda **_kwargs: SourceResult(  # type: ignore[method-assign]
        "openai_codex_docs", True
    )
    return scanner, client


def test_forecast_tibo_post_is_only_discovery_and_oembed_body_is_authoritative() -> None:
    status_id = "2095651088502591861"
    official_text = (
        "We will give one banked reset for every day you don't have access to Astra "
        "on your paid ChatGPT plan, starting today. First one will land in ~ 3 hours."
    )
    payload = _forecast_payload(40)
    payload["tiboPosts"] = [
        {
            "guid": status_id,
            "pubDate": datetime.fromtimestamp(NOW - 1800, timezone.utc).isoformat(),
            "title": "third-party classifier says reset tomorrow",
            "context": "untrusted context",
        }
    ]
    client = _XFixtureClient(
        {FORECAST_URL: payload},
        {status_id: _oembed_payload(status_id, official_text)},
    )
    scanner = ResetAlertScanner(ResetAlertConfig(), client=client)
    forecast = scanner._forecast(now=NOW, window_start=NOW - 3600)
    assert forecast.candidates == (
        {
            "guid": status_id,
            "published_at": NOW - 1800,
            "reply_to_guid": "",
            "discovery_source": "forecast",
        },
    )
    verified = scanner._x(forecast.candidates)
    assert verified.success is True
    assert verified.signals[0].text == official_text
    assert "third-party" not in verified.signals[0].text
    assert verified.signals[0].official is True
    assert client.oembed_calls == [status_id]


def test_forecast_and_syndication_discovery_are_deduplicated() -> None:
    status_id = "2095651088502591861"
    published = datetime.fromtimestamp(NOW - 1800, timezone.utc).isoformat()
    payload = _forecast_payload(40)
    payload["tiboPosts"] = [{"guid": status_id, "pubDate": published}]
    syndication = '<script type="application/json">' + json.dumps(
        {
            "timeline": [
                {
                    "id_str": status_id,
                    "created_at": published,
                    "conversation_id_str": status_id,
                    "user": {"screen_name": "thsottiaux"},
                }
            ]
        }
    ) + "</script>"
    scanner, client = _x_scan_scanner(
        forecast_payload=payload,
        syndication=syndication,
        oembeds={status_id: _oembed_payload(status_id, "Codex usage limits will reset within 3 hours")},
    )
    results = scanner.scan(
        now=NOW,
        window_starts={source_id: NOW - 3600 for source_id in EXPECTED_SOURCE_IDS},
    )
    x_result = next(item for item in results if item.source_id == "x_thsottiaux")
    assert x_result.success is True
    assert [item.item_id for item in x_result.signals] == [status_id]
    assert client.oembed_calls == [status_id]


def test_syndication_429_falls_back_to_forecast_discovery() -> None:
    status_id = "2095651088502591861"
    payload = _forecast_payload(40)
    payload["tiboPosts"] = [
        {
            "guid": status_id,
            "pubDate": datetime.fromtimestamp(NOW - 1800, timezone.utc).isoformat(),
        }
    ]
    scanner, client = _x_scan_scanner(
        forecast_payload=payload,
        syndication=ResetAlertSourceError("source_http_429"),
        oembeds={status_id: _oembed_payload(status_id, "Codex usage limits will reset within 3 hours")},
    )
    results = scanner.scan(
        now=NOW,
        window_starts={source_id: NOW - 3600 for source_id in EXPECTED_SOURCE_IDS},
    )
    x_result = next(item for item in results if item.source_id == "x_thsottiaux")
    assert x_result.success is True
    assert [item.item_id for item in x_result.signals] == [status_id]
    assert x_result.cursor["discovery_fallback"] == "forecast"
    assert x_result.cursor["syndication_error"] == "source_http_429"
    assert client.oembed_calls == [status_id]


def test_forecast_candidate_with_zero_independent_x_candidates_is_unavailable() -> None:
    status_id = "2095651088502591861"
    payload = _forecast_payload(40)
    payload["tiboPosts"] = [
        {
            "guid": status_id,
            "pubDate": datetime.fromtimestamp(NOW - 1800, timezone.utc).isoformat(),
        }
    ]
    scanner, _client = _x_scan_scanner(
        forecast_payload=payload,
        syndication='<script type="application/json">{"timeline":[]}</script>',
        oembeds={status_id: ResetAlertSourceError("x_oembed_author_mismatch")},
    )
    results = scanner.scan(
        now=NOW,
        window_starts={source_id: NOW - 3600 for source_id in EXPECTED_SOURCE_IDS},
    )
    x_result = next(item for item in results if item.source_id == "x_thsottiaux")
    assert x_result.success is False
    assert x_result.error_code == "x_oembed_author_mismatch"


@pytest.mark.parametrize(
    "oembed",
    [
        _oembed_payload(
            "2095651088502591861",
            "Codex usage limits will reset within 3 hours",
            author_url="https://x.com/not-thsottiaux",
        ),
        _oembed_payload(
            "2095651088502591861",
            "Codex usage limits will reset within 3 hours",
            response_status_id="2095651088502591862",
        ),
    ],
)
def test_forecast_candidate_requires_exact_oembed_author_and_status_id(oembed: Mapping[str, object]) -> None:
    status_id = "2095651088502591861"
    scanner = ResetAlertScanner(
        ResetAlertConfig(),
        client=_XFixtureClient(
            {},
            {status_id: oembed},
        ),
    )
    result = scanner._x(
        (
            {
                "guid": status_id,
                "published_at": NOW - 1800,
                "reply_to_guid": "",
                "discovery_source": "forecast",
            },
        )
    )
    assert result.success is False
    assert result.signals == ()


def test_syndication_is_independent_author_filtered_and_windowed() -> None:
    fresh = datetime.fromtimestamp(NOW - 1800, timezone.utc).isoformat()
    old = datetime.fromtimestamp(NOW - 7200, timezone.utc).isoformat()
    payload = {
        "timeline": [
            {
                "id_str": "2092316228497063958",
                "created_at": fresh,
                "conversation_id_str": "2092256496063033418",
                "in_reply_to_status_id_str": "2092256496063033418",
                "user": {"screen_name": "thsottiaux"},
            },
            {
                "id_str": "2092316228497063959",
                "created_at": old,
                "conversation_id_str": "2092316228497063959",
                "user": {"screen_name": "thsottiaux"},
            },
            {
                "id_str": "2092316228497063960",
                "created_at": fresh,
                "conversation_id_str": "2092316228497063960",
                "user": {"screen_name": "someone_else"},
            },
            {
                "id_str": "2092316228497063961",
                "created_at": fresh,
                "conversation_id_str": "2092316228497063961",
                "user": {"screen_name": "thsottiaux"},
                "full_text": "本人新增的额度说明 https://t.co/example",
                "is_quote_status": True,
                "quoted_status": {
                    "id_str": "2092000000000000000",
                    "created_at": fresh,
                    "conversation_id_str": "2092000000000000000",
                    "user": {"screen_name": "someone_else"},
                },
            },
            {
                "id_str": "2092316228497063962",
                "created_at": fresh,
                "conversation_id_str": "2092316228497063962",
                "user": {"screen_name": "thsottiaux"},
                "full_text": "RT @someone_else: quoted body",
                "retweeted_status": {
                    "id_str": "2092000000000000001",
                    "created_at": fresh,
                    "conversation_id_str": "2092000000000000001",
                    "user": {"screen_name": "someone_else"},
                },
            },
        ]
    }
    markup = '<script type="application/json">' + json.dumps(payload) + "</script>"
    assert _syndication_candidates(
        markup, window_start=NOW - 3600, now=NOW, max_count=40
    ) == (
        {
            "guid": "2092316228497063958",
            "published_at": NOW - 1800,
            "reply_to_guid": "2092256496063033418",
        },
        {
            "guid": "2092316228497063961",
            "published_at": NOW - 1800,
            "reply_to_guid": "",
        },
    )


def test_syndication_excludes_outer_wrapper_retweet_marker() -> None:
    fresh = datetime.fromtimestamp(NOW - 1800, timezone.utc).isoformat()
    status_id = "2092316228497063964"
    payload = {
        "timeline": [
            {
                "rest_id": status_id,
                "legacy": {
                    "id_str": status_id,
                    "created_at": fresh,
                    "conversation_id_str": status_id,
                    "user": {"screen_name": "thsottiaux"},
                    "full_text": "ordinary wrapper text",
                },
                "retweeted_status_result": {
                    "result": {"rest_id": "2092000000000000003"}
                },
            }
        ]
    }
    markup = '<script type="application/json">' + json.dumps(payload) + "</script>"
    assert _syndication_candidates(
        markup, window_start=NOW - 3600, now=NOW, max_count=40
    ) == ()


def test_syndication_react_timeline_keeps_own_quote_not_nested_tweet() -> None:
    fresh = datetime.fromtimestamp(NOW - 1800, timezone.utc).isoformat()
    outer_id = "2092316228497063963"
    nested_id = "2092000000000000002"
    markup = (
        '<!doctype html><html><body><script>'
        f'$R[1]={{__typename:"TimelineTimelineItem",itemContent:{{itemType:"TimelineTweet",'
        f'tweet_results:{{result:{{__ref:"TweetResults:{outer_id}"}}}}}}}};'
        f'$R[2]={{__typename:"Tweet",rest_id:"{outer_id}",legacy:{{'
        f'created_at:"{fresh}",conversation_id_str:"{outer_id}",'
        'screen_name:"thsottiaux",is_quote_status:true,'
        f'quoted_status_result:{{result:{{__ref:"TweetResults:{nested_id}"}}}}}}}};'
        f'$R[3]={{__typename:"Tweet",rest_id:"{nested_id}",legacy:{{'
        f'created_at:"{fresh}",conversation_id_str:"{nested_id}",'
        'screen_name:"thsottiaux"}}};'
        '</script></body></html>'
    )
    assert _syndication_candidates(
        markup, window_start=NOW - 3600, now=NOW, max_count=40
    ) == (
        {
            "guid": outer_id,
            "published_at": NOW - 1800,
            "reply_to_guid": "",
        },
    )


def test_syndication_react_uses_only_direct_timeline_tweet_ref() -> None:
    fresh = datetime.fromtimestamp(NOW - 1800, timezone.utc).isoformat()
    outer_id = "2092316228497063965"
    quoted_id = "2092000000000000004"
    markup = (
        '<!doctype html><html><body><script>'
        '$R[1]={__typename:"TimelineTimelineItem",itemContent:{itemType:"TimelineTweet",'
        'tweet_results:{result:{__ref:"TweetResults:' + outer_id + '"}},'
        'quoted_status_result:{result:{__ref:"TweetResults:' + quoted_id + '"}}}}};'
        f'$R[2]={{__typename:"Tweet",rest_id:"{outer_id}",legacy:{{'
        f'created_at:"{fresh}",conversation_id_str:"{outer_id}",'
        'screen_name:"thsottiaux"}}};'
        f'$R[3]={{__typename:"Tweet",rest_id:"{quoted_id}",legacy:{{'
        f'created_at:"{fresh}",conversation_id_str:"{quoted_id}",'
        'screen_name:"thsottiaux"}}};'
        '</script></body></html>'
    )
    assert _syndication_candidates(
        markup, window_start=NOW - 3600, now=NOW, max_count=40
    ) == (
        {
            "guid": outer_id,
            "published_at": NOW - 1800,
            "reply_to_guid": "",
        },
    )


def test_syndication_react_tweet_without_timeline_ref_fails_closed() -> None:
    fresh = datetime.fromtimestamp(NOW - 1800, timezone.utc).isoformat()
    status_id = "2092316228497063966"
    markup = (
        '<html><script>'
        f'$R[1]={{__typename:"Tweet",rest_id:"{status_id}",legacy:{{'
        f'created_at:"{fresh}",conversation_id_str:"{status_id}",'
        'screen_name:"thsottiaux"}}};'
        '</script></html>'
    )
    assert _syndication_candidates(
        markup, window_start=NOW - 3600, now=NOW, max_count=40
    ) == ()


_DOCS_SECTION_ORDER = (
    "What are the usage limits for my plan?",
    "ChatGPT Voice in Desktop",
    "What happens when you hit usage limits?",
    "How does image generation count toward usage limits?",
    "Where can I see my current usage limits?",
    "What are tokens and credits?",
    "What counts as Code Review usage?",
    "What can I do to make my usage limits last longer?",
)
_DOCS_USAGE_HEADER = (
    "Model",
    "Plus",
    "Pro 5x",
    "Pro 20x",
    "Business",
    "API Key",
)
_DOCS_CREDIT_HEADER = (
    "Credits per 1M tokens",
    "Input Tokens",
    "Cached input tokens",
    "Output Tokens",
)


def _docs_table(headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...]) -> str:
    header_html = "<tr>" + "".join(f"<th>{cell}</th>" for cell in headers) + "</tr>"
    rows_html = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead>{header_html}</thead><tbody>{rows_html}</tbody></table>"


def _docs_html(
    *,
    changed_line: str = "",
    nav_value: str = "noise-a",
    script_value: str = "script-a",
    article_class: str = "prose build-a",
    article_data: str = "article-a",
    article_id: str = "mainContent",
    static_noise: str = "plan-v1",
    omitted_heading: str | None = None,
    duplicate_heading: str | None = None,
    section_order: tuple[str, ...] = _DOCS_SECTION_ORDER,
    usage_header: tuple[str, ...] = _DOCS_USAGE_HEADER,
    credit_header: tuple[str, ...] = _DOCS_CREDIT_HEADER,
    usage_rows: tuple[tuple[str, ...], ...] = (
        ("GPT-5.6 Sol", "40-80", "200-400", "400-800", "80-160", "API"),
    ),
    credit_rows: tuple[tuple[str, ...], ...] = (
        ("GPT-5.6 Sol", "1.00", "0.10", "5.00"),
    ),
    include_usage_anchor: bool = True,
    include_credits_anchor: bool = True,
    duplicate_usage_anchor: bool = False,
    duplicate_credits_anchor: bool = False,
    extra_article: bool = False,
    close_article: bool = True,
) -> str:
    bodies = {
        "What are the usage limits for my plan?": (
            "Codex usage limits depend on the model and plan. Included usage "
            "is measured in a five-hour window."
        ),
        "ChatGPT Voice in Desktop": (
            "ChatGPT Voice in Desktop has a separate allowance, and Codex "
            "usage budget details are shown with the plan limits."
        ),
        "What happens when you hit usage limits?": (
            "After reaching Codex usage limits, eligible users can purchase "
            "additional credits and continue an active turn."
        ),
        "How does image generation count toward usage limits?": (
            "Image generation counts toward the applicable Codex and ChatGPT "
            "usage limits for the selected plan."
        ),
        "Where can I see my current usage limits?": (
            "Current Codex usage limits are shown in the usage dashboard and "
            "the Codex CLI status command."
        ),
        "What are tokens and credits?": (
            "Credits track Codex token usage after included usage limits are "
            "reached and are listed in the rate card."
        ),
        "What counts as Code Review usage?": (
            "Codex reviews through GitHub count toward Code Review usage limits."
        ),
        "What can I do to make my usage limits last longer?": (
            "Use smaller Codex models and narrower tasks to make usage limits "
            "last longer."
        ),
    }
    parts = [
        "<!doctype html>",
        "<html><head><title>Pricing</title><meta data-build=\"head-a\"></head>",
        "<body>",
        f'<nav data-build="{nav_value}">dynamic navigation {nav_value}</nav>',
        f'<main data-build="main-a"><article id="{article_id}" class="{article_class}" data-build="{article_data}">',
        '<p class="not-prose"><strong>ChatGPT Work and Codex share usage.</strong> '
        "ChatGPT Work usage inside ChatGPT uses the same pricing, credits, "
        "and usage limits as Codex.</p>",
        f'<div id="content-switcher-codex-pricing-plans" data-build="{static_noise}">'
        f"Plan card {static_noise}; $20/month; dynamic plan props"
        "<script>window.planNoise='ignored';</script></div>",
        f'<script data-hydration="{script_value}">window.dynamic="{script_value}";</script>',
        f'<style data-style="{script_value}">.build-{script_value} {{ color: red; }}</style>',
        "<h2>Pricing options</h2>",
        f"<h2>Invite friends and coworkers</h2><p>Referral promo {static_noise}</p>",
        "<h2>Frequently asked questions</h2>",
    ]
    for index, title in enumerate(section_order):
        if title not in bodies or title == omitted_heading:
            continue
        parts.append(
            f'<h3 id="generated-heading-{index}" class="heading-build-{script_value}">{title}</h3>'
        )
        if title == duplicate_heading:
            parts.append(
                f'<h3 id="generated-duplicate-{index}">{title}</h3>'
            )
        parts.append(f"<section><p>{bodies[title]}</p>")
        if changed_line and title == "What happens when you hit usage limits?":
            parts.append(f"<p>{changed_line}</p>")
        if title == "What are the usage limits for my plan?":
            if include_usage_anchor:
                parts.append('<div id="usage-limits" data-anchor-build="a"></div>')
                if duplicate_usage_anchor:
                    parts.append('<span id="usage-limits"></span>')
            parts.append(_docs_table(_DOCS_USAGE_HEADER if usage_header is None else usage_header, usage_rows))
        elif title == "What are tokens and credits?":
            if include_credits_anchor:
                parts.append('<div id="credits-overview" data-anchor-build="a"></div>')
                if duplicate_credits_anchor:
                    parts.append('<span id="credits-overview"></span>')
            parts.append(_docs_table(_DOCS_CREDIT_HEADER if credit_header is None else credit_header, credit_rows))
        parts.append("</section>")
    parts.extend(
        (
            "<h2>Feature availability</h2>",
            f'<div class="hidden xl:block">feature matrix {static_noise}</div>',
            f'<astro-island data-props="{script_value}"><script>matrix({script_value})</script></astro-island>',
        )
    )
    if close_article:
        parts.append("</article>")
    parts.extend(
        (
            "</main>",
            f'<footer data-build="{nav_value}">footer {nav_value}</footer>',
        )
    )
    if extra_article:
        parts.append('<article id="mainContent"></article>')
    parts.extend(("</body>", "</html>"))
    return "".join(parts)


def test_official_docs_html_extractor_uses_only_stable_quota_body() -> None:
    first = _docs_html()
    noisy = _docs_html(
        nav_value="noise-b",
        script_value="script-b",
        article_class="prose completely-rebuilt-class",
        article_data="article-b",
        static_noise="plan-v2",
    )
    changed = _docs_html(
        changed_line="Codex weekly usage limits will increase for eligible plans."
    )
    normalized = _extract_codex_quota_document(first)
    assert normalized == _extract_codex_quota_document(noisy)
    assert normalized != _extract_codex_quota_document(changed)
    assert "dynamic navigation" not in normalized
    assert "dynamic" not in normalized
    assert all(title in normalized for title in _DOCS_SECTION_ORDER)


def test_official_docs_html_extractor_rejects_nested_duplicate_target_article() -> None:
    source = _docs_html()
    nested = source.replace(
        "</article>",
        '<article id="mainContent"></article></article>',
        1,
    )
    with pytest.raises(ResetAlertSourceError, match="schema_invalid"):
        _extract_codex_quota_document(nested)


@pytest.mark.parametrize(
    ("case", "kwargs"),
    [
        ("missing core heading", {"omitted_heading": "What are the usage limits for my plan?"}),
        ("duplicate heading", {"duplicate_heading": "What are tokens and credits?"}),
        ("invalid usage header", {"usage_header": ("Noise", "Prices", "Other")}),
        ("invalid credit header", {"credit_header": ("Noise", "Prices", "Other")}),
        ("duplicate usage anchor", {"duplicate_usage_anchor": True}),
        ("duplicate credits anchor", {"duplicate_credits_anchor": True}),
        ("multiple main articles", {"extra_article": True}),
        ("wrong article id", {"article_id": "not-mainContent"}),
        ("unclosed article", {"close_article": False}),
        ("usage table has no data rows", {"usage_rows": ()}),
        ("credit table has no data rows", {"credit_rows": ()}),
    ],
)
def test_official_docs_html_structure_anomalies_fail_closed(case: str, kwargs: dict[str, object]) -> None:
    del case
    with pytest.raises(ResetAlertSourceError, match="openai_codex_docs_schema_invalid"):
        _extract_codex_quota_document(_docs_html(**kwargs))


def test_official_docs_first_success_and_same_hash_emit_no_signal() -> None:
    url = OPENAI_CODEX_PRICING_URL
    scanner = ResetAlertScanner(
        ResetAlertConfig(),
        client=_FixtureClient(
            {url: (_docs_html(), {"etag": "v1", "last_modified": "stamp"})}
        ),
    )
    baseline = scanner._openai_codex_docs(now=NOW, previous_payload_hash="")
    assert baseline.success is True
    assert baseline.signals == ()
    same = scanner._openai_codex_docs(
        now=NOW + 3600, previous_payload_hash=baseline.payload_hash
    )
    assert same.signals == ()


def test_official_docs_changed_hash_is_b1_only_with_forecast() -> None:
    url = OPENAI_CODEX_PRICING_URL
    scanner = ResetAlertScanner(
        ResetAlertConfig(),
        client=_FixtureClient(
            {
                url: _docs_html(
                    changed_line=(
                        "Codex weekly usage limits will increase within 4 hours."
                    )
                )
            }
        ),
    )
    changed = scanner._openai_codex_docs(
        now=NOW, previous_payload_hash="html-v2:" + "0" * 64
    )
    assert len(changed.signals) == 1
    docs_signal = changed.signals[0]
    assert docs_signal.item_id.startswith("pricing-")
    # 文档变化只是 B1 的独立佐证，绝不能单独升级为 A/B2/B3。
    assert classify_signals(
        (docs_signal,), forecast_threshold=70, now=NOW
    ) == ()
    forecast = _signal(
        "forecast score 70",
        source="forecast",
        item="forecast-new",
        official=False,
        kind="forecast",
        metadata={"score": 70, "evidence_ids": ()},
    )
    decisions = classify_signals(
        (forecast, docs_signal), forecast_threshold=70, now=NOW
    )
    assert len(decisions) == 1
    assert decisions[0].level == "B"
    assert decisions[0].source_ids == ("forecast", "openai_codex_docs")


def test_scanner_source_failures_are_independent() -> None:
    client = _FixtureClient(
        {
            "https://www.willcodexquotareset.com/api/forecast": _forecast_payload(),
            "https://status.openai.com/api/v2/incidents.json": {"incidents": []},
            OPENAI_CODEX_PRICING_URL: ResetAlertSourceError(
                "source_http_403"
            ),
            "https://syndication.twitter.com/srv/timeline-profile/screen-name/thsottiaux": ResetAlertSourceError(
                "source_http_429"
            ),
        }
    )
    results = ResetAlertScanner(ResetAlertConfig(), client=client).scan(
        now=NOW,
        window_starts={
            "forecast": NOW - 3600,
            "openai_status": NOW - 3600,
            "openai_codex_docs": NOW - 3600,
            "x_thsottiaux": NOW - 3600,
        },
        previous_payload_hashes={"openai_codex_docs": "a" * 64},
    )
    by_source = {item.source_id: item for item in results}
    assert by_source["forecast"].success is True
    assert by_source["openai_status"].success is True
    assert by_source["openai_codex_docs"].error_code == "source_http_403"
    assert by_source["x_thsottiaux"].error_code == "source_http_429"


@pytest.mark.parametrize(
    ("value", "expected_slot", "expected_next"),
    [
        ("2026-08-31T07:59:00+08:00", None, "2026-08-31T08:00:00+08:00"),
        ("2026-08-31T08:00:00+08:00", "2026-08-31T08:00:00+08:00", "2026-08-31T09:00:00+08:00"),
        ("2026-08-31T22:59:00+08:00", "2026-08-31T22:00:00+08:00", "2026-08-31T23:00:00+08:00"),
        ("2026-08-31T23:00:00+08:00", "2026-08-31T23:00:00+08:00", "2026-09-01T08:00:00+08:00"),
        ("2026-09-01T00:00:00+08:00", None, "2026-09-01T08:00:00+08:00"),
    ],
)
def test_schedule_uses_fixed_beijing_hours(value: str, expected_slot: str | None, expected_next: str) -> None:
    now = _ts(value)
    assert schedule_slot(now) == (None if expected_slot is None else _ts(expected_slot))
    assert next_check_at(now) == _ts(expected_next)


def test_0800_window_is_midnight_then_uses_source_cursor() -> None:
    at_eight = _ts("2026-08-31T08:45:00+08:00")
    prior = _ts("2026-08-30T23:00:00+08:00")
    assert scan_window_start(at_eight, prior) == at_eight - 86400
    at_nine = _ts("2026-08-31T09:00:00+08:00")
    cursor = _ts("2026-08-31T08:00:30+08:00")
    assert scan_window_start(at_nine, cursor) == at_nine - 86400


class _Scanner:
    def __init__(self, batches: list[tuple[SourceResult, ...]]):
        self.batches = list(batches)
        self.config = ResetAlertConfig()
        self.windows: list[dict[str, int]] = []

    def scan(
        self,
        *,
        now: int,
        window_starts: dict[str, int],
        previous_payload_hashes: dict[str, str] | None = None,
    ):
        del now, previous_payload_hashes
        self.windows.append(dict(window_starts))
        return self.batches.pop(0)


def _results(*, x_success: bool, score: int = 50, status_text: str = "") -> tuple[SourceResult, ...]:
    forecast = SourceResult(
        "forecast",
        True,
        (_signal("forecast", source="forecast", item="forecast", official=False, kind="forecast", metadata={"score": score}),),
    )
    status_signals = (_signal(status_text),) if status_text else ()
    return (
        forecast,
        SourceResult("openai_status", True, status_signals),
        SourceResult("openai_codex_docs", True, payload_hash="docs-v1"),
        SourceResult("x_thsottiaux", x_success, error_code="x_unavailable" if not x_success else ""),
    )


def _worker(tmp_path: Path, scanner: _Scanner, sent: list[str] | None = None) -> tuple[StateStore, ResetAlertWorker]:
    store = StateStore(tmp_path / "state.sqlite")
    output = sent if sent is not None else []
    worker = ResetAlertWorker(
        store=store,
        config=ResetAlertConfig(),
        send_text=lambda text, _key: output.append(text) or "om_alert",
        is_online=lambda: True,
        stop_event=threading.Event(),
        scanner=scanner,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    return store, worker


def test_per_source_bootstrap_suppresses_recovery_history_and_score50(tmp_path: Path) -> None:
    scanner = _Scanner([
        _results(x_success=False, score=50, status_text="Codex usage limits will reset within 2 hours"),
        _results(x_success=True, score=50),
    ])
    store, worker = _worker(tmp_path, scanner)
    try:
        assert worker.scan_due(now=NOW) is True
        first = store.reset_alert_status()
        baselines = {item["source_id"]: item["baseline_completed_at"] for item in first["sources"]}
        assert baselines["forecast"] is not None
        assert baselines["openai_status"] is not None
        assert baselines["x_thsottiaux"] is None
        assert len(store.latest_reset_alerts()) == 1

        later = NOW + 3600
        assert worker.scan_due(now=later) is True
        assert len(store.latest_reset_alerts()) == 1
        final = store.reset_alert_status()
        assert all(item["baseline_completed_at"] is not None for item in final["sources"])
    finally:
        store.close()


def test_empty_x_baseline_rechecks_midnight_and_recovers_live_a_once(tmp_path: Path) -> None:
    run_at = _ts("2026-09-04T09:00:00+08:00")
    published_at = _ts("2026-09-04T07:12:09+08:00")
    status_id = "2095651088502591861"
    text = (
        "We will give one banked reset for every day you don't have access to Astra "
        "on your paid ChatGPT plan, starting today. First one will land in ~ 3 hours."
    )
    signal = _signal(
        text,
        source="x_thsottiaux",
        item=status_id,
        kind="x_post",
        published_at=published_at,
        metadata={"author_verified": True, "discovery_source": "forecast"},
    )
    results = (
        SourceResult("forecast", True),
        SourceResult("openai_status", True),
        SourceResult("openai_codex_docs", True),
        SourceResult("x_thsottiaux", True, (signal,)),
    )
    scanner = _Scanner([results, results])
    store, worker = _worker(tmp_path, scanner)
    try:
        seed_at = _ts("2026-09-04T08:00:00+08:00")
        for source_id in EXPECTED_SOURCE_IDS:
            store.upsert_reset_alert_source(
                source_id,
                cursor={},
                success=True,
                last_item_at=None,
                payload_hash=None,
                error_code=None,
                mark_baseline=True,
                now=seed_at,
            )
        assert worker.scan_due(now=run_at) is True
        assert scanner.windows[0]["x_thsottiaux"] == run_at - 86400
        assert len(store.latest_reset_alerts()) == 1
        assert worker.scan_due(now=run_at + 3600) is True
        assert len(store.latest_reset_alerts()) == 1
        status = store.reset_alert_status()
        x_status = next(item for item in status["sources"] if item["source_id"] == "x_thsottiaux")
        assert x_status["last_item_at"] == published_at
    finally:
        store.close()


def test_empty_x_baseline_does_not_recover_expired_a(tmp_path: Path) -> None:
    run_at = _ts("2026-09-04T09:00:00+08:00")
    signal = _signal(
        "We will give one banked reset for every day you don't have access to Astra "
        "on your paid ChatGPT plan, starting today. First one will land in ~ 3 hours.",
        source="x_thsottiaux",
        item="2095651088502591861",
        kind="x_post",
        published_at=_ts("2026-09-04T05:00:00+08:00"),
        metadata={"author_verified": True, "discovery_source": "forecast"},
    )
    scanner = _Scanner(
        [
            (
                SourceResult("forecast", True),
                SourceResult("openai_status", True),
                SourceResult("openai_codex_docs", True),
                SourceResult("x_thsottiaux", True, (signal,)),
            )
        ]
    )
    store, worker = _worker(tmp_path, scanner)
    try:
        seed_at = _ts("2026-09-04T08:00:00+08:00")
        for source_id in EXPECTED_SOURCE_IDS:
            store.upsert_reset_alert_source(
                source_id,
                cursor={},
                success=True,
                last_item_at=None,
                payload_hash=None,
                error_code=None,
                mark_baseline=True,
                now=seed_at,
            )
        assert worker.scan_due(now=run_at) is True
        assert store.latest_reset_alerts() == []
    finally:
        store.close()


def _reserve(store: StateStore, *, now: int = NOW, expires: int | None = None) -> str:
    key = "reset-alert:test"
    store.reserve_reset_alert_event(
        event_key=key,
        level="A",
        evidence="合成官方证据",
        window_text="预计 2026-08-31 18:00 前（北京时间）",
        advice="集中消耗额度",
        source_ids=("openai_status",),
        fingerprint="f" * 64,
        expires_at=expires if expires is not None else now + 3600,
        now=now,
    )
    return key


def test_alert_message_is_exactly_four_lines_and_latest_has_event_id(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    try:
        _reserve(store)
        message = store._connection.execute(
            "SELECT message_text FROM reset_alert_deliveries"
        ).fetchone()[0]
        assert len(message.splitlines()) == 4
        assert message.splitlines()[0] == "【Codex 重置预警｜A 级】"
    finally:
        store.close()


def test_release_submitted_requires_explicit_proof(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    try:
        _reserve(store)
        delivery = store.claim_reset_alert_delivery(now=NOW)
        assert delivery is not None
        assert store.mark_reset_alert_submitted(delivery.delivery_id, now=NOW)
        assert not store.release_reset_alert_delivery(
            delivery.delivery_id,
            next_attempt_at=NOW + 30,
            error_code="unsafe",
            now=NOW,
        )
        assert store.latest_reset_alerts()[0]["delivery"]["state"] == "claimed"
        assert store.release_reset_alert_delivery(
            delivery.delivery_id,
            next_attempt_at=NOW + 30,
            error_code="proved_not_submitted",
            allow_submitted=True,
            now=NOW,
        )
        assert store.latest_reset_alerts()[0]["delivery"]["state"] == "pending"
    finally:
        store.close()


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (FeishuSendNotSubmittedError("offline before write"), "pending"),
        (FeishuSendRejectedError(code="rejected", raw_code=400, retryable=True), "retrying"),
        (FeishuSendRejectedError(code="rejected", raw_code=400, retryable=False), "rejected"),
        (MessageChannelOfflineError("generic unknown"), "uncertain"),
        (FeishuSendError("result unknown"), "uncertain"),
    ],
)
def test_worker_delivery_submit_boundaries(tmp_path: Path, error: BaseException, state: str) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    _reserve(store)
    worker = ResetAlertWorker(
        store=store,
        config=ResetAlertConfig(),
        send_text=lambda _text, _key: (_ for _ in ()).throw(error),
        is_online=lambda: True,
        stop_event=threading.Event(),
        scanner=_Scanner([]),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    try:
        assert worker.deliver_one(now=NOW) is False
        assert store.latest_reset_alerts()[0]["delivery"]["state"] == state
    finally:
        store.close()


def test_worker_config_reload_updates_owned_http_timeout(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    initial = ResetAlertConfig(request_timeout_seconds=4.0)
    worker = ResetAlertWorker(
        store=store,
        config=initial,
        send_text=lambda _text, _key: "om",
        is_online=lambda: True,
        stop_event=threading.Event(),
        clock=lambda: NOW,
    )
    try:
        original_scanner = worker.scanner
        updated = replace(initial, request_timeout_seconds=9.0)
        worker.update_config(updated)
        assert worker.scanner is not original_scanner
        assert worker.scanner.client.timeout_seconds == 9.0

        injected = _Scanner([])
        injected_worker = ResetAlertWorker(
            store=store,
            config=initial,
            send_text=lambda _text, _key: "om",
            is_online=lambda: True,
            stop_event=threading.Event(),
            scanner=injected,  # type: ignore[arg-type]
            clock=lambda: NOW,
        )
        injected_worker.update_config(updated)
        assert injected_worker.scanner is injected
        assert injected.config == updated
    finally:
        store.close()


def test_expired_delivery_is_never_sent(tmp_path: Path) -> None:
    sent: list[str] = []
    store = StateStore(tmp_path / "state.sqlite")
    _reserve(store, now=NOW - 100, expires=NOW - 1)
    worker = ResetAlertWorker(
        store=store,
        config=ResetAlertConfig(),
        send_text=lambda text, _key: sent.append(text) or "om",
        is_online=lambda: True,
        stop_event=threading.Event(),
        scanner=_Scanner([]),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    try:
        assert worker.deliver_one(now=NOW) is False
        assert sent == []
        assert store.latest_reset_alerts()[0]["delivery"]["state"] == "expired"
    finally:
        store.close()


def test_reset_alert_read_only_cli_is_json_and_does_not_write(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    path = tmp_path / "state.sqlite"
    store = StateStore(path)
    _reserve(store)
    store.close()
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    config = SimpleNamespace(
        service=SimpleNamespace(database=path),
        reset_alert=ResetAlertConfig(),
    )
    monkeypatch.setattr(cli, "_config", lambda _args, ready=False: config)
    assert cli._reset_alert_status(SimpleNamespace(json=True)) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["schema_version"] == 1
    assert status["worker_running"] is False
    assert cli._reset_alert_latest(SimpleNamespace(json=True, limit=10)) == 0
    latest = json.loads(capsys.readouterr().out)
    assert latest["items"][0]["event_id"] == latest["items"][0]["event_key"]
    assert latest["items"][0]["delivery"] == {
        "delivery_id": latest["items"][0]["delivery"]["delivery_id"],
        "state": "pending",
        "attempt_count": 0,
        "next_attempt_at": NOW,
        "last_error_code": None,
        "terminal": False,
        "consumable": False,
        "consumer_state": "wait",
    }
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize(
    ("state", "terminal", "consumable", "consumer_state"),
    [
        ("pending", False, False, "wait"),
        ("retrying", False, False, "wait"),
        ("claimed", False, False, "wait"),
        ("delivered", True, True, "delivered"),
        ("rejected", True, True, "failed"),
        ("expired", True, True, "failed"),
        ("uncertain", True, True, "needs_attention"),
    ],
)
def test_reset_alert_latest_delivery_contract_is_machine_readable(
    state: str,
    terminal: bool,
    consumable: bool,
    consumer_state: str,
) -> None:
    delivery = cli._reset_alert_delivery_contract({"state": state})
    assert delivery == {
        "state": state,
        "terminal": terminal,
        "consumable": consumable,
        "consumer_state": consumer_state,
    }


def test_progress_service_starts_one_worker_and_reload_updates_it(
    monkeypatch, tmp_path: Path
) -> None:
    started = threading.Event()
    instances: list[object] = []

    class DummyWorker:
        def __init__(self, **kwargs):
            self.stop_event = kwargs["stop_event"]
            self.updates: list[ResetAlertConfig] = []
            instances.append(self)

        def update_config(self, config: ResetAlertConfig) -> None:
            self.updates.append(config)

        def run(self) -> None:
            started.set()
            self.stop_event.wait()

    class Channel:
        def is_online(self) -> bool:
            return True

    monkeypatch.setattr(service_module, "ResetAlertWorker", DummyWorker)
    progress = service_module.ProgressService(tmp_path / "config.yaml")
    progress.store = object()  # type: ignore[assignment]
    progress.channel = Channel()  # type: ignore[assignment]
    progress.config = SimpleNamespace(
        messaging=SimpleNamespace(backend="feishu"),
        reset_alert=ResetAlertConfig(),
    )  # type: ignore[assignment]
    progress._start_reset_alert_worker()
    assert started.wait(2)
    first_thread = progress.reset_alert_thread
    progress._start_reset_alert_worker()
    assert progress.reset_alert_thread is first_thread
    assert len(instances) == 1
    assert len(instances[0].updates) == 1  # type: ignore[attr-defined]
    progress.stop_event.set()
    assert first_thread is not None
    first_thread.join(2)
    assert not first_thread.is_alive()
