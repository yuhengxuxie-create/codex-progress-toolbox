"""Private, bounded JSON IPC and durable transport state (no pickle or network listener)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
import ctypes

MAX_PAYLOAD = 32 * 1024 * 1024
MAX_QUEUE = 128
MAX_PENDING_BYTES = 256 * 1024 * 1024


def private_directory(path: Path) -> None:
    path = Path(path).absolute()
    if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in (path, *path.parents)):
        raise ValueError('guardian private path cannot contain reparse links')
    path.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        # New children inherit this DACL. No credentials are read or printed.
        result = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True, check=True, text=True,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        import csv
        sid = next(csv.reader(result.stdout.splitlines()))[1]
        if not sid.startswith('S-1-5-'):
            raise ValueError('invalid Windows owner SID')
        from ctypes import wintypes
        advapi = ctypes.WinDLL('advapi32',use_last_error=True)
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,ctypes.POINTER(ctypes.c_void_p),ctypes.c_void_p]
        advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,ctypes.c_void_p]
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        # Replace the complete DACL; grant:r alone would retain foreign explicit ACEs.
        descriptor = ctypes.c_void_p()
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(f'D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)',1,ctypes.byref(descriptor),None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            for item in (path,*path.rglob('*')):
                if item.is_symlink() or (hasattr(item,'is_junction') and item.is_junction()):
                    raise ValueError('guardian child reparse link')
                if not advapi.SetFileSecurityW(str(item),0x80000004,descriptor):
                    raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.LocalFree(descriptor)
    else:
        path.chmod(0o700)


class GuardianStore:
    def __init__(self, root: Path, *, readonly: bool = False):
        self.root = Path(root)
        self.path = self.root / 'transport.sqlite'
        self.readonly = readonly
        if readonly:
            self.db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2, check_same_thread=False)
        else:
            private_directory(self.root)
            self.db = sqlite3.connect(self.path, timeout=2, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        if not readonly:
            self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA max_page_count=131072;
            CREATE TABLE IF NOT EXISTS control(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outgoing(
              key TEXT PRIMARY KEY,digest TEXT NOT NULL,method TEXT NOT NULL,payload TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'pending',result TEXT,error TEXT,created REAL NOT NULL,
              updated REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_at REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS incoming(
              key TEXT PRIMARY KEY,payload TEXT NOT NULL,created REAL NOT NULL,generation TEXT NOT NULL DEFAULT '',acknowledged_at REAL);
            CREATE TABLE IF NOT EXISTS commands(key TEXT PRIMARY KEY,created REAL NOT NULL,result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS transitions(key TEXT PRIMARY KEY,kind TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS system_checked(key TEXT PRIMARY KEY,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS file_uploads(
             kind TEXT NOT NULL,key TEXT NOT NULL,sha256 TEXT NOT NULL,filename TEXT NOT NULL,
             file_key TEXT NOT NULL,created REAL NOT NULL,
             PRIMARY KEY(kind,key,sha256,filename));
            ''')
            if 'acknowledged_at' not in {row[1] for row in self.db.execute('PRAGMA table_info(incoming)')}:
                self.db.execute('ALTER TABLE incoming ADD COLUMN acknowledged_at REAL')
            self.db.commit()

    def lookup_media_key(self,kind,key,sha256,filename):
        with self.lock:
            row=self.db.execute("SELECT file_key FROM file_uploads WHERE kind=? AND key=? AND sha256=? AND filename=?",(kind,key,sha256,filename)).fetchone()
            return row[0] if row else None

    def store_media_key(self,kind,key,sha256,filename,file_key):
        with self.lock,self.db:
            self.db.execute("INSERT OR IGNORE INTO file_uploads VALUES(?,?,?,?,?,?)",(kind,key,sha256,filename,file_key,time.time()))
            if self.lookup_media_key(kind,key,sha256,filename)!=file_key:
                raise ValueError('uploaded_media_key_conflict')

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute('SELECT value FROM control WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO control VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def intent(self, desired):
        if desired not in {'running', 'stopped', 'maintenance', 'exited'}:
            raise ValueError('invalid guardian intent')
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                previous = self.get('desired_state', 'stopped')
                if previous == 'maintenance' and desired == 'running':
                    raise ValueError('maintenance active')
                values = {'desired_state':desired,'intent_at':time.time()}
                if desired == 'maintenance' and previous != 'maintenance':
                    values['previous_desired_state'] = previous
                self.db.executemany('INSERT OR REPLACE INTO control VALUES(?,?)',[(key,json.dumps(value)) for key,value in values.items()])
                self.db.commit()
                return values['intent_at']
            except BaseException:
                self.db.rollback()
                raise

    def leave_maintenance(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                if self.get('desired_state') == 'maintenance':
                    self.db.executemany('INSERT OR REPLACE INTO control VALUES(?,?)',
                        [('desired_state',json.dumps(self.get('previous_desired_state','stopped'))),('intent_at',json.dumps(time.time()))])
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def enqueue(self, key, method, payload):
        raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        size = len(raw.encode('utf-8'))
        if not key or len(key) > 512 or size > MAX_PAYLOAD:
            raise ValueError('guardian payload limit')
        digest = hashlib.sha256((method + '\0' + raw).encode()).hexdigest()
        now = time.time()
        with self.lock, self.db:
            row = self.db.execute('SELECT digest,state FROM outgoing WHERE key=?', (key,)).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError('idempotency key payload mismatch')
                if row[1] == 'cancelled':
                    self.db.execute("UPDATE outgoing SET state='pending',error=NULL,next_at=0 WHERE key=?",(key,))
                return
            count, total = self.db.execute("SELECT count(*),coalesce(sum(length(cast(payload as blob))),0) FROM outgoing WHERE state IN ('pending','submitted','cancelled')").fetchone()
            reserve = 16 if key.startswith('system:') else 0
            if count >= MAX_QUEUE+reserve or total + size > MAX_PENDING_BYTES+reserve*4096:
                raise BufferError('guardian outgoing queue full')
            self.db.execute('INSERT INTO outgoing(key,digest,method,payload,created,updated) VALUES(?,?,?,?,?,?)', (key,digest,method,raw,now,now))

    def outcome(self, key):
        with self.lock:
            row = self.db.execute('SELECT state,result,error FROM outgoing WHERE key=?',(key,)).fetchone()
            return dict(row) if row else None

    def claim(self,*,system_only=False):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                row = self.db.execute("SELECT * FROM outgoing WHERE state='pending' AND next_at<=? AND (?=0 OR key LIKE 'system:%') ORDER BY created LIMIT 1",(time.time(),int(system_only))).fetchone()
                if row:
                    self.db.execute("UPDATE outgoing SET state='submitted',updated=?,attempts=attempts+1 WHERE key=?",(time.time(),row['key']))
                self.db.commit()
                return dict(row) if row else None
            except BaseException:
                self.db.rollback()
                raise

    def finish(self, key, state, *, result=None, error=None):
        if state not in {'done','rejected','uncertain','pending','superseded'}:
            raise ValueError('invalid delivery state')
        with self.lock, self.db:
            self.db.execute("UPDATE outgoing SET state=?,result=?,error=?,updated=?,next_at=?,payload=CASE WHEN ?='pending' OR (?='uncertain' AND json_type(payload,'$.blob_ref')='object') THEN payload ELSE '{}' END WHERE key=?",
                            (state,json.dumps(result,ensure_ascii=False),error,time.time(),time.time()+2,state,state,key))

    def cleanup_blobs(self,blobs,*,terminal_names=()):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                refs={r[0] for r in self.db.execute("SELECT json_extract(payload,'$.blob_ref.name') FROM outgoing WHERE state IN ('pending','submitted','cancelled','uncertain') AND json_type(payload,'$.blob_ref')='object'")}
                blobs.cleanup(refs,terminal_names=terminal_names)
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def coalesce_disconnection(self,key):
        with self.lock,self.db:
            self.db.execute("UPDATE outgoing SET state='superseded',payload='{}',error='connection_restored',updated=? WHERE key=? AND state IN ('pending','cancelled','rejected')",(time.time(),key))

    def recover(self):
        with self.lock, self.db:
            self.db.execute("UPDATE outgoing SET state='uncertain',error='guardian_interrupted_after_submit',payload=CASE WHEN json_type(payload,'$.blob_ref')='object' THEN payload ELSE '{}' END WHERE state='submitted'")

    def receive(self, key, payload):
        raw = json.dumps(payload,ensure_ascii=False)
        if not key or len(raw.encode()) > 128*1024:
            raise ValueError('invalid inbound envelope')
        with self.lock, self.db:
            if self.db.execute('SELECT 1 FROM incoming WHERE key=?',(key,)).fetchone():
                return
            if self.db.execute('SELECT count(*) FROM incoming').fetchone()[0] >= 4096:
                raise BufferError('guardian incoming queue full')
            self.db.execute('INSERT INTO incoming(key,payload,created) VALUES(?,?,?)',(key,raw,time.time()))

    def inbound(self, generation):
        with self.lock:
            row = self.db.execute("SELECT * FROM incoming WHERE generation<>? AND generation<>'system' AND acknowledged_at IS NULL AND created>? ORDER BY created LIMIT 1",(generation,time.time()-86400)).fetchone()
            return dict(row) if row else None

    def handed_unacknowledged(self, generation, *, limit=128):
        """Return events handed to this worker but awaiting durable ACK.

        ``accepted()`` advances an event to the current generation without
        discarding its payload.  The worker can therefore poll its own durable
        business state after an in-memory queue hand-off or management action
        completes, while a new generation still receives every unacknowledged
        event after restart.
        """

        if isinstance(limit, bool) or not 1 <= int(limit) <= 4096:
            raise ValueError('invalid inbound poll limit')
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM incoming "
                "WHERE generation=? AND acknowledged_at IS NULL "
                "AND created>? ORDER BY created LIMIT ?",
                (generation, time.time() - 86400, int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def accepted(self, key, generation):
        with self.lock, self.db:
            self.db.execute('UPDATE incoming SET generation=? WHERE key=?',(generation,key))

    def acknowledge(self,key):
        with self.lock,self.db:
            self.db.execute("UPDATE incoming SET acknowledged_at=?,payload='{}' WHERE key=?",(time.time(),key))

    def command(self, key, result):
        with self.lock, self.db:
            return self.db.execute('INSERT OR IGNORE INTO commands VALUES(?,?,?)',(key,time.time(),result)).rowcount == 1

    def remote_start(self,key):
        """Command receipt and desired intent commit together, including crash recovery."""
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                if self.db.execute('SELECT 1 FROM commands WHERE key=?',(key,)).fetchone():
                    self.db.rollback()
                    return False
                if self.get('desired_state') in {'maintenance','exited'}:
                    self.db.rollback()
                    return False
                self.db.execute('INSERT INTO commands VALUES(?,?,?)',(key,time.time(),'start_requested'))
                self.db.executemany('INSERT OR REPLACE INTO control VALUES(?,?)',
                    [('desired_state',json.dumps('running')),('intent_at',json.dumps(time.time()))])
                self.db.commit()
                return True
            except BaseException:
                self.db.rollback()
                raise

    def transition(self, key, kind):
        with self.lock, self.db:
            return self.db.execute('INSERT OR IGNORE INTO transitions VALUES(?,?,?)',(key,kind,time.time())).rowcount == 1

    def cleanup(self):
        with self.lock:
            unacked=self.db.execute("SELECT count(*),min(created) FROM incoming WHERE acknowledged_at IS NULL AND generation<>'system' AND created<?",(time.time()-86400,)).fetchone()
        if unacked[0]:
            self.enqueue('system:expired:'+str(unacked[1]),'send_text',{'text':f'有 {unacked[0]} 条消息在保留时限内未能确认业务接收，请检查任务状态后重新发送。'})
        with self.lock, self.db:
            # Never delete unresolved outgoing records. Completed keys remain for 30 days.
            cutoff = time.time()-30*86400
            self.db.execute("DELETE FROM outgoing WHERE state IN ('done','rejected','superseded','cancelled') AND updated<?",(cutoff,))
            self.db.execute("DELETE FROM incoming WHERE (acknowledged_at IS NOT NULL OR generation='system') AND created<?",(cutoff,))
            self.db.execute("UPDATE incoming SET acknowledged_at=?,payload='{}' WHERE acknowledged_at IS NULL AND created<?",(time.time(),time.time()-86400))
            self.db.execute('DELETE FROM commands WHERE created<?',(cutoff,))
            self.db.execute('DELETE FROM transitions WHERE created<?',(cutoff,))
            self.db.execute('DELETE FROM system_checked WHERE created<?',(cutoff,))
