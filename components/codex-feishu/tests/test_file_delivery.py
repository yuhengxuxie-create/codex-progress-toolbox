import hashlib
import json
import threading
import time
from dataclasses import replace

import pytest

from progress_wx.delivered_files import DeliveredFileCandidate
from progress_wx.file_delivery import FileDeliveryQueue
from progress_wx.feishu import FeishuSendError, FeishuSendRejectedError
from progress_wx.models import TurnEvent
from progress_wx.state import StateStore


def candidate(path):
    data=path.read_bytes()
    return DeliveredFileCandidate(str(path),path,path.name,'explicit_delivery','ready',True,
                                  size=len(data),sha256=hashlib.sha256(data).hexdigest())


class Channel:
    def __init__(self):
        self.calls=[]
        self.notices=[]
        self.failure={}
        self.online=True
    def is_online(self): return self.online
    def send_file(self,data,*,file_name,idempotency_key):
        self.calls.append((file_name,data,idempotency_key))
        if file_name in self.failure: raise self.failure[file_name]
        return ['message-'+idempotency_key]
    def send_text(self,text,*,idempotency_key):
        self.notices.append(text)
        return ['notice-'+idempotency_key]


@pytest.fixture
def setup(tmp_path):
    store=StateStore(tmp_path/'state.sqlite')
    channel=Channel()
    bindings=[]
    queue=FileDeliveryQueue(store,tmp_path/'artifact-snapshots',channel,
                            lambda row,ids,notice:bindings.append((row['turn_id'],ids,notice)),start=False)
    yield store,channel,queue,bindings
    queue.stop()
    store.close()


def rows(queue): return queue._rows('SELECT * FROM artifact_file_deliveries ORDER BY created,ordinal')


def test_same_path_dedup_new_turn_and_different_names(setup,tmp_path):
    store,ch,q,bindings=setup
    a=tmp_path/'中文报告.bin';b=tmp_path/'copy.bin'
    a.write_bytes(b'one');b.write_bytes(b'one')
    event=TurnEvent('thread','turn','completed')
    q.reserve(event,[candidate(a),candidate(a),candidate(b)])
    q.drain_once()
    assert [x[0] for x in ch.calls]==[a.name,b.name]
    a.write_bytes(b'two')
    q.reserve(event,[candidate(a)])
    q.drain_once()
    assert len(ch.calls)==2
    q.reserve(replace(event,turn_id='turn2'),[candidate(a)])
    q.drain_once()
    assert ch.calls[-1][1]==b'two'
    assert bindings[-1][0]=='turn2'


def test_snapshot_survives_source_deletion_and_restart(setup,tmp_path):
    store,ch,q,bindings=setup
    path=tmp_path/'report';path.write_bytes(b'original')
    ch.online=False
    q.reserve(TurnEvent('t','u','completed'),[candidate(path)])
    q.drain_once()
    path.unlink()
    restarted=FileDeliveryQueue(store,q.blobs.root,ch,q.bind_messages,start=False)
    ch.online=True
    restarted.drain_once()
    assert ch.calls[0][1]==b'original'
    assert rows(q)[0]['state']=='done'


def test_change_before_capture_is_explicit_failure(setup,tmp_path):
    _,ch,q,_=setup
    path=tmp_path/'file';path.write_bytes(b'original')
    q.reserve(TurnEvent('t','u','completed'),[candidate(path)])
    path.write_bytes(b'changed')
    q.drain_once()
    assert not ch.calls
    assert rows(q)[0]['state']=='rejected'
    assert rows(q)[0]['reason']=='source_changed_before_snapshot'


def test_one_failure_does_not_block_other_files_and_unknown_frozen(setup,tmp_path):
    _,ch,q,_=setup
    paths=[tmp_path/f'{i}.bin' for i in range(3)]
    for i,path in enumerate(paths): path.write_bytes(bytes([i]))
    ch.failure['1.bin']=FeishuSendError('unknown')
    q.reserve(TurnEvent('t','u','completed'),[candidate(path) for path in paths])
    q.drain_once();q.drain_once()
    assert [r['state'] for r in rows(q)]==['done','uncertain','done']
    assert len(ch.calls)==3
    q.cleanup_snapshots()
    assert len(list(q.blobs.root.glob('*.blob')))==1
    restarted=FileDeliveryQueue(q.store,q.blobs.root,ch,q.bind_messages,start=False)
    restarted.drain_once()
    assert len(ch.calls)==3


