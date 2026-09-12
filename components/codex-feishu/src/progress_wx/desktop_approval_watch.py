"""Fresh Desktop approval observations, not an approval execution channel."""
import hashlib
import json
import sqlite3
import time
import uuid
from .models import TurnEvent

def _normal(value):
    return ''.join(c for c in str(value).casefold() if c.isalnum())

def is_approval(status):
    if isinstance(status,str):
        value=_normal(status)
        return True if value=='waitingonapproval' else False if value in {'idle','completed','failed','interrupted'} else None
    if not isinstance(status,dict):return None
    if _normal(status.get('type'))=='waitingonapproval':return True
    if _normal(status.get('type')) in {'idle','completed','failed','interrupted'}:return False
    if _normal(status.get('type'))!='active' or not isinstance(status.get('activeFlags'),list):return None
    flags=[_normal(f.get('type',f.get('name','')) if isinstance(f,dict) else f) for f in status['activeFlags']]
    if 'waitingonapproval' in flags:return True
    return False if all(f in {'waitingonuserinput'} for f in flags) else None

class DesktopApprovalWatch:
    def __init__(self,path):
        self.path=path
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS observations(thread_id TEXT PRIMARY KEY,turn_id TEXT,anchor TEXT,episode TEXT,state TEXT,updated REAL)')

    def resolved_event_key(self,thread_id):
        with sqlite3.connect(self.path) as db:
            row=db.execute("SELECT turn_id,episode FROM observations WHERE thread_id=? AND state='resolved'",(thread_id,)).fetchone()
        return f'{thread_id}:{row[0]}@desktop-approval-{row[1]}:waitingOnApproval' if row else None

    def has_pending(self,thread_id):
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT 1 FROM observations WHERE thread_id=? AND state='pending'",(thread_id,)).fetchone() is not None

    def observe(self,snapshot,expected_thread,record,expected_turn_id=None):
        thread=snapshot.get('thread')
        if not isinstance(thread,dict) or thread.get('id')!=expected_thread:raise ValueError('desktop_approval_thread_mismatch')
        if thread.get('status') is None:return None
        pending=is_approval(thread['status'])
        if pending is None:return None
        turns=snapshot.get('turns')
        # read_thread returns newest first. Never use an old wait_threads turn.
        turn=turns[0] if isinstance(turns,list) and turns and isinstance(turns[0],dict) else None
        if expected_turn_id and (not turn or turn.get('id')!=expected_turn_id):raise ValueError('desktop_approval_stale_turn')
        with sqlite3.connect(self.path) as db:
            if not pending:
                db.execute("UPDATE observations SET state='resolved',updated=? WHERE thread_id=?",(time.time(),expected_thread));return None
            if not turn or not turn.get('id') or turn.get('status') in {'completed','failed','interrupted'}:return None
            turn_id=str(turn['id'])
            items=turn.get('items',[])
            anchors=sorted(str(i['id']) for i in items if isinstance(i,dict) and i.get('id') and i.get('type') in {'commandExecution','fileChange','mcpToolCall','dynamicToolCall'} and _normal(i.get('status')) in {'waitingonapproval','pendingapproval'})
            anchor=hashlib.sha256(json.dumps(anchors).encode()).hexdigest() if anchors else ''
            row=db.execute('SELECT turn_id,anchor,episode,state FROM observations WHERE thread_id=?',(expected_thread,)).fetchone()
            episode=row[2] if row and row[0]==turn_id and row[1]==anchor and row[3]=='pending' else uuid.uuid4().hex
            db.execute('INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?)',(expected_thread,turn_id,anchor,episode,'pending',time.time()))
        return TurnEvent(thread_id=expected_thread,turn_id=turn_id+'@desktop-approval-'+episode,status='waitingOnApproval',title=record.title if record else '',cwd=record.cwd if record else '',source='codex-desktop-approval-observation',final_message='Codex 当前正在等待你批准操作。请在电脑上打开此任务，核对审批框后选择允许或拒绝。此提醒不能代为批准；引用回复 A 不会执行审批。',raw={'actual_turn_id':turn_id,'waiting_identity_kind':'approval_marked_item_ids' if anchors else 'observed_episode','waiting_identity':anchor or episode})
