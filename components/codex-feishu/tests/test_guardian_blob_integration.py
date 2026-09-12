import json
import threading
import time

from progress_wx.guardian import Guardian
from progress_wx.guardian_channel import GuardianChannel
from progress_wx.guardian_blob import GuardianBlobs
from progress_wx.guardian_store import GuardianStore
from progress_wx.feishu import FeishuSendError
from test_guardian import FakeChannel,config

def test_shared_blob_done_does_not_delete_pending_or_uncertain(tmp_path):
    s=GuardianStore(tmp_path/'guardian');b=GuardianBlobs(tmp_path/'guardian/media-blobs')
    ref=b.put(b'x'*(2*1024*1024))
    for key in ('first','second'):s.enqueue(key,'send_image',{'blob_ref':ref})
    s.claim();s.finish('first','done',result='synthetic')
    s.cleanup_blobs(b,terminal_names=(ref['name'],))
    assert (b.root/ref['name']).is_file()
    assert s.claim()['key']=='second'
    s.recover()
    assert s.outcome('second')['state']=='uncertain'
    s.cleanup_blobs(b,terminal_names=(ref['name'],))
    assert (b.root/ref['name']).is_file() and s.claim() is None
    assert json.loads(s.db.execute("SELECT payload FROM outgoing WHERE key='second'").fetchone()[0])['blob_ref']==ref
    s.close()

def test_large_proxy_payload_uses_blob_and_cleans_after_success(tmp_path):
    class Channel(FakeChannel):
        def send_image(self,data,*,idempotency_key):
            assert len(data)==2*1024*1024
            return 'sent'
    c=config(tmp_path);g=Guardian(c,Channel());p=GuardianChannel(c,'g')
    p.store.put('guardian',{'heartbeat_at':time.time()});p.store.put('channel',{'online':True})
    thread=threading.Thread(target=g._send_loop);thread.start()
    try:
        assert p.send_image(b'x'*(2*1024*1024),idempotency_key='large')=='sent'
        deadline=time.monotonic()+2
        while list(g.blobs.root.glob('*.blob')) and time.monotonic()<deadline:time.sleep(.02)
        assert not list(g.blobs.root.glob('*.blob'))
        assert g.store.db.execute("SELECT length(payload) FROM outgoing WHERE key='large'").fetchone()[0]==2
    finally:g.stop.set();thread.join(2);g.store.close();p.store.close()

def test_unknown_send_retains_blob_and_never_reclaims(tmp_path):
    class Channel(FakeChannel):
        def send_image(self,data,*,idempotency_key):raise FeishuSendError('synthetic unknown')
    g=Guardian(config(tmp_path),Channel())
    ref=g.blobs.put(b'x'*(2*1024*1024));g.store.enqueue('unknown','send_image',{'blob_ref':ref})
    thread=threading.Thread(target=g._send_loop);thread.start()
    try:
        deadline=time.monotonic()+2
        while g.store.outcome('unknown')['state']!='uncertain' and time.monotonic()<deadline:time.sleep(.02)
        assert g.store.outcome('unknown')['state']=='uncertain'
        assert (g.blobs.root/ref['name']).exists() and g.store.claim() is None
    finally:g.stop.set();thread.join(2);g.store.close()