def test_known_success_binding_failure_only_rebinds(setup,tmp_path):
    _,ch,q,_=setup
    path=tmp_path/'file';path.write_bytes(b'bytes')
    count=[]
    def bind(row,ids,notice):
        count.append(ids)
        if len(count)==1: raise RuntimeError('temporary_database_failure')
    q.bind_messages=bind
    q.reserve(TurnEvent('t','u','completed'),[candidate(path)])
    q.drain_once()
    assert rows(q)[0]['state']=='binding'
    q.drain_once()
    assert rows(q)[0]['state']=='done'
    assert len(ch.calls)==1 and len(count)==2


def test_no_small_file_count_cap(setup,tmp_path):
    _,ch,q,_=setup
    paths=[]
    for i in range(70):
        path=tmp_path/f'{i}.py';path.write_bytes(str(i).encode());paths.append(path)
    q.reserve(TurnEvent('t','u','completed'),[candidate(p) for p in paths])
    for _ in range(5): q.drain_once();q.cleanup_snapshots()
    assert len(ch.calls)==70
    assert all(r['state']=='done' for r in rows(q))
    assert not list(q.blobs.root.glob('*.blob'))


def test_slow_upload_does_not_block_reservation(setup,tmp_path):
    _,ch,q,_=setup
    path=tmp_path/'file';path.write_bytes(b'bytes')
    entered=threading.Event();release=threading.Event()
    original=ch.send_file
    def send(*args,**kwargs):
        entered.set();release.wait(3);return original(*args,**kwargs)
    ch.send_file=send
    q.reserve(TurnEvent('t','u','completed'),[candidate(path)])
    thread=threading.Thread(target=q.drain_once);thread.start()
    assert entered.wait(2)
    start=time.monotonic()
    q.reserve(TurnEvent('t','next','completed'),[candidate(path)])
    assert time.monotonic()-start<.5
    release.set();thread.join(3)
    assert not thread.is_alive()


def test_migrations_preserve_unknown_and_reopen(setup,tmp_path):
    store,_,q,_=setup
    path=tmp_path/'file';path.write_bytes(b'bytes')
    q.reserve(TurnEvent('t','u','completed'),[candidate(path)])
    q._update(rows(q)[0]['delivery_id'],state='uncertain',reason='old_unknown')
    for version in (22,10):
        with store._lock,store._connection:
            store._connection.execute("UPDATE meta SET value=? WHERE key='schema_version'",(str(version),))
        reopened=StateStore(store.path)
        assert reopened._connection.execute("SELECT state FROM artifact_file_deliveries").fetchone()[0]=='uncertain'
        assert reopened._connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        reopened.close()


def test_stat_only_discovery_and_real_sample_bytes(setup):
    from pathlib import Path
    from progress_wx.delivered_files import discover_delivered_files
    _,ch,q,_=setup
    root=Path(__file__).resolve().parents[1]/'tests/fixtures/artifact-samples'
    paths=[root/f'artifact-sample.{suffix}' for suffix in ('docx','pdf','xlsx','zip')]
    text='\n'.join(f'[报告](<{p.as_posix()}>)' for p in paths)
    result=discover_delivered_files([{'type':'agentMessage','id':'f','phase':'final_answer','text':text}],turn_id='u',inspect_content=False)
    assert len(result.ready)==4 and all(not c.sha256 for c in result.ready)
    q.reserve(TurnEvent('t','u','completed'),result.candidates)
    q.drain_once()
    assert [call[1] for call in ch.calls]==[p.read_bytes() for p in paths]


def test_first_activation_does_not_backfill_history(setup,tmp_path):
    store,ch,q,_=setup
    path=tmp_path/'report';path.write_bytes(b'file')
    old=TurnEvent('t','old','completed',completed_at=int(q.enabled_at)-10)
    q.reserve(old,[candidate(path)])
    no_time=TurnEvent('t','old-no-time','completed')
    store.mark_processed(no_time.dedupe_key)
    q.reserve(no_time,[candidate(path)])
    assert not rows(q)
    new=TurnEvent('t','new','completed',completed_at=int(q.enabled_at)+1)
    store.mark_processed(new.dedupe_key)  # late file projection remains valid
    q.reserve(new,[candidate(path)])
    q.drain_once()
    assert len(ch.calls)==1


