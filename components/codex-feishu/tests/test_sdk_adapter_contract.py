"""Exercise our adapter through the installed SDK; replace only HTTP I/O."""
import asyncio
import hashlib
import json
import socket
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from lark_channel.channel import FeishuChannel, OutboundConfig, RetryConfig
from progress_wx.feishu import FeishuMessageChannel, FeishuSendError


@contextmanager
def connected_adapter(tmp_path, **hooks):
    sdk=FeishuChannel(app_id='cli_synthetic_contract',app_secret='synthetic',
        outbound=OutboundConfig(retry=RetryConfig(max_attempts=1)))
    # Seed an already-connected lifecycle without starting a WebSocket. All
    # upload, send, coercion, driver and response methods remain real SDK code.
    sdk._ready_flag=True
    sdk._ws_client=SimpleNamespace(_conn=object())
    loop=asyncio.new_event_loop()
    started=threading.Event()
    def run():
        asyncio.set_event_loop(loop)
        started.set()
        loop.run_forever()
    thread=threading.Thread(target=run)
    thread.start()
    assert started.wait(2)
    adapter=FeishuMessageChannel(app_id='cli_synthetic_contract',app_secret='synthetic',
        target_open_id='ou_synthetic_contract',sdk_factory=lambda *args:sdk,
        media_cache_dir=tmp_path/'cache',**hooks)
    adapter._channel=sdk
    adapter._loop=loop
    adapter._thread=thread
    adapter._online_event.set()
    assert adapter.is_online()
    try:
        yield adapter,sdk
    finally:
        adapter._online_event.clear()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(3)
        assert not thread.is_alive()
        loop.close()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    original=socket.socket.connect
    original_create=socket.create_connection
    def guarded(sock,address,*args,**kwargs):
        if isinstance(address,tuple) and address[0] in {'127.0.0.1','::1','localhost'}:
            return original(sock,address,*args,**kwargs)
        raise AssertionError('unexpected network I/O')
    def guarded_create(address,*args,**kwargs):
        if isinstance(address,tuple) and address[0] in {'127.0.0.1','::1','localhost'}:
            return original_create(address,*args,**kwargs)
        raise AssertionError('unexpected network I/O')
    # Windows asyncio uses a loopback socketpair for its local wakeup pipe.
    monkeypatch.setattr(socket.socket,'connect',guarded)
    monkeypatch.setattr(socket,'create_connection',guarded_create)


@pytest.mark.parametrize('kind',['file','image'])
def test_adapter_upload_persist_and_create_use_real_sdk(tmp_path,monkeypatch,kind):
    payload=b'synthetic-exact-bytes'
    name='报告 (修订版).bin'
    key='file_synthetic_contract' if kind=='file' else 'img_synthetic_contract'
    order=[]
    cache={}
    def persist(media_kind,token,digest,filename,media_key):
        assert media_kind==kind and digest==hashlib.sha256(payload).hexdigest()
        cache['key']=media_key
        order.append('persist')
    with connected_adapter(tmp_path,media_key_store=persist) as (adapter,sdk):
        async def upload(request):
            body=request.request_body
            stream=body.file if kind=='file' else body.image
            assert stream.getvalue()==payload
            if kind=='file':
                assert body.file_type=='stream' and body.file_name==name and stream.name==name
            else:
                assert body.image_type=='message'
            order.append('upload')
            return SimpleNamespace(code=0,msg='ok',data=SimpleNamespace(**{'file_key' if kind=='file' else 'image_key':key}))
        async def create(request):
            assert cache['key']==key
            body=request.request_body
            assert body.msg_type==kind
            assert json.loads(body.content)=={'file_key' if kind=='file' else 'image_key':key}
            assert body.uuid
            order.append('create')
            return SimpleNamespace(code=0,msg='ok',data=SimpleNamespace(message_id='om_synthetic_result'))
        monkeypatch.setattr(getattr(sdk.client.im.v1,kind),'acreate',upload)
        monkeypatch.setattr(sdk.client.im.v1.message,'acreate',create)
        if kind=='file':
            result=adapter.send_file(payload,file_name=name,idempotency_key='synthetic-delivery')
        else:
            result=adapter.send_image(payload,idempotency_key='synthetic-delivery')
        assert result=='om_synthetic_result'
        assert order==['upload','persist','create']


def test_adapter_cached_key_real_sdk_unknown_is_not_reported_success(tmp_path,monkeypatch):
    counts={'upload':0,'create':0}
    with connected_adapter(tmp_path,media_key_lookup=lambda *args:'file_cached_contract') as (adapter,sdk):
        async def upload(request):
            counts['upload']+=1
            raise AssertionError('persisted key must avoid upload')
        async def create(request):
            counts['create']+=1
            assert json.loads(request.request_body.content)=={'file_key':'file_cached_contract'}
            raise RuntimeError('synthetic uncertain HTTP result')
        monkeypatch.setattr(sdk.client.im.v1.file,'acreate',upload)
        monkeypatch.setattr(sdk.client.im.v1.message,'acreate',create)
        with pytest.raises(FeishuSendError) as caught:
            adapter.send_file(b'synthetic',file_name='cached.bin',idempotency_key='synthetic-cached')
        assert caught.value.uploaded is True
        assert caught.value.media_key=='file_cached_contract'
        assert counts=={'upload':0,'create':1}
