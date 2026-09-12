import json
import threading
from pathlib import Path
import pytest
from test_file_delivery import setup, candidate, rows
from progress_wx.file_delivery import FileDeliveryQueue, failure_description
from progress_wx.delivered_files import discover_delivered_files, make_final_agent_item
from progress_wx.models import TurnEvent

def discover(path, style="directive"):
    text = ':codex-file-citation{path="'+str(path)+'" purpose="output"}' if style=="directive" else '[output](<'+str(path)+'>)'
    return discover_delivered_files([make_final_agent_item(item_id="final",text=text)],turn_id="u").candidates

def test_same_turn_local_missing_recovers_without_duplicate_notice(setup,tmp_path):
    store,ch,q,_=setup
    p=tmp_path/'report？.bin';event=TurnEvent('t','u','completed')
    q.reserve(event,discover(p));q.drain_once()
    assert rows(q)[0]['reason']=='missing' and len(ch.notices)==1
    p.write_bytes(b'actual')
    q.reserve(event,discover(p));q.drain_once();q.drain_once()
    assert rows(q)[0]['state']=='done' and len(ch.calls)==1 and len(ch.notices)==1
    audit=q._rows("SELECT value FROM meta WHERE key LIKE 'artifact_local_recovery:%'")
    assert len(audit)==1 and json.loads(audit[0]['value'])['reason']=='missing'

def test_restart_does_not_backfill_old_missing(setup,tmp_path):
    store,ch,q,_=setup
    p=tmp_path/'report.bin';event=TurnEvent('t','u','completed')
    q.reserve(event,discover(p));q.drain_once()
    restarted=FileDeliveryQueue(store,q.blobs.root,ch,q.bind_messages,start=False)
    p.write_bytes(b'actual')
    restarted.reserve(event,discover(p));restarted.drain_once()
    assert rows(q)[0]['state']=='rejected' and not ch.calls

@pytest.mark.parametrize('state',['submitted','uncertain','done','binding'])
def test_no_recovery_after_possible_submission(setup,tmp_path,state):
    _,ch,q,_=setup
    p=tmp_path/'report.bin';event=TurnEvent('t','u','completed')
    q.reserve(event,discover(p));q._update(rows(q)[0]['delivery_id'],state=state,attempts=1)
    p.write_bytes(b'actual');q.reserve(event,discover(p))
    assert rows(q)[0]['state']==state and not ch.calls

def test_capture_checks_parent_even_after_discovery(setup,tmp_path,monkeypatch):
    _,ch,q,_=setup
    source=tmp_path/'source';source.mkdir()
    p=source/'report.bin';p.write_bytes(b'actual')
    q.reserve(TurnEvent('t','u','completed'),[candidate(p)])
    original=Path.lstat
    class Reparse:
        st_mode=0o040755
        st_file_attributes=0x400
    def replaced(path,*args,**kwargs):
        return Reparse() if path==source else original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'lstat',replaced)
    q.drain_once()
    assert not ch.calls and rows(q)[0]['reason']=='reparse_path'

def test_ready_candidate_cannot_bypass_validation(setup,tmp_path):
    _,ch,q,_=setup
    p=tmp_path/'report.bin';p.write_bytes(b'actual');c=candidate(p);p.unlink()
    q.reserve(TurnEvent('t','u','completed'),[c]);q.drain_once()
    assert rows(q)[0]['reason']=='missing' and not ch.calls

def test_literal_percent_in_directive_keeps_exact_file(tmp_path):
    literal=tmp_path/'report%20name.bin';decoded=tmp_path/'report name.bin'
    literal.write_bytes(b'literal');decoded.write_bytes(b'decoded')
    result=discover(literal)
    assert len(result)==1 and result[0].path==literal and result[0].sha256==candidate(literal).sha256

@pytest.mark.parametrize('drive_prefix',[False,True])
def test_ambiguous_markdown_encoding_is_rejected(tmp_path,drive_prefix):
    literal=tmp_path/'report%20name.bin';decoded=tmp_path/'report name.bin'
    literal.write_bytes(b'literal');decoded.write_bytes(b'decoded')
    target=('/'+literal.as_posix()) if drive_prefix else str(literal)
    result=discover(target,'markdown')
    assert all(not c.ready for c in result) and any(c.reason=='ambiguous_path_encoding' for c in result)

