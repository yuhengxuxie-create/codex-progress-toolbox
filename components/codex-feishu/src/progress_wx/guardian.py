"""Single-owner Feishu transport and deterministic worker lifecycle supervisor."""
from __future__ import annotations

import base64
from dataclasses import asdict
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from .guardian_store import GuardianStore
from .process_control import (acquire_instance, release_instance, read_pid_file,
    instance_running, request_stop, stop_requested_for, process_creation_time)

LOGGER = logging.getLogger('progress_wx.guardian')


def control_root(config):
    return config.service.database.parent / 'guardian'


_WORKER_ERRORS = {'worker_unresponsive': 'hung', 'worker_initialization_timeout': 'startup-timeout'}


def _alive(record):
    return bool(record and record.get('pid') and isinstance(record.get('creation_time'),int)
                and process_creation_time(int(record['pid'])) == record.get('creation_time'))


def guardian_status(config):
    root = control_root(config)
    saved = {}
    error = None
    store = None
    try:
        store = GuardianStore(root, readonly=True)
        for key in ('desired_state','previous_desired_state','guardian','worker','channel','last_error_code'):
            saved[key] = store.get(key)
        with store.lock:
            queue = dict(store.db.execute('SELECT state,count(*) FROM outgoing GROUP BY state').fetchall())
        available = True
    except (OSError, ValueError, __import__('sqlite3').Error):
        available = False
        error = 'guardian_state_unavailable' if (root/'transport.sqlite').exists() else 'guardian_not_initialized'
        queue = {}
    finally:
        if store is not None:
            store.close()
    now = time.time()
    g = dict(saved.get('guardian') or {})
    w = dict(saved.get('worker') or {})
    for record in (g,w):
        record['running'] = _alive(record)
        for key in ('pid','creation_time','generation','heartbeat_at'):
            record.setdefault(key,None)
    heartbeat = g.get('heartbeat_at')
    skew = heartbeat is not None and heartbeat > now+10
    g['healthy'] = bool(g['running'] and heartbeat and -10 <= now-heartbeat <= 15)
    g['control_directory'] = str(root)
    w['ready'] = bool(w.get('ready') and w['running'] and w.get('heartbeat_at') and -10 <= now-w['heartbeat_at'] <= 30)
    w.setdefault('state','stopped')
    if w['running'] and w.get('heartbeat_at') and now-w['heartbeat_at'] > 30:
        w['state'] = 'unresponsive'
    desired = saved.get('desired_state') or 'stopped'
    channel = dict(saved.get('channel') or {'online':False,'state':'stopped'})
    channel['online'] = bool(channel.get('online') and g['healthy'])
    if not g['running']:
        channel['state'] = 'stopped'
    supervisor_path = root/'windows-supervisor.json'
    supervisor = None
    try:
        if supervisor_path.stat().st_size <= 16384:
            supervisor = json.loads(supervisor_path.read_text(encoding='utf-8-sig'))
    except (OSError,ValueError):
        pass
    return dict(schema_version=1,available=available,desired_state=desired,guardian=g,worker=w,channel=channel,
                maintenance={'active':desired=='maintenance','previous_desired_state':saved.get('previous_desired_state')},
                last_error_code='clock_skew' if skew else error or saved.get('last_error_code') or channel.get('last_error_code'),
                recovery_required=bool(available and desired not in {'maintenance','exited'} and not g['healthy']),
                supervisor_state_path=str(supervisor_path),windows_supervisor=supervisor,queue=queue)


def launch(config, command, *extra):
    from .config import PROJECT_ROOT
    executable = Path(sys.executable)
    if os.name == 'nt' and executable.with_name('pythonw.exe').is_file():
        executable = executable.with_name('pythonw.exe')
    return subprocess.Popen([str(executable),str(PROJECT_ROOT/'progress-wx.py'),'--config',str(config.path),command,*extra],
                            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),close_fds=True)


