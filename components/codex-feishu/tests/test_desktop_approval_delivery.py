from types import SimpleNamespace
from progress_wx.service import ProgressService
from progress_wx.desktop_approval_watch import DesktopApprovalWatch
from progress_wx.state import StateStore
from progress_wx.guardian_store import GuardianStore
from progress_wx.state import CorrelationCodec
from test_service import make_config
from test_desktop_approval_watch import snapshot,observe
import progress_wx.service as service_module
from progress_wx.codex_store import ThreadRecord

def setup(tmp_path):
 config=make_config(tmp_path);store=StateStore(config.service.database)
 transport=GuardianStore(tmp_path/'guardian')
 service=ProgressService(config.path);service.config=config;service.store=store;service.codec=CorrelationCodec(b't'*32)
 service.channel=SimpleNamespace(is_guardian_proxy=True,store=transport)
 event=observe(DesktopApprovalWatch(tmp_path/'watch.sqlite'),snapshot())
 return service,store,transport,event

def test_pending_restart_and_success_are_exactly_once(tmp_path):
 s,db,t,e=setup(tmp_path)
 try:
  s._sync_desktop_approval_notice(e.dedupe_key,e);s._sync_desktop_approval_notice(e.dedupe_key,e)
  assert t.db.execute('SELECT count(*) FROM outgoing').fetchone()[0]==1
  assert not db.notification_sent(e.dedupe_key)
  t.finish('notification:'+e.dedupe_key,'done',result=['test-message'])
  s._sync_desktop_approval_notice(e.dedupe_key,e);s._sync_desktop_approval_notice(e.dedupe_key,e)
  assert db.notification_sent(e.dedupe_key);assert db.was_processed(e.dedupe_key)
  assert t.db.execute('SELECT count(*) FROM outgoing').fetchone()[0]==1
  assert db._connection.execute('SELECT reply_kind FROM notifications').fetchone()[0]=='notice'
 finally:db.close();t.close()

def test_resolved_cancels_only_unsubmitted_and_keeps_evidence(tmp_path):
 s,db,t,e=setup(tmp_path)
 try:
  s._sync_desktop_approval_notice(e.dedupe_key,e);s._sync_desktop_approval_notice(e.dedupe_key)
  assert t.outcome('notification:'+e.dedupe_key)['state']=='superseded'
  assert not db.notification_sent(e.dedupe_key)
 finally:db.close();t.close()

def test_platform_unknown_is_not_sent_or_reenqueued(tmp_path):
 s,db,t,e=setup(tmp_path)
 try:
  s._sync_desktop_approval_notice(e.dedupe_key,e)
  t.finish('notification:'+e.dedupe_key,'uncertain',error='synthetic_unknown')
  s._sync_desktop_approval_notice(e.dedupe_key,e);s._sync_desktop_approval_notice(e.dedupe_key)
  assert t.outcome('notification:'+e.dedupe_key)['state']=='uncertain'
  assert not db.notification_sent(e.dedupe_key)
 finally:db.close();t.close()

class StopAfterCycle:
 def __init__(self):self.stopped=False;self.set_calls=0
 def is_set(self):return self.stopped
 def wait(self,_seconds):self.stopped=True;return True
 def set(self):self.set_calls+=1;self.stopped=True

def prepare_worker(s,monkeypatch,snapshots):
 class Session:
  def __init__(self):self.closed=False;self.calls=0
  def read_thread(self,*args,**kwargs):
   self.calls+=1;return snapshots[min(self.calls-1,len(snapshots)-1)]
  def close(self):self.closed=True
 session=Session()
 class Client:
  def __init__(self,*a,**kw):assert kw['response_timeout']==3
  def open_verified(self,**kwargs):assert kwargs['required_tools']==('read_thread',);return session
 monkeypatch.setattr(service_module,'DesktopAppToolsClient',Client)
 s._selected_threads=lambda config:{'thread':ThreadRecord('thread')}
 s.codex_store=SimpleNamespace(latest_turn=lambda target:None)
 s.stop_event=StopAfterCycle()
 return session

def test_no_list_api_needed_and_resolved_presend_is_suppressed(tmp_path,monkeypatch):
 s,db,t,e=setup(tmp_path)
 try:
  session=prepare_worker(s,monkeypatch,[snapshot(),snapshot({'type':'active','activeFlags':[]})])
  s._desktop_approval_worker()
  assert session.calls==2
  assert t.db.execute('SELECT count(*) FROM outgoing').fetchone()[0]==0
  assert s._fatal is None and s.stop_event.set_calls==0
 finally:db.close();t.close()

def test_delivery_exception_isolated_from_service_stop(tmp_path,monkeypatch):
 s,db,t,e=setup(tmp_path)
 try:
  session=prepare_worker(s,monkeypatch,[snapshot()])
  calls=[]
  def failing(*args):calls.append(args);raise RuntimeError('synthetic queue failure')
  s._sync_desktop_approval_notice=failing
  s._desktop_approval_worker()
  assert len(calls)==1 and session.closed
  assert s._fatal is None and s.stop_event.set_calls==0
 finally:db.close();t.close()

def test_live_signed_hook_owner_suppresses_fallback(tmp_path):
 s,db,t,e=setup(tmp_path)
 try:
  s.approval_bridge=SimpleNamespace(pending=lambda:[SimpleNamespace(session_id=e.thread_id,turn_id=e.raw['actual_turn_id'])])
  s._sync_desktop_approval_notice(e.dedupe_key,e)
  assert t.db.execute('SELECT count(*) FROM outgoing').fetchone()[0]==0
  s.approval_bridge=SimpleNamespace(pending=lambda:[])
  s._sync_desktop_approval_notice(e.dedupe_key,e)
  assert t.db.execute('SELECT count(*) FROM outgoing').fetchone()[0]==1
 finally:db.close();t.close()

def test_idle_scan_caches_registry_and_never_reads_full_local_turn(tmp_path,monkeypatch):
 s,db,t,e=setup(tmp_path)
 try:
  prepare_worker(s,monkeypatch,[snapshot({'type':'idle'})])
  selections=[]
  def selected(config):selections.append(1);return {'thread':ThreadRecord('thread')}
  def forbidden(target):raise AssertionError('idle target must not scan full local turn')
  s._selected_threads=selected;s.codex_store=SimpleNamespace(latest_turn=forbidden)
  class StopAfterThree(StopAfterCycle):
   def __init__(self):super().__init__();self.waits=0
   def wait(self,_seconds):self.waits+=1;self.stopped=self.waits>=3;return self.stopped
  s.stop_event=StopAfterThree();s._desktop_approval_worker()
  assert len(selections)==1 and s.stop_event.waits==3
  assert s.stop_event.set_calls==0
 finally:db.close();t.close()

def test_registry_refreshes_after_fifteen_seconds(tmp_path,monkeypatch):
 s,db,t,e=setup(tmp_path)
 try:
  prepare_worker(s,monkeypatch,[snapshot({'type':'idle'})])
  calls=[]
  s._selected_threads=lambda config:(calls.append(1) or {'thread':ThreadRecord('thread')})
  times=iter([0,0,10,16,16])
  monkeypatch.setattr(service_module,'time',SimpleNamespace(monotonic=lambda:next(times)))
  class StopAfterThree(StopAfterCycle):
   def __init__(self):super().__init__();self.waits=0
   def wait(self,_seconds):self.waits+=1;self.stopped=self.waits>=3;return self.stopped
  s.stop_event=StopAfterThree();s._desktop_approval_worker()
  assert len(calls)==2
 finally:db.close();t.close()