def test_permission_failure_has_actionable_local_cause(setup,tmp_path,monkeypatch):
    _,ch,q,_=setup
    p=tmp_path/'report.bin';p.write_bytes(b'actual')
    q.reserve(TurnEvent('t','u','completed'),[candidate(p)])
    original=Path.open
    def denied(path,*args,**kwargs):
        if path==p:raise PermissionError('private detail')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',denied);q.drain_once()
    assert rows(q)[0]['reason']=='local_permission_denied'
    assert not ch.calls and '权限' in ch.notices[0] and 'private detail' not in ch.notices[0]

def test_concurrent_rediscovery_reserves_one_recovery(setup,tmp_path):
    _,ch,q,_=setup
    p=tmp_path/'report.bin';event=TurnEvent('t','u','completed')
    q.reserve(event,discover(p));p.write_bytes(b'actual')
    errors=[]
    def reserve():
        try:q.reserve(event,discover(p))
        except Exception as e:errors.append(e)
    threads=[threading.Thread(target=reserve) for _ in range(5)]
    for t in threads:t.start()
    for t in threads:t.join()
    assert not errors
    q.drain_once()
    assert len(ch.calls)==1 and len(q._rows("SELECT value FROM meta WHERE key LIKE 'artifact_local_recovery:%'"))==1

def test_missing_does_not_claim_deleted():
    message=failure_description('missing')
    assert '未找到' in message and '实际生成路径' in message and '已不存在' not in message

def test_stale_notice_is_not_sent_after_recovery(setup,tmp_path):
    _,ch,q,_=setup
    p=tmp_path/'report.bin';event=TurnEvent('t','u','completed')
    q.reserve(event,discover(p))
    stale=rows(q)[0]
    selected=threading.Event();recovered=threading.Event()
    def notify():
        selected.set();assert recovered.wait(3)
        q._send(stale,notice=True)
    thread=threading.Thread(target=notify);thread.start();assert selected.wait(3)
    p.write_bytes(b'actual');q.reserve(event,discover(p));recovered.set();thread.join()
    q.drain_once()
    assert not ch.notices and len(ch.calls)==1 and rows(q)[0]['state']=='done'

@pytest.mark.parametrize('outcome',['recover','exhausted','unknown'])
def test_actual_guardian_retry_and_unknown_contract(tmp_path,outcome):
    from types import SimpleNamespace
    from progress_wx.guardian import Guardian
    from progress_wx.feishu import FeishuSendRejectedError, FeishuSendError
    calls=[]
    def send(**payload):
        calls.append(payload['idempotency_key'])
        if outcome=='unknown':raise FeishuSendError('response_lost')
        if outcome=='exhausted' or len(calls)<3:
            raise FeishuSendRejectedError(code='rate_limited',raw_code=999,retryable=True)
        return 'platform-success'
    config=SimpleNamespace(path=tmp_path/'unused.yaml',service=SimpleNamespace(database=tmp_path/'business.sqlite',pid_file=tmp_path/'worker.pid'),feishu=SimpleNamespace(target_open_id='synthetic-owner'))
    guardian=Guardian(config,SimpleNamespace(is_online=lambda:True,send_file=send))
    original=guardian.store.finish
    def finish(key,state,**kwargs):
        original(key,state,**kwargs)
        if state=='pending':
            with guardian.store.lock,guardian.store.db:
                guardian.store.db.execute('UPDATE outgoing SET next_at=0 WHERE key=?',(key,))
        else:guardian.stop.set()
    guardian.store.finish=finish
    try:
        guardian.store.enqueue('same-file-key','send_file',{'file_name':'sample.bin','data_b64':'Ynl0ZXM='})
        guardian._send_loop()
        result=guardian.store.outcome('same-file-key')
        assert set(calls)=={'same-file-key'}
        if outcome=='recover':assert result['state']=='done' and len(calls)==3
        elif outcome=='unknown':assert result['state']=='uncertain' and len(calls)==1
        else:
            assert result['state']=='rejected' and len(calls)==5
            assert json.loads(result['result'])['retryable'] is False
            assert json.loads(result['result'])['retry_exhausted'] is True
    finally:guardian.store.close()