def ensure_guardian(config):
    root = control_root(config)
    if not instance_running(root/'guardian.pid'):
        # Never overlap the legacy production WS owner during migration.
        state = guardian_status(config)
        if instance_running(config.service.pid_file) and not state['worker']['running']:
            raise RuntimeError('legacy_worker_requires_controlled_migration')
        launch(config,'guardian-run')


def control(config, action, *, timeout=100, shutdown_guardian=False):
    store = GuardianStore(control_root(config))
    try:
        if action == 'start':
            before = guardian_status(config)
            if instance_running(config.service.pid_file) and not before['worker']['running']:
                raise RuntimeError('legacy_worker_requires_controlled_migration')
            requested_intent = store.intent('running')
            ensure_guardian(config)
        elif action == 'stop':
            store.intent('stopped')
        elif action == 'exit':
            store.intent('exited')
        elif action == 'enter':
            store.intent('maintenance')
            if shutdown_guardian:
                store.put('maintenance_shutdown',True)
        elif action == 'leave':
            store.leave_maintenance()
            store.put('maintenance_shutdown',False)
            if store.get('desired_state') != 'exited':
                ensure_guardian(config)
        else:
            raise ValueError('unknown guardian control')
        # A legacy worker may exist before the first guardian deployment.
        if action in {'stop','exit','enter'}:
            request_stop(config.service.pid_file)
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            status = guardian_status(config)
            running = instance_running(config.service.pid_file)
            if action in {'start','leave'}:
                desired = status['desired_state']
                if desired != 'running':
                    return 0 if action == 'leave' else 1
                if status['worker']['ready'] and status['channel']['online']:
                    return 0
                if status['worker']['state'] == 'failed' and (action=='leave' or store.get('last_launch_intent',0)>=requested_intent):
                    return 1
            elif not running and (action not in {'exit'} and not shutdown_guardian or not status['guardian']['running']):
                return 0
            time.sleep(.2)
        return 1 if action in {'start','leave'} else 2
    finally:
        store.close()


