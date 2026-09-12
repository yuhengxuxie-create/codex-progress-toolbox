import json
import threading
import urllib.error
from datetime import datetime, timezone
from email.message import Message
from email.utils import format_datetime
from types import SimpleNamespace

import pytest

from progress_wx.cli import _x_endpoint_status
from progress_wx.config import ResetAlertConfig
from progress_wx.reset_alert import (
    EXPECTED_SOURCE_IDS, X_SYNDICATION_URL, PublicJsonClient, ResetAlertScanner,
    ResetAlertSourceError, ResetAlertWorker, _rate_metadata,
)
from progress_wx.state import StateStore
from test_reset_alert import NOW, _forecast_payload, _oembed_payload, _x_scan_scanner


def headers(**values):
    message = Message()
    for key, value in values.items():
        message[key.replace('_', '-')] = value
    return message


@pytest.mark.parametrize('raw', ['-1', '1.5', '+2', 'NaN', '1, 2', 'later', ''])
def test_invalid_retry_after_is_not_retained(raw):
    assert _rate_metadata(headers(Retry_After=raw, Cookie='private', Authorization='secret')) == {}


def test_whitelist_seconds_date_duplicate_conflict_and_huge():
    future = NOW + 3 * 86400
    parsed = _rate_metadata(headers(Retry_After=format_datetime(datetime.fromtimestamp(future, timezone.utc), usegmt=True),
        X_Rate_Limit_Limit='30', X_Rate_Limit_Remaining='0', X_Rate_Limit_Reset=str(future+100),
        Cookie='do-not-store'))
    assert parsed == {'retry_after_at': future, 'rate_limit': 30, 'rate_remaining': 0, 'rate_reset_at': future+100}
    duplicate = headers(Retry_After='120')
    duplicate['Retry-After'] = '120'
    assert _rate_metadata(duplicate) == {'retry_after_seconds': 120}
    duplicate['Retry-After'] = '240'
    assert _rate_metadata(duplicate) == {}
    assert _rate_metadata(headers(Retry_After=str(10**100)))['retry_after_seconds'] == 10**100
    assert _rate_metadata(headers(Retry_After='9'*129)) == {'wait_unrepresentable': 1}


def test_actual_http_error_boundary_preserves_only_whitelisted_metadata():
    client = PublicJsonClient(1)
    def open_request(*args, **kwargs):
        raise urllib.error.HTTPError(X_SYNDICATION_URL, 429, 'limited',
            headers(Retry_After='90000', X_Rate_Limit_Remaining='0', Cookie='private'), None)
    client._opener = SimpleNamespace(open=open_request)
    with pytest.raises(ResetAlertSourceError) as caught:
        client.get_html(X_SYNDICATION_URL, allowed_host='syndication.twitter.com')
    assert str(caught.value) == 'source_http_429'
    assert caught.value.rate_metadata == {'retry_after_seconds': 90000, 'rate_remaining': 0}


def scanner_at(now=NOW):
    scanner = ResetAlertScanner(ResetAlertConfig(), client=SimpleNamespace())
    scanner._scan_now = now
    return scanner


def limited(metadata=None):
    raise ResetAlertSourceError('source_http_429', rate_metadata=metadata)


def test_backoff_progression_preserved_skip_and_success_reset(monkeypatch):
    monkeypatch.setattr('progress_wx.reset_alert.time.monotonic', lambda: 100.0)
    scanner = scanner_at()
    persisted = []
    scanner.persist_endpoint = lambda key, value: persisted.append((key, dict(value)))
    for count, seconds in enumerate([3600, 7200, 14400, 28800, 43200, 43200], start=1):
        with pytest.raises(ResetAlertSourceError, match='429'):
            scanner._endpoint_request('syndication', limited)
        state = dict(scanner.endpoint_states['syndication'])
        assert state['cooldown_until'] == scanner._scan_now + seconds
        assert state['consecutive_429'] == count
        before = len(persisted)
        with pytest.raises(ResetAlertSourceError, match='cooldown'):
            scanner._endpoint_request('syndication', lambda: pytest.fail('cooldown sent request'))
        assert len(persisted) == before and scanner.endpoint_states['syndication'] == state
        scanner._scan_now = state['cooldown_until']
    assert scanner._endpoint_request('syndication', lambda: 'ok') == 'ok'
    state = scanner.endpoint_states['syndication']
    assert state['consecutive_429'] == 0 and state['cooldown_until'] == 0
    assert state['last_success_at'] == scanner._scan_now


@pytest.mark.parametrize('metadata,wait', [
    ({'retry_after_seconds': 3*86400}, 3*86400),
    ({'retry_after_at': NOW+90000, 'rate_reset_at': NOW+120000}, 120000),
    ({'retry_after_seconds': 10**100}, 10**100),
])
def test_server_wait_is_not_capped(monkeypatch, metadata, wait):
    monkeypatch.setattr('progress_wx.reset_alert.time.monotonic', lambda: 0.0)
    scanner = scanner_at()
    with pytest.raises(ResetAlertSourceError):
        scanner._endpoint_request('syndication', lambda: limited(metadata))
    assert scanner.endpoint_states['syndication']['cooldown_until'] == NOW+wait


