"""Regression of September 2026 missed announcements, entirely isolated."""
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from progress_wx import cli
from progress_wx.config import ResetAlertConfig
from progress_wx.reset_alert import (
    EXPECTED_SOURCE_IDS, FORECAST_URL, ResetAlertScanner, ResetAlertSourceError,
    SourceResult, classify_signals, _extract_codex_quota_document,
)
from test_reset_alert import (
    NOW, _signal, _ts, _docs_html, _DOCS_SECTION_ORDER, _DOCS_USAGE_HEADER,
    _Scanner, _results, _worker, _forecast_payload, _x_scan_scanner, _oembed_payload,
)


@pytest.mark.parametrize("text", [
    "If you have a paid ChatGPT plan, your banked reset is now available.",
    "Codex usage limits have been reset. We will keep monitoring.",
    "Your Codex quota was reset.",
    "Codex quota reset is complete.",
    "We have issued a banked reset for Plus, Pro and Business users.",
])
def test_completed_announcements_with_conditions(text):
    decision, = classify_signals((_signal(text),), forecast_threshold=70, now=NOW)
    assert decision.phase == "announced_available"
    assert "个人账户" in decision.window_text
    assert "集中消耗" not in decision.advice


@pytest.mark.parametrize("text", [
    "Codex banked resets are NOT available.",
    "Codex quota has not been reset.",
    "If the Codex quota reset is complete, we will notify you.",
    "Maybe we could reset Codex usage limits within 3 hours.",
    "Codex usage limits are documented. Password reset is complete.",
])
def test_non_announcements_still_rejected(text):
    assert classify_signals((_signal(text),), forecast_threshold=70, now=NOW) == ()


@pytest.mark.parametrize(("post_id", "published", "text"), [
    ("2095979536043401428", "2026-09-04T20:57:17+00:00",
     "Some Plus and Business users won't yet get access to Astra today, we've got you covered with a banked reset. Lands by end of day and if you create your account by 8pm PT then you'll get it too."),
    ("2096035437299237298", "2026-09-05T00:39:25+00:00",
     "We will do the full banked reset today too for all Plus, Pro and Business users. Lands end of day."),
])
def test_real_missed_public_posts_are_b_then_expire(post_id, published, text):
    published = _ts(published)
    signal = _signal(text, source="x_thsottiaux", item=post_id,
                     published_at=published, kind="x_post", metadata={"author_verified": True})
    early, = classify_signals((signal,), forecast_threshold=70, now=published+7200)
    later, = classify_signals((signal,), forecast_threshold=70, now=published+10800)
    assert early.level == "B" and early.phase == "upcoming"
    assert early.event_key == later.event_key
    assert early.expires_at == later.expires_at == published+86400
    assert classify_signals((signal,), forecast_threshold=70, now=published+86400) == ()


def test_b_forecast_fingerprint_does_not_change_each_hour():
    official = _signal("Codex usage limits incident is under investigation")
    forecast = _signal("score", source="forecast", official=False, metadata={"score": 80})
    a, = classify_signals((official, forecast), forecast_threshold=70, now=NOW)
    b, = classify_signals((official, replace(forecast, item_id="next-hour", published_at=NOW+3500)),
                         forecast_threshold=70, now=NOW+3600)
    assert a.event_key == b.event_key and a.expires_at == b.expires_at


def test_real_pricing_plan_rename_and_peripheral_changes():
    # Minimal equivalent of official 2026-09-08 HTML: Business -> Standard Business.
    headers = tuple("Standard Business" if x == "Business" else x for x in _DOCS_USAGE_HEADER)
    markup = _docs_html(usage_header=headers, omitted_heading="ChatGPT Voice in Desktop",
                        section_order=tuple(reversed(_DOCS_SECTION_ORDER)),
                        include_usage_anchor=False, include_credits_anchor=False)
    assert "Standard Business" in _extract_codex_quota_document(markup)
    for title in ("What are the usage limits for my plan?", "What are tokens and credits?"):
        with pytest.raises(ResetAlertSourceError):
            _extract_codex_quota_document(_docs_html(omitted_heading=title))


def test_partial_verification_retry_cache_and_429_stop():
    ids = ["2095979536043401428", "2096035437299237298", "2095651088502591861"]
    scanner, client = _x_scan_scanner(forecast_payload=_forecast_payload(40),
        syndication=ResetAlertSourceError("source_http_429"), oembeds={
            ids[0]: _oembed_payload(ids[0], "Codex usage limits will reset within 3 hours"),
            ids[1]: ResetAlertSourceError("source_http_429"),
            ids[2]: _oembed_payload(ids[2], "Codex usage limits will reset within 3 hours"),
        })
    candidates = tuple({"guid": x, "published_at": NOW-60} for x in ids)
    result = scanner._x(candidates)
    assert not result.success and result.error_code == "source_http_429"
    assert client.oembed_calls == ids[:2]
    scanner.verified_cache = {x.item_id: x for x in result.signals}
    scanner._x(candidates)
    assert client.oembed_calls == ids[:2]  # Immediate retry is now correctly cooled.
    scanner._scan_now = scanner.endpoint_states['oembed']['cooldown_until']
    scanner._x(candidates)
    assert client.oembed_calls == [ids[0], ids[1], ids[1]]


def test_degraded_forecast_keeps_discovery_without_forecast_score():
    payload = _forecast_payload(80)
    payload["sourceErrors"] = {"status": "unavailable"}
    payload["tiboPosts"] = [{"guid": "2095979536043401428",
        "pubDate": datetime.fromtimestamp(NOW-60, timezone.utc).isoformat()}]
    scanner, _ = _x_scan_scanner(forecast_payload=payload, syndication="", oembeds={})
    result = scanner._forecast(now=NOW, window_start=NOW-86400)
    assert not result.success and not result.signals and len(result.candidates) == 1