class Guardian:
    def __init__(self, config, channel, *, spawn=None, offline_sender=None):
        self.config = config
        self.channel = channel
        self.offline_sender=offline_sender
        self.store = GuardianStore(control_root(config))
        from .guardian_spool import InboundSpool
        self.inbound_spool=InboundSpool(control_root(config))
        from .guardian_blob import GuardianBlobs
        self.blobs=GuardianBlobs(control_root(config)/'media-blobs')
        self.spawn = spawn or (lambda token: launch(config,'worker-run','--guardian-token',token))
        self.stop = threading.Event()
        self.identity = None
        self.send_thread = None
        self.last_online = None
        self.disconnected_at = None
        self.last_cleanup = 0.0
        self.send_progress = time.monotonic()

    def system(self,key,text):
        # INSERT OR IGNORE in outgoing atomically owns deduplication with payload.
        first = self.store.outcome('system:'+key) is None
        self.store.enqueue('system:'+key,'send_text',{'text':text})
        if first:
            LOGGER.info('guardian lifecycle kind=%s',key.split(':',1)[0])

    def _worker_error_origin(self, generation):
        code = self.store.get('last_error_code')
        if code not in _WORKER_ERRORS or not isinstance(generation, str) or not generation:
            return None
        context = self.store.get('worker_error_context')
        if context is not None:
            return context if context.get('code') == code and context.get('generation') == generation else None
        # R2 only persisted the fault through its deterministic outgoing key.
        key = _WORKER_ERRORS[code] + ':' + generation
        if self.store.outcome('system:' + key) is not None:
            return {'code': code, 'generation': generation, 'notice_key': key, 'legacy': True}
        return None

    def _record_worker_error(self, code, worker, now):
        generation = worker['generation']
        with self.store.lock:
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                current = self.store.get('worker', {})
                if (self.store.get('desired_state') != 'running'
                    or current.get('generation') != generation or not _alive(current)):
                    return None
                stale = (current.get('ready') and now-current.get('heartbeat_at', now)>30
                         if code == 'worker_unresponsive' else
                         not current.get('ready') and now-current.get('started_at',now)>90)
                if not stale:
                    return None
                context = self._worker_error_origin(generation)
                if context is None or context['code'] != code:
                    key = _WORKER_ERRORS[code] + ':' + generation
                    if self.store.outcome('system:' + key) is not None:
                        key += ':' + uuid.uuid4().hex
                    context = {'code': code, 'generation': generation, 'notice_key': key, 'started_at': now}
                values = {'last_error_code': code, 'worker_error_context': context,
                          'worker_error_recovery': None}
                self.store.db.executemany('INSERT OR REPLACE INTO control VALUES(?,?)',
                    [(key, json.dumps(value)) for key, value in values.items()])
                return context['notice_key']
            except BaseException:
                self.store.db.rollback()
                raise
            finally:
                if self.store.db.in_transaction:
                    self.store.db.commit()

    def _recover_worker_error(self, worker, now):
        # Re-read under the same transaction so an external stop/new intent
        # cannot be mistaken for the already-observed recovery.
        with self.store.lock:
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                current = self.store.get('worker', {})
                generation = worker.get('generation')
                if (self.store.get('desired_state') != 'running'
                    or current.get('generation') != generation or not _alive(current)
                    or not current.get('ready') or not -10 <= now-current.get('heartbeat_at', 0) <= 30
                    or not self.store.get('channel', {}).get('online')):
                    self.store.db.execute('INSERT OR REPLACE INTO control VALUES(?,?)',
                        ('worker_error_recovery', 'null'))
                    return
                origin = self._worker_error_origin(generation)
                if origin is None:
                    replacement = self.store.get('worker_error_replacement') or {}
                    if (replacement.get('new_generation') == generation
                        and self.store.get('worker_token') == generation
                        and replacement.get('intent_at') == self.store.get('intent_at') == self.store.get('last_launch_intent')):
                        origin = self._worker_error_origin(replacement.get('old_generation'))
                        if origin is not None and origin['code'] != replacement.get('code'):
                            origin = None
                if origin is None:
                    return
                # One fresh sample does not close a fault episode. Require
                # 60 seconds continuously healthy and at least 3 advancing
                # business heartbeats. Persist progress across guardian restarts.
                recovery = self.store.get('worker_error_recovery') or {}
                heartbeat = current.get('heartbeat_at', 0)
                identity = (origin['notice_key'], generation, self.store.get('intent_at'))
                if (tuple(recovery.get('identity', ())) != identity
                    or not 0 <= now-recovery.get('last_seen', 0) <= 30
                    or heartbeat < recovery.get('heartbeat', 0)):
                    recovery = {'identity': identity, 'started_at': now,
                                'heartbeat': heartbeat, 'samples': 1}
                elif heartbeat > recovery.get('heartbeat', 0):
                    recovery['samples'] += 1
                    recovery['heartbeat'] = heartbeat
                recovery['last_seen'] = now
                self.store.db.execute('INSERT OR REPLACE INTO control VALUES(?,?)',
                    ('worker_error_recovery', json.dumps(recovery)))
                if now-recovery['started_at'] < 60 or recovery['samples'] < 3:
                    return
                history = self.store.get('worker_error_history', [])
                history = [*history[-31:], {**origin, 'recovered_at': now, 'recovered_generation': generation}]
                # Preserve the fault row, but do not deliver a still-unsubmitted
                # timeout warning after verified recovery. Submitted/unknown or
                # already delivered events retain their exact durable outcome.
                self.store.db.execute("UPDATE outgoing SET state='superseded',error='worker_recovered',updated=? WHERE key=? AND state IN ('pending','cancelled')",
                    (now, 'system:'+origin['notice_key']))
                values = {'last_error_code': None, 'worker_error_context': None,
                          'worker_error_replacement': None, 'worker_error_history': history,
                          'worker_error_recovery': None}
                self.store.db.executemany('INSERT OR REPLACE INTO control VALUES(?,?)',
                    [(key, json.dumps(value)) for key, value in values.items()])
            except BaseException:
                self.store.db.rollback()
                raise
            finally:
                if self.store.db.in_transaction:
                    self.store.db.commit()

    def observe_windows_session(self,marker):
        if not marker:
            LOGGER.warning('guardian Windows logon identity unavailable; no inferred business start')
            return
        with self.store.lock:
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                previous=self.store.get('windows_session_marker')
                worker=self.store.get('worker',{})
                if previous and previous!=marker and self.store.get('desired_state')=='running' and not _alive(worker):
                    next_intent=max(time.time(),self.store.get('intent_at',0),self.store.get('last_launch_intent',0))+0.000001
                    self.store.db.execute('INSERT OR REPLACE INTO control VALUES(?,?)',('intent_at',json.dumps(next_intent)))
                self.store.db.execute('INSERT OR REPLACE INTO control VALUES(?,?)',('windows_session_marker',json.dumps(marker)))
                self.store.db.commit()
            except BaseException:
                self.store.db.rollback()
                raise

    def receive(self,message):
        # The SDK adapter has already enforced owner + non-bot + p2p.
        if message.sender_id != self.config.feishu.target_open_id or (not message.chat_id and message.source_kind!='bot_menu') or not message.message_id:
            return
        try:
            self.store.receive(message.message_id,asdict(message))
        except BufferError:
            self.system('inbound-full:'+message.message_id,'消息未进入业务队列：接收队列已满，请稍后重新发送。')
        except Exception:
            self.stop.set()
            raise

    def inbound_failure(self,message,exc):
        try:
            self.inbound_spool.save(message)
            LOGGER.error('guardian intake saved for recovery type=%s',type(exc).__name__)
        except Exception as failure:
            LOGGER.critical('guardian intake NOT saved type=%s',type(failure).__name__)
            try:
                if self.offline_sender is not None:
                    self.offline_sender.send_text('飞书消息接收失败，未能安全保存；请在电脑上检查机器人状态和磁盘，并在恢复后重新发送。',idempotency_key='system:intake-failed:'+hashlib.sha256(message.message_id.encode()).hexdigest())
            except Exception:
                LOGGER.error('guardian intake failure notification unavailable')
            raise
        finally:
            self.stop.set()

    def _system_commands(self):
        # Guardian consumes only its exact command; worker receives ordinary events.
        with self.store.lock:
            rows = self.store.db.execute("SELECT key,payload,created FROM incoming WHERE generation<>'system' AND acknowledged_at IS NULL AND key NOT IN (SELECT key FROM system_checked) ORDER BY created LIMIT 128").fetchall()
        for row in rows:
            p = json.loads(row['payload'])
            if p.get('content') != '.启动飞书机器人':
                with self.store.lock,self.store.db:
                    self.store.db.execute('INSERT OR IGNORE INTO system_checked VALUES(?,?)',(row['key'],time.time()))
                continue
            if p.get('source_kind') != 'message' or p.get('attachments') or p.get('attachment_error'):
                self.store.accepted(row['key'],'system')
                continue
            if self.store.db.execute('SELECT 1 FROM commands WHERE key=?',(row['key'],)).fetchone():
                self.store.accepted(row['key'],'system')
                continue
            created = p.get('created_at') or 0
            if not created or not -10 <= time.time()-created <= 300:
                self.system('command-expired:'+row['key'],'启动请求已过期或无法核验发送时间，请重新发送 .启动飞书机器人。')
            elif self.store.get('desired_state') in {'maintenance','exited'}:
                self.system('command-maintenance:'+row['key'],'当前处于维护或完整退出状态，不能远程启动，请在本机完成维护。')
            else:
                s = guardian_status(self.config)
                if s['worker']['ready'] and s['channel']['online']:
                    self.system('command-running:'+row['key'],'飞书机器人业务服务已经运行。')
                elif s['worker']['running'] and s['worker']['state']=='unresponsive':
                    self.system('command-hung:'+row['key'],'业务进程无响应，已保留原任务状态；需要在本机核查，不能重复启动另一份业务。')
                else:
                    self.store.remote_start(row['key'])
            self.store.command(row['key'],'processed')
            self.store.accepted(row['key'],'system')

    def _send_loop(self):
        from .feishu import FeishuSendNotSubmittedError, FeishuSendRejectedError
        while not self.stop.wait(.1):
            self.send_progress = time.monotonic()
            online=self.channel.is_online()
            if not online and self.offline_sender is None:
                continue
            row = self.store.claim(system_only=not online)
            if not row:
                continue
            key = row['key']
            blob_name=None
            try:
                if key.startswith('system:ready:'):
                    worker=self.store.get('worker',{})
                    if self.store.get('desired_state')!='running' or key!='system:ready:'+str(worker.get('generation','')) or not _alive(worker):
                        self.store.finish(key,'superseded',error='worker_readiness_changed')
                        continue
                    if not self.channel.is_online() or not worker.get('ready') or not -10<=time.time()-worker.get('heartbeat_at',0)<=30:
                        self.store.finish(key,'pending',error='waiting_for_current_readiness')
                        continue
                if key.startswith('system:disconnected:') and self.channel.is_online():
                    self.store.finish(key,'superseded',error='connection_restored')
                    continue
                payload = json.loads(row['payload'])
                if key.startswith(('system:hung:', 'system:startup-timeout:')):
                    current = self.store.get('worker', {})
                    origin = self._worker_error_origin(current.get('generation'))
                    if origin is None or 'system:'+origin['notice_key'] != key:
                        self.store.finish(key, 'superseded', error='worker_fault_no_longer_current')
                        continue
                    now = time.time()
                    stale = (current.get('ready') and now-current.get('heartbeat_at',now)>30
                             if key.startswith('system:hung:') else
                             not current.get('ready') and now-current.get('started_at',now)>90)
                    if self.store.get('desired_state') != 'running' or not _alive(current) or not stale:
                        self.store.finish(key, 'pending', error='waiting_for_current_worker_fault')
                        continue
                if 'blob_ref' in payload:
                    reference=payload.pop('blob_ref')
                    blob_name=reference.get('name')
                    try:
                        payload['data']=self.blobs.load(reference)
                    except (OSError,ValueError) as exc:
                        self.store.finish(key,'rejected',error='invalid_media_blob')
                        continue
                if 'data_b64' in payload:
                    payload['data'] = base64.b64decode(payload.pop('data_b64'),validate=True)
                if row['method'].startswith('send_'):
                    payload['idempotency_key'] = key
                if row['method'] not in {'send_text','send_image','send_file','send_card','fetch_message','bot_sender_ids','recipient_scope_for_messages'}:
                    raise ValueError('unknown guardian RPC method')
                sender=self.offline_sender if key.startswith('system:') and self.offline_sender is not None else self.channel
                result = getattr(sender,row['method'])(**payload)
                self.store.finish(key,'done',result=result)
            except FeishuSendNotSubmittedError:
                self.store.finish(key,'pending' if row['attempts'] < 4 else 'rejected',error='not_submitted')
            except FeishuSendRejectedError as exc:
                self.store.finish(key,'pending' if exc.retryable and row['attempts']<4 else 'rejected',error=exc.code, result={'code':exc.code,'raw_code':exc.raw_code,'retryable':False,'retry_exhausted':bool(exc.retryable),'exception_type':type(exc.__cause__).__name__ if exc.__cause__ is not None else type(exc).__name__})
            except ValueError as exc:
                self.store.finish(key,'rejected',error=type(exc).__name__)
            except Exception as exc:
                self.store.finish(key,'uncertain',error=type(exc).__name__)
                LOGGER.warning('guardian send outcome unknown type=%s',type(exc).__name__)
            finally:
                if blob_name:
                    self.store.cleanup_blobs(self.blobs,terminal_names=(blob_name,))
                # Release the sender's large buffer before waiting for another row.
                if 'payload' in locals():payload=None

    def tick(self):
        now = time.time()
        self.inbound_spool.replay(self.config.feishu.target_open_id,self.config.service.database.parent/'feishu-media',self.store.receive,
            lambda key,reason:self.system('inbound-recovery-rejected:'+key,'一条暂存消息已过期或无法核验，未执行，请重新发送。'))
        if self.send_thread and (not self.send_thread.is_alive() or time.monotonic()-self.send_progress>120):
            raise RuntimeError('guardian_sender_unresponsive')
        desired = self.store.get('desired_state','stopped')
        online = bool(self.channel.is_online())
        snapshot_method = getattr(self.channel, 'connection_snapshot', None)
        snapshot = snapshot_method() if callable(snapshot_method) else {}
        dead_thread = snapshot.get('thread_alive') is False
        failed = snapshot.get('state') == 'failed' or dead_thread
        # Channel failure is distinct from the guardian control loop heartbeat.
        # Persist only fixed reason codes, never SDK exception bodies or URLs.
        reason = None
        if failed:
            online = False
            reason = ('channel_dependency_failed'
                      if snapshot.get('last_failure_type') == 'FeishuDependencyError'
                      else 'channel_thread_exited' if dead_thread else 'channel_failed')
        self.store.put('channel', {'online': online,
            'state': 'failed' if failed else 'online' if online else 'reconnecting',
            'last_error_code': reason})
        if self.last_online is not False and not online:
            self.disconnected_at = now
            self.store.put('disconnected_at',now)
            message = ('飞书连接已停止，暂时无法接收消息；需要在本机检查并重新启动通信。'
                       if failed else '飞书连接异常，暂时无法接收消息；通信守护正在尝试恢复。')
            self.system('disconnected:'+str(now), message)
        if online and self.last_online is False and self.disconnected_at:
            self.store.coalesce_disconnection('system:disconnected:'+str(self.disconnected_at))
            self.system('reconnected:'+str(self.disconnected_at),'飞书连接已恢复，离线期间的待处理事项将按原状态继续处理。')
            self.disconnected_at = None
            self.store.put('disconnected_at',None)
        self.last_online = online
        self._system_commands()
        desired = self.store.get('desired_state','stopped')
        worker = self.store.get('worker',{})
        alive = _alive(worker)
        intent_at = self.store.get('intent_at',0)
        if not (alive and worker.get('ready') and -10 <= now-worker.get('heartbeat_at',0) <= 30 and online):
            self.store.put('worker_error_recovery', None)
        if desired == 'running':
            if alive:
                if worker.get('ready') and -10 <= now-worker.get('heartbeat_at',0) <= 30 and online:
                    self._recover_worker_error(worker, now)
                    self.system('ready:'+worker['generation'],'飞书机器人业务服务已启动，通信与任务处理均已就绪。')
                elif worker.get('ready') and now-worker.get('heartbeat_at',0) > 30:
                    notice = self._record_worker_error('worker_unresponsive', worker, now)
                    # An older release may already own this key with its old
                    # wording. Never change its digest or resend that event.
                    if notice and self.store.outcome('system:'+notice) is None:
                        self.system(notice,'飞书机器人业务状态更新超时，暂无法确认响应；原任务状态已保留，需要在本机核查。')
                elif not worker.get('ready') and now-worker.get('started_at',now)>90:
                    notice = self._record_worker_error('worker_initialization_timeout', worker, now)
                    if notice and self.store.outcome('system:'+notice) is None:
                        self.system(notice,'飞书机器人业务服务初始化超时，尚未就绪；原任务状态已保留，需要在本机核查。')
            elif worker.get('state') in {'starting','ready'}:
                if worker.get('state') == 'starting' and now-worker.get('started_at',0)<90 and not worker.get('pid'):
                    pass
                else:
                    worker.update(state='failed',ready=False)
                    self.store.put('worker',worker)
                    self.system('failed:'+worker['generation'],'飞书机器人业务服务已停止或启动失败，可以发送 .启动飞书机器人 重试。')
            elif self.store.get('last_launch_intent') != intent_at:
                token = uuid.uuid4().hex
                origin = self._worker_error_origin(worker.get('generation'))
                if origin is not None:
                    self.store.put('worker_error_replacement', {
                        'old_generation': origin['generation'], 'new_generation': token,
                        'code': origin['code'], 'intent_at': intent_at,
                    })
                self.store.put('worker_token',token)
                self.store.put('last_launch_intent',intent_at)
                worker = {'generation':token,'state':'starting','ready':False,'started_at':now,'heartbeat_at':None}
                self.store.put('worker',worker)
                try:
                    child = self.spawn(token)
                    with self.store.lock:
                        self.store.db.execute('BEGIN IMMEDIATE')
                        current = self.store.get('worker',{})
                        if current.get('generation') == token and not current.get('pid'):
                            current.update(pid=child.pid,creation_time=process_creation_time(child.pid))
                            self.store.db.execute('INSERT OR REPLACE INTO control VALUES(?,?)',('worker',json.dumps(current)))
                        self.store.db.commit()
                except Exception as exc:
                    worker.update(state='failed')
                    self.store.put('worker',worker)
                    self.store.put('last_error_code',type(exc).__name__)
                    self.system('failed:'+token,'飞书机器人业务服务启动失败，状态已保留，可发送 .启动飞书机器人 重试。')
        else:
            if instance_running(self.config.service.pid_file):
                request_stop(self.config.service.pid_file)
            elif worker.get('state') not in {None,'stopped'}:
                worker.update(state='stopped',ready=False)
                self.store.put('worker',worker)
                if desired == 'stopped':
                    self.system('stopped:'+str(intent_at),'已按要求停止飞书机器人业务服务；远程救援仍可用，可发送 .启动飞书机器人。')
            if not instance_running(self.config.service.pid_file) and (desired=='exited' or desired=='maintenance' and self.store.get('maintenance_shutdown')):
                self.stop.set()
        # Proves the real control/queue/worker checks completed; no independent heartbeat thread.
        if self.identity:
            self.store.put('guardian',{**{k:v for k,v in self.identity.items() if k!='_mutex'},'generation':self.generation,'heartbeat_at':now})
        if now-self.last_cleanup>60:
            self.store.cleanup()
            self.store.cleanup_blobs(self.blobs)
            self.last_cleanup=now

    def run(self):
        root = control_root(self.config)
        self.identity = acquire_instance(root/'guardian.pid',self.config.path)
        self.store.put('channel_identity',{'app_id':getattr(self.config.feishu,'app_id',''),'owner':self.config.feishu.target_open_id})
        self.generation = uuid.uuid4().hex
        self.store.recover()
        from .guardian_session import current_session_marker
        self.observe_windows_session(current_session_marker())
        self.disconnected_at = self.store.get('disconnected_at')
        if self.disconnected_at:
            self.last_online=False
        try:
            self.channel.start(self.receive)
            self.send_thread = threading.Thread(target=self._send_loop,name='guardian-outgoing',daemon=True)
            self.send_thread.start()
            while not self.stop.is_set():
                if stop_requested_for(root/'guardian.pid',self.identity):
                    self.store.intent('exited')
                self.tick()
                self.stop.wait(1)
            return 0
        finally:
            self.stop.set()
            if self.send_thread:
                self.send_thread.join(timeout=2)
            self.channel.stop()
            release_instance(root/'guardian.pid',self.identity)
            self.store.close()


