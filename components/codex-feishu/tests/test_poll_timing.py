import logging
from types import SimpleNamespace

import pytest

from progress_wx import service


@pytest.fixture(autouse=True)
def isolated_service_log_capture(monkeypatch,caplog):
    # Earlier logging setup tests deliberately disable parent propagation.
    # Capture this logger explicitly and restore its prior routing afterwards.
    monkeypatch.setattr(service.LOGGER,'propagate',False)
    service.LOGGER.addHandler(caplog.handler)
    try:
        yield
    finally:
        service.LOGGER.removeHandler(caplog.handler)


@pytest.mark.parametrize('elapsed,slow',[(2,False),(29.99,False),(30,True),(43,True)])
def test_only_slow_poll_cycles_log_bounded_phase_durations(monkeypatch,caplog,elapsed,slow):
    clock=[100.0]
    monkeypatch.setattr(service.time,'monotonic',lambda:clock[0])
    timer=service._PollCycleTiming()
    clock[0]+=1;timer.enter('thread_snapshots')
    clock[0]=100+elapsed
    with caplog.at_level(logging.WARNING,logger='progress_wx.service'):timer.finish()
    assert bool(caplog.records)==slow
    if slow:
        assert len(caplog.records)==1
        assert 'channel_health:1.000' in caplog.text and 'thread_snapshots:' in caplog.text


def test_poll_failure_keeps_original_exception_and_emits_only_timing(monkeypatch,caplog):
    clock=[1.0];monkeypatch.setattr(service.time,'monotonic',lambda:clock[0])
    original=RuntimeError('synthetic-private-content-must-not-be-logged')
    def fail(config,timing):
        timing.enter('notification_media_outbox');clock[0]+=35
        raise original
    instance=SimpleNamespace(_poll_once_measured=fail)
    with caplog.at_level(logging.WARNING,logger='progress_wx.service'):
        with pytest.raises(RuntimeError) as raised:service.ProgressService._poll_once(instance,object())
    assert raised.value is original
    assert len(caplog.records)==1 and 'notification_media_outbox:35.000' in caplog.text
    assert 'private-content' not in caplog.text


def test_phase_names_cannot_inject_task_metadata():
    with pytest.raises(ValueError,match='unknown polling phase'):
        service._PollCycleTiming().enter('synthetic-task-content')
