"""Bounded private emergency intake when the transport database is unavailable."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import threading
import time

from .guardian_store import MAX_PAYLOAD, private_directory

class InboundSpool:
    def __init__(self,root):
        self.root=Path(root)/'inbound-recovery'
        private_directory(self.root)
        self.lock=threading.RLock()

    def save(self,message):
        payload=asdict(message)
        raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
        if len(raw)>MAX_PAYLOAD:raise BufferError('inbound_recovery_payload_limit')
        record=json.dumps(dict(payload=payload,digest=hashlib.sha256(raw).hexdigest(),saved_at=time.time()),ensure_ascii=False).encode('utf-8')
        target=self.root/(hashlib.sha256(message.message_id.encode()).hexdigest()+'.json')
        with self.lock:
            if target.exists():
                previous=json.loads(target.read_text(encoding='utf-8'))
                if previous['digest']!=hashlib.sha256(raw).hexdigest():raise ValueError('inbound_recovery_conflict')
                return
            files=list(self.root.iterdir())
            if len(files)>=128 or sum(f.stat().st_size for f in files)+len(record)>64*1024*1024:
                raise BufferError('inbound_recovery_full')
            temporary=target.with_suffix('.pending')
            with temporary.open('xb') as handle:
                handle.write(record);handle.flush();os.fsync(handle.fileno())
            os.replace(temporary,target)

    def replay(self,owner,media_root,accept,reject):
        with self.lock:
            for path in sorted(p for p in self.root.iterdir() if p.suffix in {'.json','.pending'})[:16]:
                reason=''
                try:
                    if path.is_symlink() or path.stat().st_size>MAX_PAYLOAD+4096:raise ValueError('invalid_record')
                    record=json.loads(path.read_text(encoding='utf-8'));p=record['payload']
                    raw=json.dumps(p,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
                    if hashlib.sha256(raw).hexdigest()!=record['digest']:raise ValueError('invalid_digest')
                    if p.get('sender_id')!=owner or not p.get('message_id') or (not p.get('chat_id') and p.get('source_kind')!='bot_menu'):raise ValueError('invalid_scope')
                    if path.stem!=hashlib.sha256(p['message_id'].encode()).hexdigest():raise ValueError('invalid_key')
                    if not -10<=time.time()-record['saved_at']<=86400:raise ValueError('expired')
                    for attachment in p.get('attachments',[]):
                        file=Path(attachment['path']);root=Path(media_root).resolve()
                        if not file.resolve().is_relative_to(root) or any(x.is_symlink() or (hasattr(x,'is_junction') and x.is_junction()) for x in (file,*file.parents)):raise ValueError('attachment_scope')
                        if not 0<attachment['size']<=20*1024*1024 or file.stat().st_size!=attachment['size']:raise ValueError('attachment_size')
                        if hashlib.sha256(file.read_bytes()).hexdigest()!=attachment['sha256']:raise ValueError('attachment_digest')
                except (ValueError,KeyError,TypeError,OSError) as exc:
                    reason=type(exc).__name__
                if reason:
                    reject(path.stem,reason)
                else:
                    accept(p['message_id'],p)
                # Delete only after durable accept or durable rejection notice.
                path.unlink()