def test_untyped_transport_oserror_is_unknown(setup,tmp_path):
    _,ch,q,_=setup
    path=tmp_path/'file';path.write_bytes(b'file')
    ch.failure[path.name]=OSError('response connection lost')
    q.reserve(TurnEvent('t','u','completed'),[candidate(path)])
    q.drain_once();q.drain_once()
    assert rows(q)[0]['state']=='uncertain'
    assert len(ch.calls)==1


def test_readonly_status_does_not_resend(setup,monkeypatch,capsys):
    from types import SimpleNamespace
    from progress_wx import cli
    store,ch,q,_=setup
    monkeypatch.setattr(cli,'_config',lambda *a,**k:SimpleNamespace(service=SimpleNamespace(database=store.path)))
    assert cli._artifact_status(SimpleNamespace(thread_id='',offset=0,limit=100))==0
    assert json.loads(capsys.readouterr().out)['available'] is True
    assert ch.calls==[]


@pytest.mark.parametrize('name,data',[
    ('audio.wav',b'RIFF'+b'\x24\x00\x00\x00'+b'WAVEfmt '+b'\x10\x00\x00\x00'+b'\x01\x00\x01\x00'+b'\x40\x1f\x00\x00'+b'\x40\x1f\x00\x00'+b'\x01\x00\x08\x00'+b'data\x00\x00\x00\x00'),
    ('video.mp4',b'\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2'),
    ('example.py',b'print("artifact sample")\n'),
    ('no-extension',b'\x00\xff\x01\x02'),
])
def test_generic_media_and_code_preserve_exact_bytes(setup,tmp_path,name,data):
    _,ch,q,_=setup
    path=tmp_path/name;path.write_bytes(data)
    q.reserve(TurnEvent('t','u','completed'),[candidate(path)])
    q.drain_once()
    assert ch.calls[0][:2]==(name,data)


def test_slow_snapshot_write_does_not_hold_main_reservation(setup,tmp_path,monkeypatch):
    _,_,q,_=setup
    path=tmp_path/'file';path.write_bytes(b'file')
    entered=threading.Event();release=threading.Event()
    original=q.blobs.put
    def slow(data):
        entered.set();release.wait(3);return original(data)
    monkeypatch.setattr(q.blobs,'put',slow)
    q.reserve(TurnEvent('t','u','completed'),[candidate(path)])
    thread=threading.Thread(target=q.drain_once);thread.start()
    assert entered.wait(2)
    start=time.monotonic()
    q.reserve(TurnEvent('t','next','completed'),[candidate(path)])
    assert time.monotonic()-start<.5
    release.set();thread.join(3)
    assert not thread.is_alive()


def test_recovery_page_rotates_past_pending_transport_prefix(setup,tmp_path):
    from types import SimpleNamespace
    _,ch,q,bindings=setup
    path=tmp_path/'file';path.write_bytes(b'file')
    ch.online=False
    ch.store=SimpleNamespace(outcome=lambda key:{'state':'pending','result':None,'error':None})
    for i in range(40):
        q.reserve(TurnEvent('t',f'u{i}','completed'),[candidate(path)])
    saved=rows(q)
    for row in saved:
        q._update(row['delivery_id'],state='submitted')
    q._update(saved[-1]['delivery_id'],state='binding',message_ids_json='["known-message"]')
    q.drain_once();q.drain_once()
    assert rows(q)[-1]['state']=='done'
    assert bindings[-1][1]==['known-message']
    assert not ch.calls


def test_same_unresolved_link_history_and_rollout_only_one_notice(setup):
    _,_,q,_=setup
    first=DeliveredFileCandidate('rollout-candidate',None,'report.pdf','explicit_delivery','unverified',True,
                                  reason='unverified_remote_reference',uri='sandbox:/mnt/data/report.pdf')
    second=replace(first,candidate_id='history-candidate')
    event=TurnEvent('t','u','completed')
    q.reserve(event,[first]);q.reserve(event,[second])
    assert len(rows(q))==1


