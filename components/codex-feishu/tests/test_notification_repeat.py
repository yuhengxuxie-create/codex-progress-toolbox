import os,threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from progress_wx.guardian import Guardian
from progress_wx.codex_store import CodexStore,ThreadRecord
from progress_wx.process_control import process_creation_time
from test_guardian import config,FakeChannel

def setup_guardian(tmp_path,monkeypatch):
    clock=[1000.0]
    monkeypatch.setattr('progress_wx.guardian.time.time',lambda:clock[0])
    g=Guardian(config(tmp_path),FakeChannel())
    g.store.intent('running')
    record={'generation':'synthetic','pid':os.getpid(),'creation_time':process_creation_time(os.getpid()),'state':'ready','ready':True,'started_at':800.0,'heartbeat_at':960.0}
    g.store.put('worker',record)
    return g,clock,record

def tick(g,clock,record,at,heartbeat=None):
    clock[0]=at
    if heartbeat is not None:record['heartbeat_at']=heartbeat
    g.store.put('worker',dict(record));g.tick()

def faults(g):return g.store.db.execute("SELECT count(*) FROM outgoing WHERE key LIKE 'system:hung:%'").fetchone()[0]

def test_repeated_brief_healthy_pulses_are_one_fault_episode(tmp_path,monkeypatch):
    g,c,w=setup_guardian(tmp_path,monkeypatch)
    try:
        g.tick()
        for n in range(1,12):
            at=1000+n*35
            tick(g,c,w,at-1)
            tick(g,c,w,at,at)
        assert faults(g)==1
        assert g.store.get('last_error_code')=='worker_unresponsive'
        assert not g.store.get('worker_error_history',[])
    finally:g.store.close()

def test_stable_progress_closes_episode_then_new_timeout_is_new_event(tmp_path,monkeypatch):
    g,c,w=setup_guardian(tmp_path,monkeypatch)
    try:
        g.tick()
        for at in [1010,1030,1050]:tick(g,c,w,at,at)
        assert g.store.get('last_error_code')=='worker_unresponsive'
        tick(g,c,w,1070,1070)
        assert g.store.get('last_error_code') is None
        tick(g,c,w,1101)
        assert faults(g)==2
    finally:g.store.close()

def test_restart_preserves_episode_and_recovery_progress(tmp_path,monkeypatch):
    g,c,w=setup_guardian(tmp_path,monkeypatch)
    g.tick();tick(g,c,w,1010,1010);tick(g,c,w,1030,1030)
    g.store.close();g=Guardian(config(tmp_path),FakeChannel())
    try:
        tick(g,c,w,1050,1050)
        assert faults(g)==1 and g.store.get('last_error_code')=='worker_unresponsive'
        tick(g,c,w,1070,1070)
        assert g.store.get('last_error_code') is None
    finally:g.store.close()

@pytest.mark.parametrize('change',['fixed','rollback','intent','generation'])
def test_invalid_recovery_proof_does_not_clear_fault(tmp_path,monkeypatch,change):
    g,c,w=setup_guardian(tmp_path,monkeypatch)
    try:
        g.tick();tick(g,c,w,1010,1010);tick(g,c,w,1030,1030)
        if change=='intent':g.store.put('intent_at',2000)
        if change=='generation':w['generation']='other'
        for at in ([1005,1020,1040] if change=='rollback' else [1040,1060,1080]):
            tick(g,c,w,at,None if change=='fixed' else at)
        assert g.store.get('last_error_code')=='worker_unresponsive'
    finally:g.store.close()

def test_concurrent_connections_own_one_episode_and_stale_snapshot_is_rejected(tmp_path,monkeypatch):
    g,c,w=setup_guardian(tmp_path,monkeypatch)
    other=Guardian(config(tmp_path),FakeChannel())
    try:
        with ThreadPoolExecutor(2) as pool:
            keys=list(pool.map(lambda obj:obj._record_worker_error('worker_unresponsive',w,1000),[g,other]))
        assert keys[0]==keys[1]
        current=dict(w,heartbeat_at=1000);g.store.put('worker',current)
        assert g._record_worker_error('worker_unresponsive',w,1001) is None
    finally:other.store.close();g.store.close()

