from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from progress_wx.guardian import Guardian,control_root,guardian_status,_alive
from progress_wx.guardian_store import GuardianStore
from progress_wx.guardian_channel import GuardianChannel
from progress_wx.channel import ChannelReply
from progress_wx.feishu import FeishuSendError,FeishuSendNotSubmittedError
from progress_wx.process_control import process_creation_time


def config(root):
    return SimpleNamespace(path=root/'config.yaml',service=SimpleNamespace(database=root/'business.sqlite',pid_file=root/'worker.pid'),feishu=SimpleNamespace(target_open_id='synthetic-owner'))


class FakeChannel:
    def __init__(self): self.online=True; self.sent=[]
    def is_online(self): return self.online
    def start(self,callback): self.callback=callback
    def stop(self): self.online=False
    def send_text(self,text,*,idempotency_key):
        self.sent.append((idempotency_key,text))
        return 'synthetic-message'


def test_status_never_initializes(tmp_path):
    c=config(tmp_path/'missing')
    s=guardian_status(c)
    assert s['available'] is False and not s['guardian']['healthy']
    assert not c.service.database.parent.exists()


def test_intent_maintenance_preserves_previous_and_refuses_start(tmp_path):
    s=GuardianStore(tmp_path/'guardian')
    s.intent('running');s.intent('maintenance');s.intent('maintenance')
    assert s.get('previous_desired_state')=='running'
    with pytest.raises(ValueError):s.intent('running')
    s.leave_maintenance()
    assert s.get('desired_state')=='running'
    s.close()


def test_outgoing_dedup_unknown_and_hash_conflict(tmp_path):
    s=GuardianStore(tmp_path/'guardian')
    s.enqueue('key','send_text',{'text':'a'})
    s.enqueue('key','send_text',{'text':'a'})
    with pytest.raises(ValueError):s.enqueue('key','send_text',{'text':'b'})
    assert s.claim()['key']=='key'
    s.recover()
    assert s.outcome('key')['state']=='uncertain' and s.claim() is None
    s.close()


def test_queue_bound_and_inbound_generation_redelivery(tmp_path,monkeypatch):
    import progress_wx.guardian_store as module
    monkeypatch.setattr(module,'MAX_QUEUE',1)
    s=GuardianStore(tmp_path/'guardian')
    s.enqueue('one','send_text',{'text':'a'})
    with pytest.raises(BufferError):s.enqueue('two','send_text',{'text':'a'})
    s.receive('incoming',{'content':'hello'})
    assert s.inbound('g1')['key']=='incoming'
    s.accepted('incoming','g1')
    assert s.inbound('g1') is None and s.inbound('g2')['key']=='incoming'
    s.accepted('incoming','system')
    assert s.inbound('g2') is None
    s.close()


def test_command_not_starved_by_ordinary_history_and_freshness(tmp_path):
    c=config(tmp_path); g=Guardian(c,FakeChannel())
    g.store.intent('stopped')
    for i in range(140):g.receive(ChannelReply('synthetic-owner','ordinary',message_id=str(i),chat_id='chat'))
    command=ChannelReply('synthetic-owner','.启动飞书机器人',message_id='start',chat_id='chat',created_at=int(time.time()))
    g.receive(command);g._system_commands();g._system_commands()
    assert g.store.get('desired_state')=='running'
    g.store.intent('stopped');g.receive(command);g._system_commands()
    assert g.store.get('desired_state')=='stopped'
    g.receive(replace(command,message_id='old',created_at=1));g._system_commands()
    assert g.store.get('desired_state')=='stopped'
    g.store.close()


def test_command_owner_and_exact_text(tmp_path):
    g=Guardian(config(tmp_path),FakeChannel());g.store.intent('stopped')
    g.receive(ChannelReply('stranger','.启动飞书机器人',message_id='wrong',chat_id='chat',created_at=int(time.time())))
    g.receive(ChannelReply('synthetic-owner',' .启动飞书机器人',message_id='space',chat_id='chat',created_at=int(time.time())))
    g._system_commands()
    assert g.store.get('desired_state')=='stopped'
    g.store.close()


