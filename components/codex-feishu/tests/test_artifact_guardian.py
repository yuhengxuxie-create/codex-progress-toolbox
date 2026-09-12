import hashlib
import json
import threading
from types import SimpleNamespace

import pytest

from progress_wx.guardian_store import GuardianStore
from progress_wx.guardian_channel import GuardianChannel
from progress_wx.feishu import FeishuSendRejectedError
from progress_wx.file_delivery import FileDeliveryQueue
from progress_wx.delivered_files import DeliveredFileCandidate
from progress_wx.models import TurnEvent
from progress_wx.state import StateStore


def test_guardian_preserves_safe_upload_exception_type_without_message(tmp_path):
    from progress_wx.guardian import Guardian
    config=SimpleNamespace(path=tmp_path/'unused.yaml',
        service=SimpleNamespace(database=tmp_path/'business.sqlite',pid_file=tmp_path/'worker.pid'),
        feishu=SimpleNamespace(target_open_id='synthetic-owner'))
    def failed_upload(**payload):
        guardian.stop.set()
        try:
            raise TypeError('synthetic-private-detail-must-not-be-persisted')
        except TypeError as error:
            raise FeishuSendRejectedError(code='upload_failed',raw_code=None,retryable=False) from error
    channel=SimpleNamespace(is_online=lambda:True,send_file=failed_upload)
    guardian=Guardian(config,channel)
    try:
        guardian.store.enqueue('synthetic-contract','send_file',{'file_name':'sample.zip','idempotency_key':'synthetic-contract','data_b64':'ZmlsZQ=='})
        guardian._send_loop()
        result=guardian.store.outcome('synthetic-contract')
        assert result['state']=='rejected'
        detail=json.loads(result['result'])
        assert detail['exception_type']=='TypeError'
        assert detail['code']=='upload_failed' and detail['retryable'] is False
        assert 'synthetic-private-detail' not in result['result']
    finally:
        guardian.stop.set()
        guardian.store.close()


def test_upload_cache_exact_identity_and_reopen(tmp_path):
    store=GuardianStore(tmp_path/'guardian')
    store.store_media_key('file','stable','a'*64,'report.bin','file_key_a')
    store.close()
    store=GuardianStore(tmp_path/'guardian')
    try:
        assert store.lookup_media_key('file','stable','a'*64,'report.bin')=='file_key_a'
        assert store.lookup_media_key('file','stable','b'*64,'report.bin') is None
        assert store.lookup_media_key('file','stable','a'*64,'other.bin') is None
        assert store.lookup_media_key('image','stable','a'*64,'report.bin') is None
        with pytest.raises(ValueError,match='conflict'):
            store.store_media_key('file','stable','a'*64,'report.bin','conflicting_key')
        assert store.lookup_media_key('file','stable','a'*64,'report.bin')=='file_key_a'
    finally:
        store.close()


def test_proxy_keeps_explicit_platform_rejection_code():
    proxy=GuardianChannel.__new__(GuardianChannel)
    proxy.is_online=lambda:True
    proxy.stop_event=threading.Event()
    proxy.store=SimpleNamespace(enqueue=lambda *args:None,outcome=lambda key:{
        'state':'rejected','error':'upload_failed',
        'result':json.dumps({'code':'upload_failed','raw_code':234006,'retryable':False})})
    with pytest.raises(FeishuSendRejectedError) as caught:
        proxy.send_file(b'file',file_name='file.bin',idempotency_key='stable')
    assert caught.value.raw_code==234006
    assert caught.value.code=='upload_failed'
    assert caught.value.retryable is False


def test_restart_reconciles_unknown_without_new_send(tmp_path):
    store=StateStore(tmp_path/'business.sqlite')
    guardian=GuardianStore(tmp_path/'guardian')
    calls=[]
    channel=SimpleNamespace(store=guardian,is_online=lambda:False,
        send_file=lambda *a,**kw:calls.append(kw),send_text=lambda *a,**kw:())
    queue=FileDeliveryQueue(store,tmp_path/'artifact-snapshots',channel,lambda *a:None,start=False)
    path=tmp_path/'file.bin';path.write_bytes(b'original')
    candidate=DeliveredFileCandidate('candidate',path,path.name,'explicit_delivery','ready',True,
                                    size=8,sha256=hashlib.sha256(b'original').hexdigest())
    queue.reserve(TurnEvent('thread','turn','completed'),[candidate]);queue.drain_once()
    row=queue._rows('SELECT * FROM artifact_file_deliveries')[0]
    key=queue._key(row)
    guardian.enqueue(key,'send_file',{'file_name':path.name,'data_b64':'b3JpZ2luYWw='})
    guardian.claim();guardian.finish(key,'uncertain',error='response_lost')
    guardian.store_media_key('file',key,candidate.sha256,path.name,'file_uploaded_before_timeout')
    queue._update(row['delivery_id'],state='submitted')
    guardian.close();guardian=GuardianStore(tmp_path/'guardian');channel.store=guardian
    queue.drain_once();queue.cleanup_snapshots();queue.drain_once()
    recovered=queue._rows('SELECT * FROM artifact_file_deliveries')[0]
    assert recovered['state']=='uncertain' and recovered['notice_state']=='pending'
    assert recovered['snapshot_json'] and list(queue.blobs.root.glob('*.blob'))
    assert guardian.lookup_media_key('file',key,candidate.sha256,path.name)=='file_uploaded_before_timeout'
    assert not calls
    guardian.close();store.close()
