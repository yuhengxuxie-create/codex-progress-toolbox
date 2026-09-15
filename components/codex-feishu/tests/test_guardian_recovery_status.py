import os
import time
from types import SimpleNamespace

import pytest
from unittest.mock import patch

from progress_wx.guardian import Guardian, guardian_status
from progress_wx.process_control import process_creation_time
from test_guardian import FakeChannel, config


def worker(generation='same', age=0, ready=True):
    return dict(generation=generation, pid=os.getpid(), creation_time=process_creation_time(os.getpid()),
                heartbeat_at=time.time()-age, started_at=time.time()-120, ready=ready, state='ready' if ready else 'starting')


def stable_recovery(g, current=None):
    """Recovery now requires continuous progress, not one fresh sample."""
    record = dict(current or worker())
    start = time.time()
    for offset in (0,20,40,60):
        with patch('progress_wx.guardian.time.time', return_value=start+offset):
            record['heartbeat_at'] = start+offset
            g.store.put('worker', dict(record));g.tick()


@pytest.mark.parametrize('ready,age,error,prefix', [
    (True,40,'worker_unresponsive','hung'),
    (False,0,'worker_initialization_timeout','startup-timeout'),
])
def test_same_generation_real_recovery_clears_active_error_preserves_history(tmp_path, ready,age,error,prefix):
    g=Guardian(config(tmp_path),FakeChannel())
    try:
        g.store.intent('running')
        g.store.put('worker',worker(age=age,ready=ready));g.tick()
        assert g.store.get('last_error_code')==error
        fault_key='system:'+prefix+':same'
        before=dict(g.store.db.execute('SELECT * FROM outgoing WHERE key=?',(fault_key,)).fetchone())
        stable_recovery(g)
        assert g.store.get('last_error_code') is None
        after=dict(g.store.db.execute('SELECT * FROM outgoing WHERE key=?',(fault_key,)).fetchone())
        assert after['state']=='superseded' and after['error']=='worker_recovered'
        for key in before.keys()-{'state','error','updated'}:assert after[key]==before[key]
        g.store.put('worker',worker(age=40));g.tick();g.tick()
        assert g.store.get('last_error_code')=='worker_unresponsive'
        assert g.store.db.execute("SELECT count(*) FROM outgoing WHERE key LIKE 'system:hung:same%'").fetchone()[0]==(2 if error=='worker_unresponsive' else 1)
        assert len(g.store.get('worker_error_history'))==1
    finally:g.store.close()


@pytest.mark.parametrize('change', ['other-generation','offline','stale','not-ready','other-error'])
def test_recovery_cannot_clear_unverified_or_other_error(tmp_path,change):
    ch=FakeChannel();g=Guardian(config(tmp_path),ch)
    try:
        g.store.intent('running');g.store.put('worker',worker(age=40));g.tick()
        current=worker()
        if change=='other-generation':current=worker('another')
        if change=='offline':ch.online=False
        if change=='stale':current=worker(age=40)
        if change=='not-ready':current=worker(ready=False);current['started_at']=time.time()
        if change=='other-error':g.store.put('last_error_code','sender_failure')
        g.store.put('worker',current);g.tick()
        assert g.store.get('last_error_code')==('sender_failure' if change=='other-error' else 'worker_unresponsive')
    finally:g.store.close()


def test_old_control_data_requires_matching_persisted_fault_receipt(tmp_path):
    g=Guardian(config(tmp_path),FakeChannel())
    try:
        g.store.intent('running');g.store.put('worker',worker())
        g.store.put('last_error_code','worker_unresponsive')
        g.tick();assert g.store.get('last_error_code')=='worker_unresponsive'
        g.system('hung:other','synthetic fault');g.tick()
        assert g.store.get('last_error_code')=='worker_unresponsive'
        g.system('hung:same','synthetic fault');stable_recovery(g)
        assert g.store.get('last_error_code') is None
    finally:g.store.close()


@pytest.mark.parametrize('legacy', [True,False])
@pytest.mark.parametrize('proof', ['valid','changed-intent','wrong-token','not-ready','offline','other-error'])
def test_replacement_only_clears_old_fault_after_proven_new_launch(tmp_path,legacy,proof):
    ch=FakeChannel()
    g=Guardian(config(tmp_path),ch,spawn=lambda token:SimpleNamespace(pid=os.getpid()))
    try:
        old=worker('old',age=40)
        g.store.intent('running');g.store.put('worker',old);g.tick()
        if legacy:g.store.put('worker_error_context',None)
        old.update(pid=None,ready=False,state='failed')
        g.store.put('worker',old);g.store.intent('running');g.tick()
        token=g.store.get('worker_token')
        assert token and token!='old'
        current=worker(token)
        if proof=='changed-intent':g.store.put('intent_at',-1)
        if proof=='wrong-token':g.store.put('worker_token','not-the-worker')
        if proof=='not-ready':current.update(ready=False,started_at=time.time())
        if proof=='offline':ch.online=False
        if proof=='other-error':g.store.put('last_error_code','other-error')
        g.store.put('worker',current);g.tick()
        if proof=='valid':stable_recovery(g,current)
        expected=None if proof=='valid' else 'other-error' if proof=='other-error' else 'worker_unresponsive'
        assert g.store.get('last_error_code')==expected
        if proof=='valid':
            history=g.store.get('worker_error_history')
            assert history[-1]['generation']=='old' and history[-1]['recovered_generation']==token
        assert g.store.outcome('system:hung:old') is not None
    finally:g.store.close()


def test_persisted_timeout_episode_survives_guardian_reopen_without_duplicate(tmp_path):
    g=Guardian(config(tmp_path),FakeChannel())
    g.store.intent('running');g.store.put('worker',worker(age=40));g.tick()
    stable_recovery(g)
    g.store.put('worker',worker(age=40));g.tick()
    notice=g.store.get('worker_error_context')['notice_key']
    assert notice!='hung:same'
    g.store.close()
    restarted=Guardian(config(tmp_path),FakeChannel())
    try:
        restarted.tick();restarted.tick()
        assert restarted.store.get('worker_error_context')['notice_key']==notice
        assert restarted.store.db.execute("SELECT count(*) FROM outgoing WHERE key LIKE 'system:hung:same%'").fetchone()[0]==2
    finally:restarted.store.close()


@pytest.mark.parametrize('state',['done','uncertain','submitted'])
def test_recovery_preserves_already_submitted_fault_outcome(tmp_path,state):
    g=Guardian(config(tmp_path),FakeChannel())
    try:
        g.store.intent('running');g.store.put('worker',worker(age=40));g.tick()
        with g.store.db:g.store.db.execute('UPDATE outgoing SET state=? WHERE key=?',(state,'system:hung:same'))
        before=dict(g.store.db.execute('SELECT * FROM outgoing WHERE key=?',('system:hung:same',)).fetchone())
        stable_recovery(g)
        assert g.store.get('last_error_code') is None
        assert dict(g.store.db.execute('SELECT * FROM outgoing WHERE key=?',('system:hung:same',)).fetchone())==before
    finally:g.store.close()