def test_cancelled_pending_can_retry_but_submitted_is_unknown(tmp_path):
    c=config(tmp_path);p=GuardianChannel(c,'g')
    p.store.put('guardian',{'heartbeat_at':time.time()});p.store.put('channel',{'online':True})
    with pytest.raises(FeishuSendNotSubmittedError):p._call('send_text',{'text':'x'},'k',timeout=.01)
    assert p.store.outcome('k')['state']=='cancelled'
    p.store.enqueue('k','send_text',{'text':'x'})
    assert p.store.claim()['key']=='k'
    with pytest.raises(FeishuSendError):p._call('send_text',{'text':'x'},'k',timeout=.01)
    p.stop();p.store.close()


def test_sender_failure_cannot_look_healthy_and_spawn_failure_notifies(tmp_path):
    g=Guardian(config(tmp_path),FakeChannel(),spawn=lambda token: (_ for _ in ()).throw(OSError('synthetic')))
    g.store.intent('running');g.tick()
    assert g.store.get('worker')['state']=='failed'
    assert g.store.db.execute("select count(*) from outgoing where key like 'system:failed:%'").fetchone()[0]==1
    g.send_thread=threading.Thread(target=lambda:None);g.send_thread.start();g.send_thread.join()
    with pytest.raises(RuntimeError,match='sender_unresponsive'):g.tick()
    g.store.close()


def test_spawn_child_heartbeat_not_overwritten(tmp_path):
    c=config(tmp_path)
    def spawn(token):
        g.store.put('worker',dict(generation=token,pid=os.getpid(),creation_time=process_creation_time(os.getpid()),ready=True,state='ready',heartbeat_at=time.time()))
        return SimpleNamespace(pid=os.getpid())
    g=Guardian(c,FakeChannel(),spawn=spawn);g.store.intent('running');g.tick()
    assert g.store.get('worker')['ready'] is True
    g.tick();g.tick()
    assert g.store.db.execute("select count(*) from outgoing where key like 'system:ready:%'").fetchone()[0]==1
    g.store.close()


def test_reconnect_one_transition_not_new_start(tmp_path):
    ch=FakeChannel();g=Guardian(config(tmp_path),ch);g.tick()
    ch.online=False;g.tick();g.tick();ch.online=True;g.tick();g.tick()
    assert g.store.db.execute("select count(*) from outgoing where key like 'system:reconnected:%'").fetchone()[0]==1
    g.store.close()


def test_invalid_identity_not_alive():
    assert not _alive({'pid':999999999,'creation_time':None})


def test_real_menu_adapter_to_guardian_proxy_without_chat_id(tmp_path):
    import asyncio
    from progress_wx.feishu import FeishuMessageChannel
    c=config(tmp_path);g=Guardian(c,FakeChannel());p=GuardianChannel(c,'generation')
    received=[]
    adapter=FeishuMessageChannel(app_id='synthetic-app',app_secret='synthetic-secret',target_open_id='synthetic-owner')
    adapter._on_reply=g.receive
    p.start(received.append)
    try:
        for event_id in ('menu-1','menu-2','menu-1'):
            asyncio.run(adapter._handle_bot_menu(SimpleNamespace(header=SimpleNamespace(event_id=event_id),event={'event_key':'progress_wx_feature_center','operator':{'operator_id':{'open_id':'synthetic-owner'}}})))
        deadline=time.monotonic()+3
        while len(received)<2 and time.monotonic()<deadline:time.sleep(.05)
        assert len(received)==2
        assert all(m.source_kind=='bot_menu' and m.chat_id=='' for m in received)
    finally:
        p.stop();p.store.close();g.store.close()


