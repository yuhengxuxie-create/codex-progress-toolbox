import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import ctypes

import pytest

from progress_wx.guardian import guardian_status,control_root
from progress_wx.guardian_store import GuardianStore

HARNESS=Path(__file__).with_name('guardian_harness.py')
spec=importlib.util.spec_from_file_location('guardian_harness',HARNESS)
harness=importlib.util.module_from_spec(spec);spec.loader.exec_module(harness)


def wait(predicate,timeout=15):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=predicate()
        if value:return value
        time.sleep(.1)
    raise AssertionError('isolated process condition timeout')


def readlines(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def start(root):
    return subprocess.Popen([sys.executable,str(HARNESS),'guardian',str(root)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))


@pytest.mark.skipif(os.name!='nt',reason='Windows process identity integration')
def test_real_process_restart_queue_offline_and_manual_stop(tmp_path):
    c=harness.configuration(tmp_path)
    store=GuardianStore(control_root(c));store.intent('running')
    guardian=start(tmp_path)
    worker_pid=None
    try:
        s=wait(lambda: (s if (s:=guardian_status(c))['worker']['ready'] else None))
        worker_pid=s['worker']['pid']
        wait(lambda: len(readlines(tmp_path/'sent.jsonl'))==1)
        # Pressure through actual guardian sender while worker remains alive.
        for i in range(64):store.enqueue(f'pressure:{i}','send_text',{'text':'synthetic'})
        wait(lambda: len(readlines(tmp_path/'sent.jsonl'))>=65)
        (tmp_path/'offline').touch()
        wait(lambda:not guardian_status(c)['channel']['online'])
        store.enqueue('offline-held','send_text',{'text':'held'})
        time.sleep(.5)
        assert store.outcome('offline-held')['state']=='pending'
        (tmp_path/'offline').unlink()
        wait(lambda:store.outcome('offline-held')['state']=='done')
        wait(lambda:sum(x['key'].startswith('system:reconnected:') for x in readlines(tmp_path/'sent.jsonl'))==1)
        # Kill only the child process that this synthetic harness created.
        guardian.terminate();guardian.wait(timeout=5)
        assert not guardian_status(c)['guardian']['running']
        guardian=start(tmp_path)
        wait(lambda:guardian_status(c)['guardian']['healthy'])
        assert guardian_status(c)['worker']['pid']==worker_pid
        time.sleep(1)
        assert sum(x['key'].startswith('system:ready:') for x in readlines(tmp_path/'sent.jsonl'))==1
        store.intent('stopped')
        wait(lambda:not guardian_status(c)['worker']['running'])
        assert guardian_status(c)['guardian']['healthy']
        # Fresh unquoted remote command followed by ordinary messages must not starve.
        store.receive('remote-start',dict(sender_id='synthetic-owner',content='.启动飞书机器人',message_id='remote-start',chat_id='chat',source_kind='message',created_at=int(time.time())))
        wait(lambda:guardian_status(c)['worker']['ready'])
        for i in range(20):
            store.receive('in:'+str(i),dict(sender_id='synthetic-owner',content='synthetic',message_id='in:'+str(i),chat_id='chat'))
        wait(lambda:len(readlines(tmp_path/'received.jsonl'))==20)
        assert len({r['key'] for r in readlines(tmp_path/'received.jsonl')})==20
        store.intent('exited');guardian.wait(timeout=10)
        assert guardian.returncode==0
    finally:
        store.intent('exited')
        if guardian.poll() is None:
            try:guardian.wait(timeout=8)
            except subprocess.TimeoutExpired:guardian.terminate();guardian.wait(timeout=5)
        store.close()


@pytest.mark.skipif(os.name!='nt',reason='Windows handle recovery')
def test_real_control_hang_and_verified_recovery(tmp_path,monkeypatch):
    import progress_wx.guardian as module
    c=harness.configuration(tmp_path);s=GuardianStore(control_root(c));s.intent('stopped')
    processes=[start(tmp_path)]
    try:
        old=wait(lambda:(v if (v:=guardian_status(c))['guardian']['healthy'] else None))['guardian']
        (tmp_path/'block-control').touch()
        wait(lambda:not guardian_status(c)['guardian']['healthy'],timeout=20)
        assert processes[0].poll() is None  # OS process alive, control loop unhealthy.
        time.sleep(16)
        (tmp_path/'block-control').unlink()
        def launch(_config,command,*extra):
            assert command=='guardian-run'
            child=start(tmp_path);processes.append(child);return child
        monkeypatch.setattr(module,'launch',launch)
        assert module.recover_guardian(c,old['pid'],old['creation_time']+1,'unresponsive')==2
        assert module.recover_guardian(c,old['pid'],old['creation_time'],'unresponsive')==0
        new=guardian_status(c)
        assert new['guardian']['pid']!=old['pid'] and new['desired_state']=='stopped'
        assert new['worker']['running'] is False
        wait(lambda:any(x['key'].startswith('system:guardian-recovered:') for x in readlines(tmp_path/'sent.jsonl')))
    finally:
        s.intent('exited')
        for child in processes:
            try:child.wait(timeout=8)
            except subprocess.TimeoutExpired:child.terminate();child.wait(timeout=5)
        s.close()


@pytest.mark.skipif(os.name!='nt',reason='Windows real process')
def test_sqlite_lock_stops_guardian_instead_of_false_heartbeat(tmp_path):
    import sqlite3
    c=harness.configuration(tmp_path);s=GuardianStore(control_root(c));s.intent('stopped')
    child=start(tmp_path)
    locker=None
    try:
        wait(lambda:guardian_status(c)['guardian']['healthy'])
        locker=sqlite3.connect(s.path);locker.execute('BEGIN IMMEDIATE')
        child.wait(timeout=12)
        assert child.returncode!=0
        assert not guardian_status(c)['guardian']['healthy']
    finally:
        if locker:locker.rollback();locker.close()
        s.intent('exited')
        if child.poll() is None:child.terminate();child.wait(timeout=5)
        s.close()


@pytest.mark.skipif(os.name!='nt',reason='Windows worker handle recovery')
def test_explicit_local_worker_hang_recovery(tmp_path):
    from progress_wx.guardian import recover_worker
    c=harness.configuration(tmp_path);s=GuardianStore(control_root(c));s.intent('running')
    child=start(tmp_path)
    try:
        old=wait(lambda:(v if (v:=guardian_status(c))['worker']['ready'] else None))['worker']
        (tmp_path/'block-worker').touch()
        wait(lambda:guardian_status(c)['worker']['state']=='unresponsive',timeout=35)
        assert guardian_status(c)['guardian']['healthy']
        assert recover_worker(c,old['pid'],old['creation_time']+1)==2
        (tmp_path/'block-worker').unlink()
        assert recover_worker(c,old['pid'],old['creation_time'])==0
        new=guardian_status(c)['worker']
        assert new['ready'] and new['pid']!=old['pid']
    finally:
        s.intent('exited')
        try:child.wait(timeout=10)
        except subprocess.TimeoutExpired:child.terminate();child.wait(timeout=5)
        s.close()
