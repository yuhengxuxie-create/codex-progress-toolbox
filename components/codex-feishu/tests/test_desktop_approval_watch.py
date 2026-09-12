import sqlite3
import pytest
from progress_wx.desktop_approval_watch import DesktopApprovalWatch
from progress_wx.codex_store import ThreadRecord

def snapshot(status=None,turn='turn',items=None):
 return {'thread':{'id':'thread','status':status if status is not None else {'type':'active','activeFlags':['waitingOnApproval']}},'turns':[{'id':turn,'status':'inProgress','items':items or []}]}
def observe(w,s):return w.observe(s,'thread',ThreadRecord('thread',title='Task'))

@pytest.mark.parametrize('status',['waitingOnApproval',{'type':'waiting_on_approval'},{'type':'active','activeFlags':['waiting-on-approval']},{'type':'active','activeFlags':[{'type':'waitingOnApproval'}]}])
def test_waiting_shapes_are_structural(tmp_path,status):
 e=observe(DesktopApprovalWatch(tmp_path/'state.sqlite'),snapshot(status));assert e.status=='waitingOnApproval';assert '不能代为批准' in e.final_message

def test_restart_cursor_and_parallel_tool_changes_do_not_repeat(tmp_path):
 path=tmp_path/'state.sqlite';w=DesktopApprovalWatch(path)
 s=snapshot(items=[{'id':'ordinary','type':'commandExecution','status':'inProgress'}])
 first=observe(w,s);second=observe(DesktopApprovalWatch(path),snapshot())
 assert first.dedupe_key==second.dedupe_key

@pytest.mark.parametrize('status',[{},'notLoaded',{'type':'active'},{'type':'unknown'},{'type':'active','activeFlags':['unknownFlag']}])
def test_unknown_state_does_not_resolve_pending(tmp_path,status):
 w=DesktopApprovalWatch(tmp_path/'state.sqlite');first=observe(w,snapshot())
 assert observe(w,snapshot(status)) is None
 assert observe(w,snapshot()).dedupe_key==first.dedupe_key

def test_explicit_resolution_and_new_approval_are_separate(tmp_path):
 w=DesktopApprovalWatch(tmp_path/'state.sqlite');first=observe(w,snapshot())
 assert observe(w,snapshot({'type':'active','activeFlags':[]})) is None
 assert observe(w,snapshot()).dedupe_key!=first.dedupe_key

def test_marked_pending_item_identity_changes_in_same_turn(tmp_path):
 w=DesktopApprovalWatch(tmp_path/'state.sqlite')
 one=observe(w,snapshot(items=[{'id':'approval-a','type':'commandExecution','status':'waitingOnApproval'}]))
 two=observe(w,snapshot(items=[{'id':'approval-b','type':'commandExecution','status':'waitingOnApproval'}]))
 assert one.dedupe_key!=two.dedupe_key

def test_stale_projection_does_not_resolve_or_send(tmp_path):
 w=DesktopApprovalWatch(tmp_path/'state.sqlite');first=observe(w,snapshot())
 with pytest.raises(ValueError,match='stale_turn'):w.observe(snapshot({'type':'idle'},turn='old'),'thread',None,expected_turn_id='turn')
 assert observe(w,snapshot()).dedupe_key==first.dedupe_key