def test_metadata_batch_reuses_only_directory_and_preserves_errors_ownership(tmp_path,monkeypatch):
    store=CodexStore(codex_home=tmp_path);calls=[];turns=[]
    record=ThreadRecord('synthetic',title='Synthetic')
    def directory(*,prepare_rollout_ownership=True):
        calls.append(prepare_rollout_ownership)
        store._errors().append('synthetic-read-error')
        store._query_state.shared_rollout_paths={'shared'}
        return [record],True
    monkeypatch.setattr(store,'_read_threads_uncached',directory,raising=False)
    monkeypatch.setattr(store,'_read_turns',lambda tid:(turns.append(tid) or [],True))
    monkeypatch.setattr(store,'_read_rollout_latest_turn',lambda r:None)
    with store.metadata_batch():
        first=store.snapshot('synthetic');second=store.snapshot('synthetic')
        assert first.errors==second.errors==('synthetic-read-error',)
        assert store._query_state.shared_rollout_paths=={'shared'}
    assert len(calls)==1 and len(turns)==2
    with store.metadata_batch():store.snapshot('synthetic')
    assert len(calls)==2
    assert getattr(store._query_state,'metadata_batch',None) is None

def test_metadata_batch_is_thread_local_and_cleared_on_failure(tmp_path,monkeypatch):
    store=CodexStore(codex_home=tmp_path);barrier=threading.Barrier(2);calls=[]
    def directory(**kwargs):calls.append(threading.get_ident());return [],True
    monkeypatch.setattr(store,'_read_threads_uncached',directory,raising=False)
    def read(_):
        with pytest.raises(RuntimeError),store.metadata_batch():
            store._read_threads();barrier.wait();store._read_threads();raise RuntimeError()
        assert getattr(store._query_state,'metadata_batch',None) is None
    with ThreadPoolExecutor(2) as pool:list(pool.map(read,range(2)))
    assert len(calls)==2 and len(set(calls))==2

def test_sender_rechecks_fault_before_submission(tmp_path,monkeypatch):
    g,c,w=setup_guardian(tmp_path,monkeypatch)
    class Once:
        def __init__(self):self.count=0
        def wait(self,_):self.count+=1;return self.count>1
    try:
        g.tick()
        g.store.put('worker',dict(w,heartbeat_at=1000))
        g.stop=Once();g._send_loop()
        assert not g.channel.sent
        assert g.store.outcome('system:hung:synthetic')['state']=='pending'
        g.store.put('worker_error_context',None);g.store.put('last_error_code',None)
        c[0]=1003;g.stop=Once();g._send_loop()
        assert not g.channel.sent
        assert g.store.outcome('system:hung:synthetic')['state']=='superseded'
    finally:g.store.close()

def test_batch_refreshes_real_shared_rollout_ownership_next_poll(tmp_path,monkeypatch):
    from test_codex_store import CodexStoreTests
    import sqlite3
    fixture=CodexStoreTests();fixture.setUp()
    try:
        store=fixture.store
        with sqlite3.connect(fixture.state) as db:
            db.execute("UPDATE threads SET rollout_path=? WHERE id IN ('thread-a','thread-b')",('/synthetic/shared.jsonl',))
        db.close()
        with store.metadata_batch():
            store._begin_query();store._read_threads()
            assert store._rollout_path_key('/synthetic/shared.jsonl') in store._query_state.shared_rollout_paths
            with sqlite3.connect(fixture.state) as db:
                db.execute("UPDATE threads SET rollout_path=? WHERE id='thread-b'",('/synthetic/other.jsonl',))
            db.close()
            store._begin_query();store._read_threads()
            assert store._rollout_path_key('/synthetic/shared.jsonl') in store._query_state.shared_rollout_paths
        with store.metadata_batch():
            store._begin_query();store._read_threads()
            assert not store._query_state.shared_rollout_paths
    finally:fixture.tearDown()
