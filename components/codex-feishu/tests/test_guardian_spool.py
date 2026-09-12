from dataclasses import replace
import hashlib
import json
import sqlite3
import threading
import time

import pytest

from progress_wx.channel import ChannelReply,ChannelAttachment
from progress_wx.guardian import Guardian
from progress_wx.guardian_spool import InboundSpool
from test_guardian import config,FakeChannel

def test_database_failure_spools_and_replays_original_event(tmp_path,monkeypatch):
    g=Guardian(config(tmp_path),FakeChannel())
    message=ChannelReply('synthetic-owner','hello',message_id='original-id',chat_id='private',reply_to_message_id='parent')
    def fail(*args):raise sqlite3.OperationalError('synthetic lock')
    original=g.store.receive
    monkeypatch.setattr(g.store,'receive',fail)
    with pytest.raises(sqlite3.OperationalError):g.receive(message)
    g.inbound_failure(message,sqlite3.OperationalError('synthetic lock'))
    assert g.stop.is_set()
    assert len(list(g.inbound_spool.root.glob('*.json')))==1
    g.inbound_spool.replay('synthetic-owner',tmp_path/'feishu-media',original,lambda *args:pytest.fail('unexpected rejection'))
    row=g.store.inbound('new-generation')
    assert row['key']=='original-id' and json.loads(row['payload'])['reply_to_message_id']=='parent'
    assert not list(g.inbound_spool.root.glob('*.json'))
    g.store.close()

def test_spool_attachment_validation_and_rejection_is_durable(tmp_path):
    media=tmp_path/'feishu-media';media.mkdir();file=media/'image.png';file.write_bytes(b'synthetic')
    s=InboundSpool(tmp_path/'private')
    m=ChannelReply('owner','text',message_id='image',chat_id='chat',attachments=(ChannelAttachment(str(file),'image/png',hashlib.sha256(b'synthetic').hexdigest(),9),))
    s.save(m);s.save(m)
    file.write_bytes(b'changed!!')
    with pytest.raises(OSError):
        s.replay('owner',media,lambda *args:pytest.fail('invalid attachment accepted'),lambda *args:(_ for _ in ()).throw(OSError('notice not durable')))
    assert len(list(s.root.glob('*.json')))==1
    rejected=[];s.replay('owner',media,lambda *args:pytest.fail('invalid attachment accepted'),lambda *args:rejected.append(args))
    assert len(rejected)==1 and not list(s.root.glob('*.json'))

def test_spool_conflict_full_disk_are_not_success(tmp_path,monkeypatch):
    g=Guardian(config(tmp_path),FakeChannel())
    message=ChannelReply('synthetic-owner','hello',message_id='id',chat_id='private')
    g.inbound_spool.save(message)
    with pytest.raises(ValueError):g.inbound_spool.save(replace(message,content='different'))
    monkeypatch.setattr(g.inbound_spool,'save',lambda *args:(_ for _ in ()).throw(OSError('synthetic disk full')))
    with pytest.raises(OSError):g.inbound_failure(message,RuntimeError())
    assert g.stop.is_set()
    g.store.close()

def test_stale_start_notification_not_sent_after_stop(tmp_path):
    channel=FakeChannel();g=Guardian(config(tmp_path),channel)
    g.store.intent('stopped');g.system('ready:old','synthetic ready')
    thread=threading.Thread(target=g._send_loop);thread.start()
    try:
        deadline=time.monotonic()+3
        while time.monotonic()<deadline and g.store.outcome('system:ready:old')['state']!='superseded':time.sleep(.05)
        assert g.store.outcome('system:ready:old')['state']=='superseded' and not channel.sent
    finally:
        g.stop.set();thread.join(2);g.store.close()
