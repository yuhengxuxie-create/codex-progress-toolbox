"""Worker-side channel proxy. It never owns a Feishu socket or app secret."""
from __future__ import annotations
import base64
import json
import os
import threading
import time
import uuid
from .channel import ChannelReply, ChannelAttachment, MessageChannelOfflineError
from .feishu import FeishuSendError, FeishuSendNotSubmittedError, FeishuSendRejectedError
from .guardian import control_root, guardian_status
from .guardian_store import GuardianStore
from .process_control import process_creation_time


class GuardianChannel:
    is_guardian_proxy = True
    def __init__(self,config,generation,*,passive=False):
        self.config=config
        self.generation=generation
        self.store=GuardianStore(control_root(config))
        from .guardian_blob import GuardianBlobs
        self.blobs=GuardianBlobs(control_root(config)/'media-blobs')
        identity=self.store.get('channel_identity')
        if identity is not None and identity!={'app_id':getattr(config.feishu,'app_id',''),'owner':config.feishu.target_open_id}:
            self.store.close()
            raise RuntimeError('guardian_channel_identity_changed')
        self.stop_event=threading.Event()
        self.receiver=None
        self.passive=passive
        self.error_handler=lambda exc: None

    def is_online(self):
        try:
            g=self.store.get('guardian',{})
            return bool(g.get('heartbeat_at') and -10<=time.time()-g['heartbeat_at']<=15 and self.store.get('channel',{}).get('online'))
        except Exception:
            return False

    def connection_snapshot(self):
        online=self.is_online()
        return dict(state='online' if online else 'reconnecting',ever_connected=True,consecutive_failures=0,
                    last_failure_class='',last_failure_type='',retry_in_seconds=1,last_transition_at=time.time())

    def heartbeat(self,*,ready=True):
        record=self.store.get('worker',{})
        if record.get('generation') != self.generation:
            raise RuntimeError('worker_generation_replaced')
        if self.store.get('desired_state') != 'running':
            return False
        record.update(pid=os.getpid(),creation_time=process_creation_time(os.getpid()),heartbeat_at=time.time(),ready=ready,state='ready' if ready else 'starting')
        self.store.put('worker',record)
        return True

    def start(self,on_reply):
        if self.passive:
            return
        def receive():
            ack_cursor_created=0.0
            ack_cursor_key=''
            try:
                while not self.stop_event.wait(.1):
                    owner=getattr(on_reply,'__self__',None)
                    durable_status=getattr(owner,'guardian_inbound_status',None)
                    if callable(durable_status):
                        with self.store.lock:
                            cutoff=time.time()-86400
                            handed=self.store.db.execute('SELECT key,created FROM incoming WHERE generation=? AND acknowledged_at IS NULL AND created>? AND (created>? OR (created=? AND key>?)) ORDER BY created,key LIMIT 32',(self.generation,cutoff,ack_cursor_created,ack_cursor_created,ack_cursor_key)).fetchall()
                            if not handed:
                                # Complete one bounded pass, then revisit the
                                # first page.  This avoids starving later
                                # events while still retrying an early pending
                                # row without an unbounded table scan.
                                ack_cursor_created=0.0
                                ack_cursor_key=''
                                handed=self.store.db.execute('SELECT key,created FROM incoming WHERE generation=? AND acknowledged_at IS NULL AND created>? ORDER BY created,key LIMIT 32',(self.generation,cutoff)).fetchall()
                            if handed:
                                tail=handed[-1]
                                ack_cursor_created=float(tail['created'])
                                ack_cursor_key=str(tail['key'])
                        for old in handed:
                            if durable_status(old['key']) in {'accepted','rejected'}:
                                self.store.acknowledge(old['key'])
                    row=self.store.inbound(self.generation)
                    if not row:
                        continue
                    payload=json.loads(row['payload'])
                    # Reserved system command is never routed into task processing.
                    if payload.get('content')=='.启动飞书机器人':
                        self.stop_event.wait(.2)
                        continue
                    payload['attachments']=tuple(ChannelAttachment(**a) for a in payload.get('attachments',[]))
                    accepted=on_reply(ChannelReply(**payload))
                    if accepted is True:
                        self.store.acknowledge(row['key'])
                    # Retain payload; after a worker crash the same event is redelivered.
                    self.store.accepted(row['key'],self.generation)
            except Exception as exc:
                self.error_handler(exc)
        self.receiver=threading.Thread(target=receive,name='guardian-incoming',daemon=True)
        self.receiver.start()

    def _call(self,method,payload,key=None,timeout=100):
        key=key or 'read:'+uuid.uuid4().hex
        if not self.is_online():
            raise FeishuSendNotSubmittedError('guardian not online')
        try:
            data=payload.pop('data',None)
            if data is not None and len(data)>1024*1024:
                # DB -> blob lock order serializes enqueue with terminal cleanup.
                with self.store.lock:
                    self.store.db.execute('BEGIN IMMEDIATE')
                    try:
                        payload['blob_ref']=self.blobs.put(data)
                        self.store.enqueue(key,method,payload)
                    except BaseException:
                        self.store.db.rollback()
                        raise
            else:
                if data is not None:payload['data_b64']=base64.b64encode(data).decode()
                self.store.enqueue(key,method,payload)
        except BufferError as exc:
            raise FeishuSendNotSubmittedError('guardian queue full') from exc
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline and not self.stop_event.is_set():
            result=self.store.outcome(key)
            if result and result['state']=='done':
                return json.loads(result['result'])
            if result and result['state']=='rejected':
                detail=json.loads(result.get('result') or 'null') or {}
                raise FeishuSendRejectedError(code=detail.get('code') or result.get('error') or 'guardian_rejected',raw_code=detail.get('raw_code'),retryable=bool(detail.get('retryable',False)))
            if result and result['state']=='uncertain':
                raise FeishuSendError('guardian submission outcome unknown')
            self.stop_event.wait(.1)
        # Atomically cancel only if no platform submission has begun.
        with self.store.lock,self.store.db:
            cancelled=self.store.db.execute("UPDATE outgoing SET state='cancelled',error='client_timeout_before_submit' WHERE key=? AND state='pending'",(key,)).rowcount
        if cancelled:
            raise FeishuSendNotSubmittedError('guardian request timed out before submit')
        raise FeishuSendError('guardian request timed out after possible submit')

    def send_text(self,text,*,idempotency_key):
        return self._call('send_text',{'text':text},idempotency_key)
    def send_card(self,card,*,idempotency_key):
        return self._call('send_card',{'card':card},idempotency_key)
    def send_image(self,data,*,idempotency_key):
        return self._call('send_image',{'data':data},idempotency_key)
    def send_file(self,data,*,file_name,idempotency_key):
        return self._call('send_file',{'data':data,'file_name':file_name},idempotency_key)
    def fetch_message(self,message_id):
        return self._call('fetch_message',{'message_id':message_id})
    def bot_sender_ids(self):
        return tuple(self._call('bot_sender_ids',{}))
    def recipient_scope_for_messages(self,message_ids):
        value=self._call('recipient_scope_for_messages',{'message_ids':list(message_ids)})
        return tuple(value) if value else None
    def stop(self):
        self.stop_event.set()
        if self.receiver and self.receiver is not threading.current_thread():
            self.receiver.join(timeout=2)
        # Concurrent business send threads may still inspect this connection until shutdown.