def test_real_message_binding_returns_reply_to_original_task(setup,tmp_path):
    from types import SimpleNamespace
    from progress_wx.service import ProgressService
    from progress_wx.state import CorrelationCodec
    store,ch,q,_=setup
    service=ProgressService(tmp_path/'unused-config.yaml')
    service.store=store
    service.codec=CorrelationCodec(b'synthetic-secret-for-unit-tests-32')
    service.config=SimpleNamespace(messaging=SimpleNamespace(pending_ttl_hours=24))
    q.bind_messages=service._bind_artifact_messages
    path=tmp_path/'file';path.write_bytes(b'file')
    q.reserve(TurnEvent('original-task','original-turn','completed'),[candidate(path)])
    q.drain_once()
    message_id=json.loads(rows(q)[0]['message_ids_json'])[0]
    code=store.code_for_channel_message(message_id)
    assert code
    reply=store.enqueue_turn_reply(code,'inbound-new','synthetic-fingerprint',service.codec,reply_text='继续修改')
    assert reply is not None
    assert reply.thread_id=='original-task' and reply.turn_id=='original-turn'


def test_small_snapshot_quota_does_not_reject_an_online_batch(setup,tmp_path,monkeypatch):
    from progress_wx import guardian_blob
    _,ch,q,_=setup
    monkeypatch.setattr(guardian_blob,'MAX_TOTAL_BYTES',12)
    paths=[]
    for i in range(16):
        path=tmp_path/f'{i}.bin';path.write_bytes(bytes([i])*8);paths.append(path)
    q.reserve(TurnEvent('t','u','completed'),[candidate(path) for path in paths])
    q.drain_once()
    assert len(ch.calls)==16
    assert all(row['state']=='done' for row in rows(q))


def test_capacity_wait_is_persistent_and_recovers_after_space_frees(setup,tmp_path,monkeypatch):
    from progress_wx import guardian_blob
    _,ch,q,_=setup
    monkeypatch.setattr(guardian_blob,'MAX_TOTAL_BYTES',12)
    ch.online=False
    paths=[]
    for i in range(2):
        path=tmp_path/f'{i}.bin';path.write_bytes(bytes([i])*8);paths.append(path)
    q.reserve(TurnEvent('t','u','completed'),[candidate(path) for path in paths])
    q.drain_once()
    assert [r['state'] for r in rows(q)]==['pending','capture_pending']
    assert rows(q)[1]['reason']=='snapshot_capacity_wait'
    ch.online=True
    q._update(rows(q)[1]['delivery_id'],next_at=0)
    q.drain_once()
    assert all(row['state']=='done' for row in rows(q))
    assert len(ch.calls)==2
    assert not ch.notices
    assert rows(q)[1]['reason']=='' and rows(q)[1]['notice_state']=='none'


def test_notice_recovery_uses_persisted_key_after_file_reason_changes(setup,tmp_path):
    from types import SimpleNamespace
    _,ch,q,bindings=setup
    path=tmp_path/'file';path.write_bytes(b'file')
    q.reserve(TurnEvent('t','u','completed'),[candidate(path)])
    row=rows(q)[0]
    q._update(row['delivery_id'],reason='snapshot_capacity_wait',notice_state='submitted',notice_key='actual-submitted-notice-key')
    q._update(row['delivery_id'],state='done',reason='')
    looked_up=[]
    def outcome(key):
        looked_up.append(key)
        return {'state':'done','result':'["capacity-notice"]','error':None}
    ch.store=SimpleNamespace(outcome=outcome)
    ch.online=False
    q.drain_once()
    assert looked_up==['actual-submitted-notice-key']
    assert rows(q)[0]['notice_state']=='done'
    assert bindings[-1][1]==['capacity-notice']


def test_capture_recovery_preserves_unknown_notice_identity(setup,tmp_path):
    _,ch,q,_=setup
    path=tmp_path/'file';path.write_bytes(b'file')
    q.reserve(TurnEvent('t','u','completed'),[candidate(path)])
    row=rows(q)[0]
    q._update(row['delivery_id'],reason='snapshot_capacity_wait',notice_state='uncertain',notice_key='unknown-notice-key')
    q.drain_once()
    row=rows(q)[0]
    assert row['state']=='done' and row['reason']==''
    assert row['notice_state']=='uncertain' and row['notice_key']=='unknown-notice-key'
    assert not ch.notices