@pytest.mark.skipif(os.name!='nt',reason='Windows exact DACL')
def test_existing_private_dacl_removes_foreign_explicit_ace(tmp_path):
    import subprocess
    import ctypes
    from ctypes import wintypes
    root=tmp_path/'guardian';s=GuardianStore(root);s.close()
    subprocess.run(['icacls',str(root),'/grant','*S-1-1-0:(OI)(CI)R'],check=True,capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    s=GuardianStore(root);s.close()
    for p in (root,root/'transport.sqlite'):
        advapi=ctypes.WinDLL('advapi32',use_last_error=True)
        advapi.GetFileSecurityW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)]
        size=wintypes.DWORD();advapi.GetFileSecurityW(str(p),4,None,0,ctypes.byref(size))
        buffer=ctypes.create_string_buffer(size.value)
        assert advapi.GetFileSecurityW(str(p),4,buffer,size.value,ctypes.byref(size))
        output=ctypes.c_void_p()
        advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes=[ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(ctypes.c_void_p),ctypes.c_void_p]
        assert advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(buffer,1,4,ctypes.byref(output),None)
        try:
            sddl=ctypes.wstring_at(output)
            assert ';;;WD)' not in sddl and ';;;BU)' not in sddl
            assert sddl.startswith('D:P') and sddl.count('(A;')==2
        finally:
            k=ctypes.WinDLL('kernel32');k.LocalFree.argtypes=[ctypes.c_void_p];k.LocalFree(output)


def test_restoration_supersedes_unsent_disconnect(tmp_path):
    ch=FakeChannel();g=Guardian(config(tmp_path),ch)
    g.tick();ch.online=False;g.tick()
    key=g.store.db.execute("select key from outgoing where key like 'system:disconnected:%'").fetchone()[0]
    ch.online=True;g.tick()
    assert g.store.outcome(key)['state']=='superseded'
    assert g.store.db.execute("select count(*) from outgoing where key like 'system:reconnected:%'").fetchone()[0]==1
    g.store.close()


def test_future_worker_heartbeat_never_announces_ready(tmp_path):
    g=Guardian(config(tmp_path),FakeChannel());g.store.intent('running')
    g.store.put('worker',dict(pid=os.getpid(),creation_time=process_creation_time(os.getpid()),generation='future',ready=True,state='ready',heartbeat_at=time.time()+60))
    g.tick()
    assert not guardian_status(g.config)['worker']['ready']
    assert g.store.outcome('system:ready:future') is None
    g.store.close()


def test_start_ignores_previous_failed_and_rejects_concurrent_stop(tmp_path,monkeypatch):
    import progress_wx.guardian as module
    c=config(tmp_path);s=GuardianStore(control_root(c));s.intent('stopped')
    monkeypatch.setattr(module,'ensure_guardian',lambda c:None)
    monkeypatch.setattr(module,'instance_running',lambda path:False)
    count=[0]
    def snapshot(c):
        count[0]+=1
        return {'desired_state':'running','worker':{'running':False,'ready':count[0]>=3,'state':'failed'},'channel':{'online':True}}
    monkeypatch.setattr(module,'guardian_status',snapshot)
    assert module.control(c,'start',timeout=2)==0 and count[0]>=3
    def stopped(c):
        return {'desired_state':'stopped','worker':{'running':False,'ready':False,'state':'stopped'},'channel':{'online':True}}
    monkeypatch.setattr(module,'guardian_status',stopped)
    assert module.control(c,'start',timeout=2)==1
    s.close()


def test_legacy_refusal_does_not_change_desired_state(tmp_path,monkeypatch):
    import progress_wx.guardian as module
    c=config(tmp_path);s=GuardianStore(control_root(c));s.intent('stopped')
    monkeypatch.setattr(module,'instance_running',lambda path:path==c.service.pid_file)
    with pytest.raises(RuntimeError,match='legacy_worker'):
        module.control(c,'start',timeout=1)
    assert s.get('desired_state')=='stopped'
    s.close()