def run_guardian(config):
    from .feishu import FeishuMessageChannel
    from .secrets import DpapiSecretStore
    secret = DpapiSecretStore(config.feishu.app_secret_file).load()
    if not secret:
        raise RuntimeError('guardian_credentials_unavailable')
    channel = FeishuMessageChannel(app_id=config.feishu.app_id,app_secret=secret,
        target_open_id=config.feishu.target_open_id,connect_timeout_seconds=config.feishu.connect_timeout_seconds,
        max_attempts=config.service.max_attempts,retry_delays=config.service.retry_delays,
        error_handler=lambda exc: LOGGER.error('guardian channel error type=%s',type(exc).__name__),
        media_cache_dir=config.service.database.parent/'feishu-media')
    from .guardian_rest import LifecycleRestSender
    guardian=Guardian(config,channel,offline_sender=LifecycleRestSender(config.feishu.app_id,secret,config.feishu.target_open_id))
    channel._media_key_lookup=guardian.store.lookup_media_key
    channel._media_key_store=guardian.store.store_media_key
    channel.inbound_failure_handler=guardian.inbound_failure
    return guardian.run()


def recover_guardian(config, expected_pid, expected_creation_time, reason):
    """Only the matching stale guardian may be terminated; worker state remains intact."""
    status = guardian_status(config)
    if not status['available'] or status['desired_state'] in {'maintenance','exited'}:
        return 2
    record = status['guardian']
    if (record.get('pid') or 0,record.get('creation_time') or 0) != (expected_pid,expected_creation_time):
        return 2
    if status['last_error_code']=='clock_skew' or record['healthy']:
        return 2
    if record['running']:
        if reason!='unresponsive' or not record['heartbeat_at'] or time.time()-record['heartbeat_at']<30:
            return 2
        if os.name != 'nt':
            raise RuntimeError('unresponsive recovery requires Windows verified handle')
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetProcessTimes.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.FILETIME),ctypes.POINTER(wintypes.FILETIME),ctypes.POINTER(wintypes.FILETIME),ctypes.POINTER(wintypes.FILETIME)]
        kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x100001|0x1000,False,expected_pid)
        if not handle:
            return 2
        try:
            times=[wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle,*[ctypes.byref(t) for t in times]):
                return 2
            if (times[0].dwHighDateTime<<32|times[0].dwLowDateTime)!=expected_creation_time:
                return 2
            # A last-moment loop advance cancels termination.
            fresh=guardian_status(config)
            if fresh['guardian']['heartbeat_at']!=record['heartbeat_at'] or fresh['desired_state'] in {'maintenance','exited'}:
                return 2
            if not kernel.TerminateProcess(handle,1) or kernel.WaitForSingleObject(handle,5000)!=0:
                return 2
        finally:
            kernel.CloseHandle(handle)
    store=GuardianStore(control_root(config))
    try:
        key=f"guardian-recovered:{expected_pid}:{expected_creation_time}"
        store.enqueue('system:'+key,'send_text',{'text':'飞书通信守护已从异常退出或无响应中恢复，原有业务启停意图及任务状态保持不变。'})
        ensure_guardian(config)
    finally:
        store.close()
    deadline=time.monotonic()+25
    while time.monotonic()<deadline:
        if guardian_status(config)['guardian']['healthy']:
            return 0
        time.sleep(.2)
    return 1


