"""Durable per-file delivery; transport work never runs on the polling thread."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading
import time

from .guardian_blob import GuardianBlobs, MAX_BLOB_SIZE
from .feishu import FeishuSendNotSubmittedError, FeishuSendRejectedError
from .delivered_files import DeliveredFileCandidate
from .file_validation import inspect_local_file, read_verified_file, identity as file_identity, failure_code

SCHEMA_SQL = ("""CREATE TABLE IF NOT EXISTS artifact_file_deliveries(
 delivery_id TEXT PRIMARY KEY, event_key TEXT NOT NULL, thread_id TEXT NOT NULL,
 turn_id TEXT NOT NULL, title TEXT NOT NULL, candidate_id TEXT NOT NULL,
 file_name TEXT NOT NULL, source_path TEXT NOT NULL, sha256 TEXT NOT NULL,
 media_kind TEXT NOT NULL DEFAULT 'file',
 size INTEGER NOT NULL, source_meta_json TEXT NOT NULL DEFAULT '{}', snapshot_json TEXT, ordinal INTEGER NOT NULL,
 state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
 next_at REAL NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL,
 message_ids_json TEXT NOT NULL DEFAULT '[]',
 notice_state TEXT NOT NULL DEFAULT 'none', notice_reason TEXT NOT NULL DEFAULT '',
 notice_key TEXT NOT NULL DEFAULT '',
 notice_ids_json TEXT NOT NULL DEFAULT '[]', notice_attempts INTEGER NOT NULL DEFAULT 0,
 UNIQUE(event_key,candidate_id))""",
 "CREATE INDEX IF NOT EXISTS artifact_file_due ON artifact_file_deliveries(state,next_at,created,ordinal)")


def failure_description(reason,media_kind="file"):
    if 'image_exceeds' in reason or ('234006' in reason and media_kind=='image'):
        return '图片超过飞书原生图片允许的10MB上限'
    if '234006' in reason or 'exceeds' in reason or 'too_large' in reason:
        return '文件超过飞书允许的30MB上限'
    if '234010' in reason or 'empty' in reason:
        return '飞书不能接收空文件'
    if 'source_changed' in reason:
        return '文件在保存交付副本前已发生变化，请重新交付该文件'
    if reason == 'delivery_without_path':
        return '任务声明已交付，但没有提供准确路径；请让原任务返回实际生成文件的完整路径'
    if reason == 'ambiguous_path_encoding':
        return '路径中的百分号存在编码歧义；请让原任务返回未经改写的准确文件路径'
    if reason == 'local_permission_denied':
        return '本机没有读取该文件的权限；请检查文件访问权限后让原任务重新交付'
    if reason == 'reparse_path':
        return '交付路径包含链接或重解析目录，无法确认原文件身份；请交付准确的普通文件路径'
    if reason == 'not_file' or reason == 'invalid_absolute_path':
        return '交付地址不是准确的本机普通文件路径；请让原任务核对实际生成路径'
    if 'missing' in reason or 'unavailable' in reason or 'FileNotFound' in reason:
        return '交付路径未找到，可能路径写错或文件尚未生成；请让原任务核对实际生成路径'
    if reason == 'local_unreadable' or reason == 'unreadable':
        return '本机暂时无法读取交付文件；请检查文件占用与访问权限后重新交付' 
    if 'snapshot_capture_interrupted' in reason:
        return '保存交付副本时服务中断，请重新交付该文件'
    if 'BufferError' in reason or 'quota' in reason or 'capacity' in reason:
        return '本机待发送文件存储已满，需要处理积压文件'
    if 'unknown' in reason:
        return '平台未返回确定结果，为避免重复发送已暂停该文件'
    if 'permission' in reason or 'forbidden' in reason:
        return '飞书拒绝了此操作，请检查机器人文件权限'
    if 'unverified' in reason or 'remote' in reason or 'sandbox' in reason:
        return '当前文件链接无法对应到可核验的本机文件'
    return '文件处理或平台接收未能完成，可查看文件投递状态了解详情'


class FileDeliveryQueue:
    def __init__(self, store, snapshot_root, channel, bind_messages, *, start=True):
        self.recovery_epoch = time.time()
        self.store, self.channel, self.bind_messages = store, channel, bind_messages
        self.blobs = GuardianBlobs(snapshot_root)
        self.capture_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.wakeup = threading.Event()
        self.thread = None
        with self.store._lock, self.store._connection:
            self.store._connection.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('artifact_delivery_enabled_at',?)",(str(int(time.time())),))
            self.enabled_at=float(self.store._connection.execute("SELECT value FROM meta WHERE key='artifact_delivery_enabled_at'").fetchone()[0])
        with self.store._lock, self.store._connection:
            self.store._connection.execute("UPDATE artifact_file_deliveries SET state='rejected', reason='snapshot_capture_interrupted', notice_state=CASE WHEN notice_state IN ('submitted','binding','uncertain') THEN notice_state ELSE 'pending' END WHERE state='preparing'")
        if start:
            self.thread = threading.Thread(target=self._run, name="artifact-delivery", daemon=True)
            self.thread.start()

    def _rows(self, query, args=()):
        with self.store._lock:
            return [dict(row) for row in self.store._connection.execute(query, args)]

    def _update(self, delivery_id, **values):
        values['updated'] = time.time()
        with self.store._lock, self.store._connection:
            self.store._connection.execute(
                'UPDATE artifact_file_deliveries SET '+','.join(f'{key}=?' for key in values)+' WHERE delivery_id=?',
                (*values.values(), delivery_id))

    def reserve(self, event, candidates):
        return self._reserve(event, candidates)

    def _reserve(self, event, candidates):
        """Persist discovery identity before summary classification or transport."""
        images = {str(Path(item.path).resolve()).casefold() for item in event.generated_images}
        legacy_rows=self._rows("SELECT path FROM notification_media_deliveries WHERE event_key=?",(event.dedupe_key,))
        legacy_paths={str(Path(row['path']).resolve()).casefold() for row in legacy_rows}
        handled_images=images-legacy_paths
        # Ownership is independent of eligibility: a suppressed historical
        # image must not fall through into the legacy sender and be backfilled.
        completed_at=event.completed_at
        if completed_at is not None:
            completed_at=float(completed_at)
            if completed_at>1e12:
                completed_at/=1000
            if completed_at<self.enabled_at:
                return handled_images
        elif self.store.was_processed(event.dedupe_key):
            # A current-epoch parent is evidence for a late same-turn artifact;
            # old processed history without that evidence is never backfilled.
            recent=self._rows("SELECT 1 FROM notifications WHERE event_key=? AND created_at>=? LIMIT 1",(event.dedupe_key,self.enabled_at))
            known=self._rows("SELECT 1 FROM artifact_file_deliveries WHERE thread_id=? AND turn_id=? LIMIT 1",(event.thread_id,event.turn_id))
            if not recent and not known:
                return handled_images
        candidates=tuple(candidates)+tuple(DeliveredFileCandidate(
            'image:'+item.item_id,Path(item.path),item.file_name,'tool_resource','ready',True,
            size=item.size,sha256=item.sha256) for item in event.generated_images)

        for ordinal, candidate in enumerate(candidates):
            if not candidate.delivery_requested:
                continue
            path = candidate.path
            checked_identity = None
            if candidate.ready:
                try:
                    current = inspect_local_file(path, 10_000_000 if path and str(Path(path).resolve()).casefold() in images else MAX_BLOB_SIZE)
                    checked_identity = file_identity(current)
                    if candidate.provenance.get('inspection') == 'stat':
                        for key, value in checked_identity.items():
                            previous = candidate.provenance.get('identity_' + key[3:])
                            if previous is not None and int(previous) != value:
                                raise ValueError('source_changed_before_snapshot')
                except (OSError, ValueError) as exc:
                    candidate = replace(candidate, status='missing' if isinstance(exc, FileNotFoundError) else 'unsafe', reason=failure_code(exc))
            if path and str(Path(path).resolve()).casefold() in legacy_paths:
                continue
            # Identity excludes the mutable current digest and final-item ID. A
            # late history projection must not resend a rollout-discovered file.
            identity = str(Path(path).absolute()).casefold() if path else (candidate.uri or 'unresolved-delivery')
            delivery_id = hashlib.sha256((event.thread_id+'\0'+event.turn_id+'\0'+identity).encode()).hexdigest()
            with self.store._lock, self.store._connection:
                inserted = self.store._connection.execute("""INSERT OR IGNORE INTO artifact_file_deliveries(
                 delivery_id,event_key,thread_id,turn_id,title,candidate_id,file_name,source_path,
                 sha256,size,ordinal,state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,'preparing',?,?)""",
                 (delivery_id,event.dedupe_key,event.thread_id,event.turn_id,event.title,candidate.candidate_id,
                  candidate.display_name,str(path or ''),candidate.sha256 or '',candidate.size or 0,
                  ordinal,time.time(),time.time())).rowcount
            if not inserted:
                if candidate.ready and checked_identity is not None:
                    self._recover_local_failure(delivery_id, candidate, checked_identity)
                if path and str(Path(path).resolve()).casefold() in images:
                    # A late structured image may upgrade a not-yet-submitted
                    # generic reference; an existing platform result is final.
                    with self.store._lock,self.store._connection:
                        self.store._connection.execute("UPDATE artifact_file_deliveries SET media_kind='image' WHERE delivery_id=? AND state IN ('capture_pending','preparing','pending')",(delivery_id,))
                continue
            media_kind='image' if path and str(Path(path).resolve()).casefold() in images else 'file'
            self._update(delivery_id,media_kind=media_kind)
            if candidate.status != 'ready' or path is None:
                self._update(delivery_id,state='rejected',reason=(candidate.reason or candidate.status)[:160],notice_state='pending')
            else:
                try:
                    if checked_identity is None:
                        raise ValueError('local_unreadable')
                    self._update(delivery_id,state='capture_pending',source_meta_json=json.dumps(checked_identity))
                except (OSError,ValueError,KeyError):
                    self._update(delivery_id,state='rejected',reason='source_unavailable_at_discovery',notice_state='pending')
        self.wakeup.set()
        return handled_images

    def _recover_local_failure(self, delivery_id, candidate, checked_identity):
        # Recovery requires a fresh discovery in this running queue epoch. A
        # restart must never backfill historical failures merely by scanning.
        recoverable = {'missing', 'local_permission_denied', 'local_unreadable',
                       'unreadable', 'source_unavailable_at_discovery'}
        with self.store._lock, self.store._connection:
            row = self.store._connection.execute('SELECT * FROM artifact_file_deliveries WHERE delivery_id=?',(delivery_id,)).fetchone()
            if row is None:
                return
            row = dict(row)
            if (row['state'] != 'rejected' or row['attempts'] != 0
                or row['reason'] not in recoverable or row['created'] < self.recovery_epoch
                or row['notice_state'] in {'submitted','binding','uncertain'}):
                return
            transport = getattr(self.channel, 'store', None)
            if transport is not None and transport.outcome(self._key(row)) is not None:
                return
            changed = self.store._connection.execute(
                "UPDATE artifact_file_deliveries SET state='capture_pending',reason='',sha256=?,size=?,source_meta_json=?,next_at=0,updated=?,notice_state=CASE WHEN notice_state='pending' THEN 'none' ELSE notice_state END WHERE delivery_id=? AND state='rejected' AND attempts=0 AND updated=?",
                (candidate.sha256 or '',candidate.size or checked_identity['st_size'],json.dumps(checked_identity),time.time(),delivery_id,row['updated'])).rowcount
            if changed:
                self.store._connection.execute('INSERT INTO meta(key,value) VALUES(?,?)',
                    ('artifact_local_recovery:'+delivery_id+':'+str(row['updated']),json.dumps(row)))

    def _capture(self,row):
        try:
            self._update(row['delivery_id'],state='preparing')
            path=Path(row['source_path'])
            expected=json.loads(row['source_meta_json'])
            limit = min(MAX_BLOB_SIZE,10_000_000) if row['media_kind']=='image' else MAX_BLOB_SIZE
            data=read_verified_file(path,limit,expected=expected,expected_sha256=row['sha256'])
            digest=hashlib.sha256(data).hexdigest()
            with self.capture_lock:
                reference=self.blobs.put(data)
                fields=dict(snapshot_json=json.dumps(reference),state='pending',sha256=digest,size=len(data))
                if row['reason']=='snapshot_capacity_wait':
                    fields['reason']=''
                    if row['notice_state'] in {'none','pending'}:
                        fields.update(notice_state='none',notice_key='')
                self._update(row['delivery_id'],**fields)
        except BufferError:
            self._update(row['delivery_id'],state='capture_pending',reason='snapshot_capacity_wait',
                         next_at=time.time()+30,notice_state=row['notice_state'] if row['notice_state'] in {'done','submitted','binding','uncertain'} else 'pending')
        except (OSError,ValueError) as exc:
            reason=failure_code(exc)
            self._update(row['delivery_id'],state='rejected',reason=reason[:160],notice_state=self._failure_notice_state(row,reason))

    @staticmethod
    def _key(row, notice=False):
        if notice and row.get('notice_key'):
            return row['notice_key']
        suffix=':notice:'+hashlib.sha256(row['reason'].encode()).hexdigest()[:16] if notice else ':file'
        return 'artifact:'+row['delivery_id']+suffix

    @staticmethod
    def _failure_notice_state(row, reason=None):
        if row['notice_state'] in {'submitted','binding','uncertain'}:
            return row['notice_state']
        if row['notice_state'] == 'done':
            key = FileDeliveryQueue._key(dict(row, reason=reason or row['reason'], notice_key=''), True)
            if key == row.get('notice_key'):
                return 'done'
        return 'pending' 

    def _complete(self, row, ids, *, notice=False):
        if isinstance(ids, str):
            ids = [ids]
        ids = list(ids or ())
        if not ids or not all(isinstance(item, str) and item for item in ids):
            raise RuntimeError('transport_returned_no_message_id')
        # Save result before binding. A crash can replay the binding without
        # submitting a second platform message.
        prefix = 'notice_' if notice else ''
        self._update(row['delivery_id'], **{prefix+'ids_json' if notice else 'message_ids_json':json.dumps(ids),
                                          prefix+'state':'binding'})
        try:
            self.bind_messages(row, ids, notice)
        except Exception:
            # The platform result is already durable. Keep binding retryable;
            # this failure must never become a fresh send or unknown outcome.
            return
        self._update(row['delivery_id'], **{prefix+'state':'done'})

    def _reconcile(self, row, *, notice=False):
        prefix = 'notice_' if notice else ''
        if row[prefix+'state'] == 'binding':
            ids = json.loads(row['notice_ids_json' if notice else 'message_ids_json'])
            self._complete(row, ids, notice=notice)
            return
        transport = getattr(self.channel, 'store', None)
        if transport is None or not callable(getattr(transport,'outcome',None)):
            fields={prefix+'state':'uncertain',prefix+'reason':'restart_submission_unknown'}
            if not notice:
                fields['notice_state']=self._failure_notice_state(row)
            self._update(row['delivery_id'], **fields)
            return
        result = transport.outcome(self._key(row, notice))
        if result and result['state'] == 'done':
            self._complete(row, json.loads(result['result']), notice=notice)
        elif result is None or result['state'] == 'cancelled':
            self._update(row['delivery_id'], **{prefix+'state':'pending'})
        elif result['state'] in {'submitted','pending'}:
            # Guardian retains ownership. Rotate the reconciliation page so
            # a large pending prefix cannot starve later known results.
            self._update(row['delivery_id'])
            return
        else:
            state = 'uncertain' if result['state']=='uncertain' else 'rejected'
            fields = {prefix+'state':state,prefix+'reason':str(result.get('error') or state)[:160]}
            if not notice:
                fields['notice_state']=self._failure_notice_state(row)
            self._update(row['delivery_id'], **fields)

    def _send(self, row, *, notice=False):
        prefix = 'notice_' if notice else ''
        attempts = row[prefix+'attempts'] + 1
        data = None
        if not notice:
            try:
                data = self.blobs.load(json.loads(row['snapshot_json']))
            except (OSError,ValueError,TypeError):
                self._update(row['delivery_id'],state='rejected',reason='snapshot_invalid',notice_state=self._failure_notice_state(row))
                return
        fields={prefix+'state':'submitted',prefix+'attempts':attempts}
        if notice:
            row=dict(row,notice_key='')
            row['notice_key']=self._key(row,True)
            fields['notice_key']=row['notice_key']
        if notice:
            # A recovered file can invalidate a queued failure between selection
            # and submission. Claim only the exact still-current failure.
            with self.store._lock, self.store._connection:
                claimed = self.store._connection.execute(
                    "UPDATE artifact_file_deliveries SET notice_state='submitted',notice_attempts=?,notice_key=?,updated=? WHERE delivery_id=? AND notice_state='pending' AND state=? AND reason=? AND updated=?",
                    (attempts,row['notice_key'],time.time(),row['delivery_id'],row['state'],row['reason'],row['updated'])).rowcount
            if not claimed:
                return
        else:
            self._update(row['delivery_id'], **fields)
        try:
            if notice:
                uncertainty = row['state']=='uncertain'
                status = '发送结果未知，未自动重发' if uncertainty else '未能发送'
                ids = self.channel.send_text(f"文件《{row['file_name']}》{status}。原因：{failure_description(row['reason'],row['media_kind'])}。可回复此消息继续原任务。",
                                             idempotency_key=self._key(row, True))
            else:
                if row['media_kind']=='image':
                    ids = self.channel.send_image(data,idempotency_key=self._key(row))
                else:
                    ids = self.channel.send_file(data, file_name=row['file_name'], idempotency_key=self._key(row))
            self._complete(row, ids, notice=notice)
        except FeishuSendNotSubmittedError:
            self._update(row['delivery_id'], **{prefix+'state':'pending',prefix+'reason':'not_submitted',
                                                'next_at':time.time()+min(300,2**min(attempts,8))})
        except FeishuSendRejectedError as exc:
            retry = bool(exc.retryable) and attempts < 4
            fields = {prefix+'state':'pending' if retry else 'rejected',prefix+'reason':(str(exc.code)+(':'+str(exc.raw_code) if exc.raw_code is not None else ''))[:160],
                      'next_at':time.time()+min(300,2**min(attempts,8))}
            if not notice and not retry:
                fields['notice_state']=self._failure_notice_state(row)
            self._update(row['delivery_id'], **fields)
        except Exception as exc:
            fields={prefix+'state':'uncertain',prefix+'reason':'submission_unknown:'+type(exc).__name__}
            if not notice:
                fields['notice_state']=self._failure_notice_state(row)
            self._update(row['delivery_id'], **fields)

    def cleanup_snapshots(self):
        with self.capture_lock:
            rows = self._rows("SELECT snapshot_json,state FROM artifact_file_deliveries WHERE snapshot_json IS NOT NULL")
            retained, terminal = [], []
            for row in rows:
                ref = json.loads(row['snapshot_json'])
                (terminal if row['state'] in {'done','rejected','duplicate'} else retained).append(ref['name'])
            self.blobs.cleanup(referenced_names=retained, terminal_names=terminal)
            with self.store._lock,self.store._connection:
                self.store._connection.execute("UPDATE artifact_file_deliveries SET snapshot_json=NULL WHERE state IN ('done','rejected','duplicate') AND snapshot_json IS NOT NULL")

    def _drain_ready(self,*,notices=False):
        if self.stop_event.is_set() or not self.channel.is_online():
            return
        rows=self._rows("SELECT * FROM artifact_file_deliveries WHERE (state='pending' AND next_at<=?) OR (? AND notice_state='pending') ORDER BY created,ordinal LIMIT 16",(time.time(),int(notices)))
        for row in rows:
            if self.stop_event.is_set():
                break
            self._send(row,notice=row['state']!='pending')
        self.cleanup_snapshots()

    def drain_once(self):
        self.cleanup_snapshots()
        # Bounded pages, never a cap on the number of deliverables recorded.
        rows=self._rows("SELECT * FROM artifact_file_deliveries WHERE state IN ('submitted','binding') OR notice_state IN ('submitted','binding') ORDER BY updated LIMIT 32")
        for row in rows:
            try:
                if row['state'] in {'submitted','binding'}:
                    self._reconcile(row)
                if row['notice_state'] in {'submitted','binding'}:
                    self._reconcile(row,notice=True)
            except Exception:
                # A read/binding error cannot reset submission state.
                self._update(row['delivery_id'])
        # Release existing deliverable bytes before trying to capture more.
        self._drain_ready()
        for row in self._rows("SELECT * FROM artifact_file_deliveries WHERE state='capture_pending' AND notice_state NOT IN ('submitted','binding') AND (next_at<=? OR (? AND notice_state='pending')) ORDER BY created,ordinal LIMIT 16",(time.time(),int(self.channel.is_online()))):
            if self.stop_event.is_set():
                return
            self._capture(row)
            if self.channel.is_online():
                current=self._rows("SELECT * FROM artifact_file_deliveries WHERE delivery_id=?",(row['delivery_id'],))[0]
                if current['state']=='pending':
                    self._send(current)
                self.cleanup_snapshots()
        self._drain_ready(notices=True)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.drain_once()
                self.cleanup_snapshots()
            except Exception:
                # Persisted submitted/binding rows are reconciled on next pass.
                import logging
                logging.getLogger(__name__).exception('artifact_queue_iteration_failed')
            self.wakeup.wait(1)
            self.wakeup.clear()

    def stop(self):
        self.stop_event.set()
        self.wakeup.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