def test_unrepresentable_wait_never_becomes_local_short_retry(monkeypatch):
    monkeypatch.setattr('progress_wx.reset_alert.time.monotonic', lambda: 0.0)
    scanner = scanner_at()
    with pytest.raises(ResetAlertSourceError):
        scanner._endpoint_request('syndication', lambda: limited({'wait_unrepresentable': 1}))
    scanner._scan_now += 100*86400
    with pytest.raises(ResetAlertSourceError, match='cooldown'):
        scanner._endpoint_request('syndication', lambda: pytest.fail('must not send'))
    result = _x_endpoint_status({'x_endpoints': scanner.endpoint_states}, now=scanner._scan_now)
    endpoint = result['x_endpoint_states']['syndication']
    assert endpoint['retry_unrepresentable'] and endpoint['next_attempt_at'] is None


def test_endpoint_attempt_time_uses_actual_elapsed_scan_time(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('progress_wx.reset_alert.time.monotonic', lambda: clock[0])
    scanner = scanner_at()
    clock[0] += 9.0
    scanner._endpoint_request('oembed', lambda: 'ok')
    assert scanner.endpoint_states['oembed']['last_attempt_at'] == NOW+9


def test_retry_after_seconds_start_at_response_not_request(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('progress_wx.reset_alert.time.monotonic', lambda: clock[0])
    scanner = scanner_at()
    def response():
        clock[0] += 20
        limited({'retry_after_seconds': 600})
    with pytest.raises(ResetAlertSourceError): scanner._endpoint_request('syndication', response)
    endpoint = scanner.endpoint_states['syndication']
    assert endpoint['last_attempt_at'] == NOW
    assert endpoint['last_response_at'] == NOW+20
    assert endpoint['cooldown_until'] == NOW+620
    scanner._scan_now = NOW+620
    def success():
        clock[0] += 5
        return 'ok'
    scanner._endpoint_request('syndication', success)
    assert scanner.endpoint_states['syndication']['last_success_at'] == NOW+645


def test_persist_before_call_and_rate_limit_survives_reopen(tmp_path, monkeypatch):
    monkeypatch.setattr('progress_wx.reset_alert.time.monotonic', lambda: 0.0)
    path = tmp_path / 'state.sqlite'
    state = StateStore(path)
    scanner = scanner_at()
    scanner.persist_endpoint = state.update_reset_alert_endpoint
    def request():
        row = state.reset_alert_status()['sources'][0]
        assert json.loads(row['cursor_json'])['x_endpoints']['oembed']['last_attempt_at'] == NOW
        limited({'retry_after_seconds': 90000})
    try:
        with pytest.raises(ResetAlertSourceError): scanner._endpoint_request('oembed', request)
    finally: state.close()
    reopened = StateStore(path)
    try:
        cursor = json.loads(reopened.reset_alert_status()['sources'][0]['cursor_json'])
        fresh = scanner_at(NOW+3600)
        fresh.endpoint_states = cursor['x_endpoints']
        with pytest.raises(ResetAlertSourceError, match='cooldown'):
            fresh._endpoint_request('oembed', lambda: pytest.fail('restart bypassed cooldown'))
        assert fresh._endpoint_request('syndication', lambda: 'independent') == 'independent'
        assert fresh._endpoint_request('x_parent', lambda: 'independent') == 'independent'
    finally: reopened.close()


def make_scan(syndication=None, post=True):
    guid = '2095651088502591861'
    forecast = _forecast_payload(40)
    if post:
        forecast['tiboPosts'] = [{'guid': guid, 'pubDate': datetime.fromtimestamp(NOW-1800, timezone.utc).isoformat()}]
    return _x_scan_scanner(forecast_payload=forecast,
        syndication=syndication or ResetAlertSourceError('source_http_429'),
        oembeds={guid: _oembed_payload(guid, 'Codex limits will reset in 3 hours')})


def test_scan_cooling_discovery_does_not_block_fallback_oembed_or_openai():
    scanner, client = make_scan()
    scanner.endpoint_states['syndication'] = {'cooldown_until': NOW+90000,
        'last_attempt_at': NOW-100, 'last_error': 'source_http_429'}
    results = scanner.scan(now=NOW, window_starts={key: NOW-3600 for key in EXPECTED_SOURCE_IDS})
    assert all(item.success for item in results)
    result = results[-1]
    assert result.cursor['syndication_error'] == 'source_cooldown_active'
    assert result.cursor['forecast_discovery']['success']
    assert result.cursor['official_verification']['attempted']
    assert len(client.oembed_calls) == 1
    assert scanner.endpoint_states['syndication']['last_attempt_at'] == NOW-100


def test_worker_restart_cooling_skip_keeps_attempt_success_and_other_sources(tmp_path):
    state = StateStore(tmp_path / 'state.sqlite')
    try:
        endpoint = {'cooldown_until': NOW+90000, 'last_attempt_at': NOW-3600,
                    'last_success_at': NOW-7200, 'consecutive_429': 2, 'last_error': 'source_http_429'}
        state.upsert_reset_alert_source('x_thsottiaux', cursor={'x_endpoints': {'syndication': endpoint, 'oembed': endpoint}},
            success=True, last_item_at=None, payload_hash='', error_code='', now=NOW-3600)
        for offset in (0, 3600):
            scanner, client = make_scan()
            worker = ResetAlertWorker(store=state, config=ResetAlertConfig(enabled=True), scanner=scanner,
                send_text=lambda *_: pytest.fail('no delivery requested'), is_online=lambda: False,
                stop_event=threading.Event(), clock=lambda: NOW+offset)
            assert worker.scan_due(now=NOW+offset)
            sources = {row['source_id']: row for row in state.reset_alert_status()['sources']}
            x = sources['x_thsottiaux']
            assert x['last_attempt_at'] == NOW-3600 and x['last_success_at'] == NOW-3600
            assert x['health'] != 'ok' and client.oembed_calls == []
            assert sources['forecast']['last_attempt_at'] == NOW+offset
    finally: state.close()


def test_cli_reports_independent_retry_and_actual_fallback_state():
    # 23:30 expiry must wait for 08:00, not a global source scan timestamp.
    expiry = int(datetime.fromisoformat('2026-08-31T23:30:00+08:00').timestamp())
    cursor = {'x_endpoints': {'syndication': {'cooldown_until': expiry,
        'last_error': 'source_http_429', 'consecutive_429': 4, 'cooldown_basis': 'server'}},
        'forecast_discovery': {'success': False, 'candidate_count': 0, 'checked_at': NOW},
        'official_verification': {'attempted': False, 'verified_count': 2, 'error_code': ''}}
    result = _x_endpoint_status(cursor, now=NOW)
    endpoint = result['x_endpoint_states']['syndication']
    assert endpoint['next_attempt_at'] == '2026-09-01T08:00:00+08:00'
    assert endpoint['retry_not_before'] == '2026-08-31T23:30:00+08:00'
    assert result['fallback_discovery']['state'] == 'unavailable'
    assert result['official_verification']['state'] == 'cached'
    assert _x_endpoint_status({}, now=NOW)['fallback_discovery']['state'] == 'unknown'


def test_parent_page_cooldown_does_not_block_other_oembed_candidates():
    ids = ['2095651088502591861', '2095651088502591862']
    parent = '2095651088502591800'
    scanner, client = make_scan()
    for guid in ids:
        client.oembeds[guid] = _oembed_payload(guid, 'Codex limits will reset in 3 hours')
        client.payloads[f'https://x.com/thsottiaux/status/{guid}'] = ResetAlertSourceError('source_http_429')
    calls = []
    original = client.get_html
    def get_html(url, **kwargs):
        calls.append(url)
        return original(url, **kwargs)
    client.get_html = get_html
    result = scanner._x(tuple({'guid': guid, 'published_at': NOW-60, 'reply_to_guid': parent} for guid in ids))
    assert client.oembed_calls == ids and len(calls) == 1
    assert len(result.signals) == 2 and not result.success


def test_cached_success_plus_failed_attempt_is_not_live_verified():
    scanner, client = make_scan()
    guid = '2095651088502591861'
    second = '2095651088502591862'
    first = scanner._x(({'guid': guid, 'published_at': NOW-60},))
    scanner.verified_cache = {s.item_id: s for s in first.signals}
    client.oembeds[second] = ResetAlertSourceError('source_http_429')
    result = scanner._x(tuple({'guid': x, 'published_at': NOW-60} for x in [guid, second]))
    assert result.cursor['live_verified_count'] == 0
    status = _x_endpoint_status({'official_verification': {
        'attempted': True, 'verified_count': 1, 'live_verified_count': 0,
        'error_code': result.error_code}}, now=NOW)
    assert status['official_verification']['state'] == 'cached'


def test_invalid_official_payload_does_not_mark_endpoint_success():
    scanner, client = make_scan()
    guid = '2095651088502591861'
    client.oembeds[guid] = _oembed_payload(guid, 'text', author_url='https://x.com/not_the_author')
    with pytest.raises(ResetAlertSourceError, match='author_mismatch'): scanner._oembed(guid)
    assert not scanner.endpoint_states['oembed'].get('last_success_at')
    assert scanner.endpoint_states['oembed']['last_error'] == 'x_oembed_author_mismatch'
