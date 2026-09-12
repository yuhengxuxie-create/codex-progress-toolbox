from dataclasses import replace
from datetime import datetime, timezone

import pytest

from progress_wx.reset_alert import ResetSignal, classify_signals, _future_evidence

NOW = int(datetime(2026, 9, 9, 8, tzinfo=timezone.utc).timestamp())


def signal(text, *, source='x_thsottiaux', item='official'):
    return ResetSignal(source, item, 'https://example.test/' + item, NOW-120, text,
                       'x_post', True, {'discovery_source': 'x_syndication'})


def forecast(score):
    return ResetSignal('forecast', 'forecast', 'https://example.test/forecast', NOW-60, '',
                       'forecast', False, {'score': score})


@pytest.mark.parametrize('body', [
    'We might increase Codex usage limits in 2 hours.',
    'Should we increase Codex usage limits in 2 hours?',
    'Maybe we will restore Codex usage limits in 2 hours.',
    'We could replenish Codex usage limits in 2 hours.',
    'If things work out, we will increase Codex usage limits in 2 hours.',
    'We will not increase Codex usage limits in 2 hours.',
    'Codex usage limits may be restored tomorrow.',
])
def test_noncommitment_never_becomes_a_or_b(body):
    assert classify_signals((signal(body), forecast(90)), forecast_threshold=70, now=NOW) == ()


@pytest.mark.parametrize('body', [
    'We are resetting Codex usage limits in 2 hours.',
    "We're restoring ChatGPT Work usage limits in 2 hours.",
    'Codex usage limits will be reset in 2 hours.',
])
def test_explicit_planned_action_and_passive_promise_are_a(body):
    result = classify_signals((signal(body),), forecast_threshold=70, now=NOW)
    assert len(result) == 1 and result[0].level == 'A'


def test_progressive_nonquota_action_does_not_borrow_quota_from_another_clause():
    result = classify_signals((signal('We are resetting passwords in 2 hours. Codex usage limits remain unchanged.'),), forecast_threshold=70, now=NOW)
    assert result == ()


@pytest.mark.parametrize('body', [
    'Just joking: Codex usage limits have an issue, lol.',
    'We have restored Codex usage limits.',
    'We already increased Codex usage limits.',
])
def test_forecast_does_not_reanimate_jokes_or_past(body):
    assert classify_signals((signal(body), forecast(90)), forecast_threshold=70, now=NOW) == ()


@pytest.mark.parametrize('source', ['x_thsottiaux', 'openai_status'])
def test_explicit_official_compensation_without_time_is_b(source):
    result = classify_signals((signal('Codex is experiencing a quota outage. We will compensate affected users with usage credits.', source=source),), forecast_threshold=70, now=NOW)
    assert len(result) == 1 and result[0].level == 'B'


@pytest.mark.parametrize('intent', [
    'We are working on compensation with usage credits.',
    'We want to make this right with quota credits.',
])
def test_official_compensation_intention_does_not_require_will(intent):
    result = classify_signals((signal('Codex quota outage confirmed. ' + intent),), forecast_threshold=70, now=NOW)
    assert len(result) == 1 and result[0].level == 'B'


def test_low_fresh_forecast_marks_conflict_without_suppressing_official_a():
    official = signal('We will reset Codex usage limits in 2 hours.')
    result = classify_signals((official, forecast(10)), forecast_threshold=70, now=NOW)
    assert len(result) == 1 and result[0].level == 'A'
    assert '10%' in result[0].evidence and '冲突' in result[0].evidence
    stale = replace(forecast(10), published_at=NOW-3*86400)
    assert '冲突' not in classify_signals((official, stale), forecast_threshold=70, now=NOW)[0].evidence


@pytest.mark.parametrize('body', [
    'We will reset Codex usage limits at 2026-09-09T07:00:00Z today.',
    'We will reset Codex usage limits on 2026-09-08 Monday.',
    'We will reset Codex usage limits last Monday.',
])
def test_past_time_is_not_reinterpreted_as_new_future(body):
    assert classify_signals((signal(body),), forecast_threshold=70, now=NOW) == ()