def test_isolated_scan_persistence_mocked_feishu_and_cli(tmp_path, monkeypatch, capsys):
    post_id = "2095979536043401428"
    payload = _forecast_payload(40)
    payload["tiboPosts"] = [{"guid": post_id,
        "pubDate": datetime.fromtimestamp(NOW-60, timezone.utc).isoformat()}]
    scanner, client = _x_scan_scanner(forecast_payload=payload,
        syndication=ResetAlertSourceError("source_http_429"),
        oembeds={post_id: _oembed_payload(post_id, "Plus and Pro users get a banked reset today. Lands end of day.")})
    sent = []
    store, worker = _worker(tmp_path, scanner, sent)
    try:
        assert worker.scan_due(now=NOW)
        item, = store.latest_reset_alerts()
        assert item["delivery"]["state"] == "pending"
        assert cli._reset_alert_notification_contract(item, available=True, now=NOW)["notification_eligible"]
        assert worker.deliver_one(now=NOW)
        assert len(sent) == 1
        assert worker.scan_due(now=NOW+3600)
        assert len(store.latest_reset_alerts()) == 1
        assert not worker.deliver_one(now=NOW+3600)
        assert client.oembed_calls == [post_id]
        item, = store.latest_reset_alerts()
        assert item["delivery"]["state"] == "delivered"
        monkeypatch.setattr(cli, "_config", lambda *a, **k: type("C", (), {
            "service": type("S", (), {"database": tmp_path/"state.sqlite"})()})())
        monkeypatch.setattr(cli.time, "time", lambda: NOW+3600)
        assert cli._reset_alert_latest(type("Args", (), {"limit": 10})()) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["items"][0]["notification_eligible"]
        assert output["items"][0]["phase"] == "upcoming"
    finally:
        store.close()


def test_cutover_suppresses_old_completed_but_recovers_late_new_after_restart(tmp_path):
    old = _signal("Codex usage limits have been reset.")
    new = replace(old, item_id="new", published_at=NOW+1800)
    def batch(signal):
        return (*_results(x_success=True)[:1], SourceResult("openai_status", True, (signal,)),
                *_results(x_success=True)[2:])
    scanner = _Scanner([batch(old), batch(new), batch(new)])
    store, worker = _worker(tmp_path, scanner)
    try:
        assert worker.scan_due(now=NOW)
        assert store.latest_reset_alerts() == []
        # A process restart does not move the persisted rule cutover forward.
        assert worker.scan_due(now=NOW+3600)
        item, = store.latest_reset_alerts()
        assert "announced_available" in item["event_key"]
        assert worker.scan_due(now=NOW+7200)
        assert len(store.latest_reset_alerts()) == 1
    finally:
        store.close()


@pytest.mark.parametrize("state", ["pending", "retrying", "claimed", "rejected", "uncertain", "delivered"])
def test_desktop_notification_is_independent_of_delivery(state):
    item = {"event_key": "reset-alert:announced_available:abc", "level": "A",
            "expires_at": NOW+3600, "delivery": {"state": state}}
    assert cli._reset_alert_notification_contract(item, available=True, now=NOW)["notification_eligible"]
    assert not cli._reset_alert_notification_contract(item, available=True, now=NOW+3600)["notification_eligible"]


@pytest.mark.parametrize("startup", [
    _ts("2026-08-31T07:00:00+08:00"), _ts("2026-08-30T23:30:00+08:00")
])
def test_worker_activation_before_first_morning_scan(tmp_path, startup):
    morning = _ts("2026-08-31T08:00:00+08:00")
    signal = _signal("Codex usage limits have been reset.", published_at=startup+60)
    batch = (SourceResult("openai_status", True, (signal,)),)
    store, worker = _worker(tmp_path, _Scanner([batch]))
    times = iter([startup])
    worker.clock = lambda: next(times, morning)
    def send(text, key):
        worker.stop_event.set()
        return "om_fixture_only"
    worker.send_text = send
    try:
        worker.run()
        item, = store.latest_reset_alerts()
        assert item["delivery"]["state"] == "delivered"
        assert store.ensure_reset_alert_rule_epoch(now=morning+3600) == startup
    finally:
        store.close()


def test_failed_first_fetch_does_not_move_cutover(tmp_path):
    signal = _signal("Codex quota was reset.", published_at=NOW+120)
    scanner = _Scanner([(SourceResult("openai_status", False, error_code="source_http_503"),),
                        (SourceResult("openai_status", True, (signal,)),)])
    store, worker = _worker(tmp_path, scanner)
    try:
        assert worker.scan_due(now=NOW)
        assert worker.scan_due(now=NOW+3600)
        assert len(store.latest_reset_alerts()) == 1
        assert store.ensure_reset_alert_rule_epoch(now=NOW+7200) == NOW
    finally:
        store.close()


def test_hash_parser_upgrade_establishes_baseline_without_false_change():
    from test_reset_alert import _FixtureClient
    from progress_wx.reset_alert import OPENAI_CODEX_PRICING_URL
    scanner = ResetAlertScanner(ResetAlertConfig(), client=_FixtureClient({OPENAI_CODEX_PRICING_URL: _docs_html()}))
    upgraded = scanner._openai_codex_docs(now=NOW, previous_payload_hash="a"*64)
    assert upgraded.success and upgraded.payload_hash.startswith("html-v2:")
    assert not upgraded.signals
