import threading
import time
from types import SimpleNamespace
import pytest
import requests
from progress_wx.guardian_rest import LifecycleRestSender
from progress_wx.feishu import FeishuSendError,FeishuSendNotSubmittedError
from progress_wx.guardian import Guardian


class Response:
    def __init__(self,value):self.value=value
    def json(self):return self.value
    def raise_for_status(self):pass


class Session:
    def __init__(self):self.calls=[];self.fail_auth=False;self.fail_send=False
    def post(self,url,**kw):
        self.calls.append((url,kw))
        if '/auth/' in url:
            if self.fail_auth:raise requests.ConnectionError('synthetic')
            return Response({'code':0,'tenant_access_token':'synthetic-token','expire':7200})
        if self.fail_send:raise requests.ReadTimeout('synthetic')
        return Response({'code':0,'data':{'message_id':'synthetic-id'}})


def test_rest_no_ws_fixed_origin_only_system_and_stable_uuid():
    s=Session();sender=LifecycleRestSender('app','secret','owner',session=s)
    assert sender.send_text('offline',idempotency_key='system:offline')=='synthetic-id'
    sender.send_text('offline',idempotency_key='system:offline')
    assert len(s.calls)==3
    assert all(url.startswith('https://open.feishu.cn/open-apis/') and kw['allow_redirects'] is False for url,kw in s.calls)
    assert s.calls[1][1]['json']['uuid']==s.calls[2][1]['json']['uuid']
    with pytest.raises(ValueError):sender.send_text('business',idempotency_key='business')


def test_auth_failure_before_submit_and_send_unknown_distinct():
    s=Session();s.fail_auth=True;sender=LifecycleRestSender('app','secret','owner',session=s)
    with pytest.raises(FeishuSendNotSubmittedError):sender.send_text('offline',idempotency_key='system:a')
    assert len(s.calls)==1
    s.fail_auth=False;s.fail_send=True
    with pytest.raises(FeishuSendError) as error:sender.send_text('offline',idempotency_key='system:a')
    assert not isinstance(error.value,FeishuSendNotSubmittedError)


def test_offline_guardian_only_drains_lifecycle_not_business(tmp_path):
    c=SimpleNamespace(path=tmp_path/'synthetic.yaml',service=SimpleNamespace(database=tmp_path/'business.sqlite',pid_file=tmp_path/'worker.pid'),feishu=SimpleNamespace(target_open_id='owner'))
    offline=SimpleNamespace(is_online=lambda:False)
    rest=LifecycleRestSender('app','secret','owner',session=Session())
    g=Guardian(c,offline,offline_sender=rest)
    g.store.enqueue('business','send_text',{'text':'hold'})
    g.system('offline','connection problem')
    t=threading.Thread(target=g._send_loop);t.start()
    try:
        deadline=time.monotonic()+3
        while time.monotonic()<deadline and g.store.outcome('system:offline')['state']!='done':time.sleep(.05)
        assert g.store.outcome('system:offline')['state']=='done'
        assert g.store.outcome('business')['state']=='pending'
    finally:g.stop.set();t.join();g.store.close()