def test_authorized_available_announcement_and_direct_reply_are_preserved():
    announced = signal('We have reset Codex usage limits.')
    result = classify_signals((announced, forecast(90)), forecast_threshold=70, now=NOW)
    assert len(result) == 1 and result[0].phase == 'announced_available'
    parent = replace(signal('Codex usage limits are affected.', source='x_parent_context', item='parent'), kind='x_parent_context', published_at=0)
    child = replace(signal('We will reset them within 4 hours.'), metadata={'parent_id': 'parent', 'reply_relationship_verified': True})
    assert classify_signals((parent, child), forecast_threshold=70, now=NOW)[0].level == 'A'
    assert classify_signals((parent, replace(child, metadata={})), forecast_threshold=70, now=NOW) == ()


def test_verified_yes_only_reply_is_explicitly_unsupported():
    parent = replace(signal('Will Codex usage limits be reset?', source='x_parent_context', item='parent'), kind='x_parent_context', published_at=0)
    child = replace(signal('Yes, in 2 hours.'), metadata={'parent_id': 'parent', 'reply_relationship_verified': True})
    assert classify_signals((parent, child), forecast_threshold=70, now=NOW) == ()


def test_night_does_not_touch_pending_or_unknown_and_morning_expires_first(tmp_path):
    import sqlite3
    import threading
    from progress_wx.config import ResetAlertConfig
    from progress_wx.reset_alert import ResetAlertWorker
    from progress_wx.state import StateStore
    night = int(datetime.fromisoformat('2026-09-10T00:00:00+08:00').timestamp())
    morning = night+8*3600
    state = StateStore(tmp_path / 'state.sqlite')
    sent = []
    try:
        for name, expiry in [('expired', night+60), ('live', morning+3600), ('unknown', morning+3600)]:
            state.reserve_reset_alert_event(event_key=name, level='B', evidence=name,
                window_text='北京时间', advice='继续观察', source_ids=('x_thsottiaux',),
                fingerprint=name, expires_at=expiry, now=night-60)
        with sqlite3.connect(tmp_path / 'state.sqlite') as db, db:
            db.execute("UPDATE reset_alert_deliveries SET uncertain_at=? WHERE event_key='unknown'", (night-30,))
            before = db.execute('SELECT * FROM reset_alert_deliveries ORDER BY event_key').fetchall()
        worker = ResetAlertWorker(store=state, config=ResetAlertConfig(), send_text=lambda *_: sent.append('sent') or 'fixture',
            is_online=lambda: True, stop_event=threading.Event(), clock=lambda: night)
        assert not worker.deliver_one(now=night)
        assert not worker.deliver_one(now=morning-1)
        with sqlite3.connect(tmp_path / 'state.sqlite') as db:
            assert db.execute('SELECT * FROM reset_alert_deliveries ORDER BY event_key').fetchall() == before
        assert worker.deliver_one(now=morning)
        assert len(sent) == 1
        with sqlite3.connect(tmp_path / 'state.sqlite') as db:
            assert db.execute("SELECT expired_at FROM reset_alert_deliveries WHERE event_key='expired'").fetchone()[0] == morning
            assert db.execute("SELECT uncertain_at FROM reset_alert_deliveries WHERE event_key='unknown'").fetchone()[0] == night-30
    finally: state.close()


def test_request_submitted_before_midnight_can_finish_after_midnight(tmp_path):
    import threading
    from progress_wx.config import ResetAlertConfig
    from progress_wx.reset_alert import ResetAlertWorker
    from progress_wx.state import StateStore
    night = int(datetime.fromisoformat('2026-09-10T00:00:00+08:00').timestamp())
    clock = [night-1]
    state = StateStore(tmp_path / 'state.sqlite')
    calls = []
    try:
        state.reserve_reset_alert_event(event_key='boundary', level='B', evidence='fixture',
            window_text='北京时间', advice='继续观察', source_ids=('x_thsottiaux',),
            fingerprint='boundary', expires_at=night+3600, now=night-10)
        def send(*_args):
            calls.append(clock[0])
            clock[0] = night
            return 'fixture-platform-result'
        worker = ResetAlertWorker(store=state, config=ResetAlertConfig(), send_text=send,
            is_online=lambda: True, stop_event=threading.Event(), clock=lambda: clock[0])
        assert worker.deliver_one()
        assert not worker.deliver_one()
        assert calls == [night-1]
    finally: state.close()