def recover_worker(config,expected_pid,expected_creation_time):
    """Explicit local repair only; do not auto-kill a worker with unknown task results."""
    s=guardian_status(config);record=s['worker']
    if s['desired_state']!='running' or not s['guardian']['healthy']:
        return 2
    if (record.get('pid'),record.get('creation_time'))!=(expected_pid,expected_creation_time):
        return 2
    if not record['running']:
        return control(config,'start')
    heartbeat=record.get('heartbeat_at')
    if not heartbeat or not 30<time.time()-heartbeat<86400 or os.name!='nt':
        return 2
    import ctypes
    from ctypes import wintypes
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];k.OpenProcess.restype=wintypes.HANDLE
    k.GetProcessTimes.argtypes=[wintypes.HANDLE,*([ctypes.POINTER(wintypes.FILETIME)]*4)]
    k.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT];k.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];k.CloseHandle.argtypes=[wintypes.HANDLE]
    h=k.OpenProcess(0x101001,False,expected_pid)
    if not h:return 2
    try:
        times=[wintypes.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(h,*[ctypes.byref(t) for t in times]):return 2
        if (times[0].dwHighDateTime<<32|times[0].dwLowDateTime)!=expected_creation_time:return 2
        fresh=guardian_status(config)
        if fresh['worker'].get('heartbeat_at')!=heartbeat or fresh['desired_state']!='running':return 2
        if not k.TerminateProcess(h,1) or k.WaitForSingleObject(h,5000)!=0:return 2
    finally:k.CloseHandle(h)
    return control(config,'start')
