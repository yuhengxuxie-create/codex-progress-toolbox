import os
from types import SimpleNamespace
import pytest
from progress_wx.guardian import Guardian
from progress_wx.process_control import process_creation_time
from test_guardian import config,FakeChannel

@pytest.mark.parametrize('desired',['running','stopped','exited','maintenance'])
def test_new_windows_logon_only_resumes_running_intent_once(tmp_path,monkeypatch,desired):
    g=Guardian(config(tmp_path),FakeChannel(),spawn=lambda token:SimpleNamespace(pid=99999999))
    g.store.intent('running');g.store.put('worker',{'state':'ready','generation':'old','pid':99999999,'creation_time':1})
    g.store.put('last_launch_intent',g.store.get('intent_at'))
    g.observe_windows_session('old-logon')
    if desired!='running':g.store.intent(desired)
    before=g.store.get('intent_at')
    monkeypatch.setattr('progress_wx.guardian.time.time',lambda:1.0)
    g.observe_windows_session('new-logon');after=g.store.get('intent_at')
    assert (after>before)==(desired=='running')
    g.observe_windows_session('new-logon');assert g.store.get('intent_at')==after
    assert g.store.get('desired_state')==desired
    if desired=='running':
        g.tick();g.tick()
        assert g.store.get('worker')['generation']!='old'
    g.store.close()

def test_missing_or_same_logon_does_not_retry_failed_worker(tmp_path):
    g=Guardian(config(tmp_path),FakeChannel())
    g.store.intent('running');g.store.put('worker',{'state':'failed','generation':'old'})
    before=g.store.get('intent_at')
    g.observe_windows_session(None);g.observe_windows_session('initial');g.observe_windows_session('initial')
    assert g.store.get('intent_at')==before
    g.store.close()

def test_new_logon_does_not_replace_live_worker(tmp_path):
    g=Guardian(config(tmp_path),FakeChannel())
    g.store.intent('running');g.observe_windows_session('old')
    g.store.put('worker',dict(pid=os.getpid(),creation_time=process_creation_time(os.getpid())))
    before=g.store.get('intent_at');g.observe_windows_session('new')
    assert g.store.get('intent_at')==before
    g.store.close()

def test_new_logon_start_failure_is_not_retried_by_same_logon(tmp_path):
    calls=[]
    def fail(token):calls.append(token);raise OSError('synthetic launch failure')
    g=Guardian(config(tmp_path),FakeChannel(),spawn=fail)
    g.store.intent('running');g.observe_windows_session('old')
    g.store.put('worker',{'state':'failed','generation':'old'})
    g.store.put('last_launch_intent',g.store.get('intent_at'))
    g.observe_windows_session('new');g.tick()
    assert len(calls)==1 and g.store.get('worker')['state']=='failed'
    g.observe_windows_session('new');g.tick()
    assert len(calls)==1
    g.store.intent('running');g.tick();assert len(calls)==2
    g.store.close()
