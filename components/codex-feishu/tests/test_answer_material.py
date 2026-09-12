import json
import sqlite3
from contextlib import closing
from pathlib import Path
import pytest
import test_codex_store as fixtures


@pytest.fixture
def case():
    c=fixtures.CodexStoreTests();c.setUp()
    try:yield c
    finally:c.tearDown()


def add_item(db, tid, turn, iid, ordinal, kind, payload):
    db.execute('INSERT INTO thread_items(thread_id,turn_id,item_id,rollout_ordinal,item_type,item_json) VALUES(?,?,?,?,?,?)',(tid,turn,iid,ordinal,kind,json.dumps(payload,ensure_ascii=False)))


@pytest.mark.parametrize('tail',['方案已记录。','更正：应当先备份，暂不执行。'])
def test_all_same_turn_final_answers_preserve_order_and_short_correction(case,tail):
    with closing(sqlite3.connect(case.history)) as db,db:
        db.execute("UPDATE thread_items SET rollout_ordinal=20,item_json=? WHERE item_id='item-final'",(json.dumps({'type':'agentMessage','id':'item-final','phase':'final_answer','text':tail}),))
        for turn,iid,ordinal,phase,text in [('turn-final','answer',10,'final_answer','实质方案：先检查，再选择免费或认证路线。'),('turn-final','internal',15,'commentary','内部过程不可投递'),('other-turn','other',5,'final_answer','别轮秘密'),('turn-final','after-anchor',30,'final_answer','指针之后不可猜测')]:
            add_item(db,'thread-c',turn,iid,ordinal,'agentMessage',{'type':'agentMessage','id':iid,'phase':phase,'text':text})
    for t in [case.store.get_turn('thread-c','turn-final'),case.store.snapshot('thread-c').latest_turn]:
        assert t.final_agent_item_id=='item-final'
        assert '实质方案' in t.final_message
        assert tail in t.final_message
        assert t.final_message.index('实质方案')<t.final_message.index(tail)
        assert '内部过程' not in t.final_message and '别轮秘密' not in t.final_message
        assert '指针之后' not in t.final_message


def test_normal_single_final_is_byte_compatible(case):
    turn=case.store.get_turn('thread-c','turn-final')
    assert turn.final_message=='结构化最终答复'
    assert turn.final_agent_item_id=='item-final'


def test_model_input_keeps_early_answer_and_late_correction_within_budget():
    from progress_wx.models import TurnEvent
    from progress_wx.summarizer import _completed_answer_input
    parts=('实质方案'+('甲'*8000),'更正：不要执行。')
    event=TurnEvent('thread','turn','completed',final_message='\n'.join(parts),final_answer_parts=parts)
    for limit in (1000,5000,50000):
        text=_completed_answer_input(event,limit)
        assert len(text)<=limit and '实质方案' in text and '更正：不要执行。' in text
        assert text.index('实质方案')<text.index('更正')


@pytest.mark.parametrize('source,namespace,name,proof',[
    ('other-thread','codex_app','send_message_to_thread',('真实问题',)),
    ('thread-c','other','send_message_to_thread',('真实问题',)),
    ('thread-c','codex_app','untrusted',('真实问题',)),
    ('thread-c','codex_app','send_message_to_thread',('另一个问题',)),
])
def test_unverified_tool_material_is_not_a_user_request(case,source,namespace,name,proof):
    payload=delegation(source,'真实问题');payload.update(namespace=namespace,name=name)
    with closing(sqlite3.connect(case.history)) as db,db:
        add_item(db,'thread-c','turn-final','incoming',1,'functionCallOutput',payload)
    assert case.store.notification_context('thread-c','turn-final',verified_reply_texts=proof).user_request==''


def delegation(source,text):
    return {'type':'functionCallOutput','id':'incoming','namespace':'codex_app','name':'send_message_to_thread','output':f'<codex_delegation>\n<source_thread_id>{source}</source_thread_id>\n<input>{text}</input>\n</codex_delegation>'}


def test_exact_delegation_requires_successful_local_reply_evidence(case):
    with closing(sqlite3.connect(case.history)) as db,db:
        add_item(db,'thread-c','turn-final','incoming',1,'functionCallOutput',delegation('thread-c','有办法解决吗？'))
        p=delegation('other-thread','内部回报');p['id']='internal'
        add_item(db,'thread-c','turn-final','internal',18,'functionCallOutput',p)
    context=case.store.notification_context('thread-c','turn-final',verified_reply_texts=('有办法解决吗？',))
    assert context.user_request=='有办法解决吗？'
    assert 'reply_verified' in context.task_state
    assert case.store.notification_context('thread-c','turn-final').user_request==''


def test_truncated_rollout_never_uses_initial_plugin_list_as_request(case,monkeypatch):
    from progress_wx import codex_store
    sessions=Path(case.temp_dir.name)/'sessions';sessions.mkdir()
    path=sessions/'long.jsonl'
    path.write_text(json.dumps({'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'<recommended_plugins>附加推荐</recommended_plugins>'}]}})+'\n'+json.dumps({'type':'event_msg','payload':{'type':'task_complete','turn_id':'turn-final'}})+'\n',encoding='utf-8')
    with closing(sqlite3.connect(case.state)) as db,db:db.execute("UPDATE threads SET rollout_path=? WHERE id='thread-c'",(str(path),))
    monkeypatch.setattr(codex_store,'_NOTIFICATION_ROLLOUT_MAX_LINES',1)
    context=case.store.notification_context('thread-c','turn-final')
    assert context.user_request==''
    assert 'truncated' in context.task_state


def test_structured_user_content_is_text_not_python_list(case):
    with closing(sqlite3.connect(case.history)) as db,db:
        db.execute('ALTER TABLE thread_turns ADD COLUMN first_user_item_id TEXT')
        db.execute("UPDATE thread_turns SET first_user_item_id='user-exact' WHERE turn_id='turn-final'")
        add_item(db,'thread-c','turn-final','user-exact',1,'userMessage',{'type':'userMessage','id':'user-exact','content':[{'type':'text','text':'<recommended_plugins>附加内容</recommended_plugins>\n实际问题是什么？'}]})
    context=case.store.notification_context('thread-c','turn-final')
    assert context.user_request=='实际问题是什么？'


def test_first_user_and_later_real_corrections_keep_order(case):
    with closing(sqlite3.connect(case.history)) as db,db:
        db.execute('ALTER TABLE thread_turns ADD COLUMN first_user_item_id TEXT')
        db.execute("UPDATE thread_turns SET first_user_item_id='first-user' WHERE turn_id='turn-final'")
        add_item(db,'thread-c','turn-final','first-user',1,'userMessage',{'type':'userMessage','id':'first-user','content':[{'type':'text','text':'原始要求'+('甲'*2000)}]})
        add_item(db,'thread-c','turn-final','incoming',2,'functionCallOutput',delegation('thread-c','用户追加：改为免费方案'))
        add_item(db,'thread-c','turn-final','correction',3,'userMessage',{'type':'userMessage','id':'correction','content':[{'type':'text','text':'最新纠正：不要执行'}]})
    text=case.store.notification_context('thread-c','turn-final',verified_reply_texts=('用户追加：改为免费方案',)).user_request
    assert len(text)<=1200
    assert text.index('原始要求')<text.index('用户追加')<text.index('最新纠正')


@pytest.mark.parametrize('unknown', [False, True])
def test_real_service_receives_full_answer_and_verified_question_once(case,tmp_path,unknown):
    from dataclasses import replace
    import test_service as helpers
    from progress_wx.state import StateStore,CorrelationCodec
    from progress_wx.models import TurnEvent,ProgressReport,NotificationReason
    from progress_wx.service import ProgressService,snapshot_to_event
    from progress_wx.feishu import FeishuSendError
    with closing(sqlite3.connect(case.history)) as db,db:
        db.execute("UPDATE thread_items SET rollout_ordinal=20,item_json=? WHERE item_id='item-final'",(json.dumps({'type':'agentMessage','id':'item-final','phase':'final_answer','text':'方案已记录。'}),))
        add_item(db,'thread-c','turn-final','answer',10,'agentMessage',{'type':'agentMessage','id':'answer','phase':'final_answer','text':'实质答案：免费改善与认证路线。'})
        add_item(db,'thread-c','turn-final','incoming',1,'functionCallOutput',delegation('thread-c','有办法解决吗？'))
    config=helpers.make_config(tmp_path)
    config=replace(config,messaging=replace(config.messaging,backend='feishu'))
    store=StateStore(config.service.database);codec=CorrelationCodec(b'q'*32)
    try:
        parent=TurnEvent('thread-c','parent','completed',final_message='前一条通知')
        code=codec.issue();store.reserve_notification(parent,code,'前一条通知',72);store.mark_sent(parent.dedupe_key)
        delivery=store.enqueue_turn_reply(code,'synthetic-inbound','synthetic-fingerprint',codec,reply_text='有办法解决吗？')
        assert delivery is not None
        assert store.claim_turn_reply(delivery.delivery_id)
        store.mark_reply_delivered(delivery.delivery_id)
        class Summarizer(helpers.TwoStageSummarizer):
            def summarize(self,event,*,context=None,wait=None):
                self.calls+=1
                assert '实质答案' in event.final_message and '方案已记录' in event.final_message
                assert context.user_request=='有办法解决吗？'
                return ProgressReport('已回答','两条可行路线已说明。',NotificationReason.ANSWER_READY,matched_request=context.user_request)
        class Channel:
            calls=0
            def is_online(self):return True
            def send_text(self,text,*,idempotency_key):
                self.calls+=1
                if unknown:raise FeishuSendError('synthetic unknown')
                return 'synthetic-answer-message'
        service=ProgressService(config.path);service.config=config;service.store=store;service.codec=codec
        service.codex_store=case.store;service.channel=Channel();service.summarizer=Summarizer()
        helpers.install_artifact_queue(service)
        event=snapshot_to_event(case.store.snapshot('thread-c'))
        assert event is not None
        service._send_event(event)
        pending=store.claim_notification_summary(event.dedupe_key);assert pending is not None
        service._process_notification_summary(pending)
        assert service.summarizer.calls==1 and service.channel.calls==1
        service._send_event(event)
        assert store.claim_notification_summary(event.dedupe_key) is None
        assert service.channel.calls==1
        result=store.notification_summary_delivery(event.dedupe_key)
        assert result is not None and result.state==('uncertain' if unknown else 'delivered')
        assert store.notification_judgment(event.dedupe_key).user_request=='有办法解决吗？'
    finally:store.close()


@pytest.mark.parametrize('mode', ['codex_cli', 'openai_compatible'])
def test_actual_model_payload_preserves_answer_correction_and_request(monkeypatch, mode):
    from pathlib import Path
    from types import SimpleNamespace
    from progress_wx.config import SummaryConfig
    from progress_wx.models import TurnEvent, NotificationContext
    from progress_wx.summarizer import ProgressSummarizer

    observed = {}
    answer = '实质答案：免费路线可改善诊断。' + '正文解释。' * 12000
    correction = '更正：上述路线不能保证完整覆盖。'
    event = TurnEvent('thread', 'turn', 'completed', final_message=correction,
                      final_answer_parts=(answer, correction))
    result = {'status': '已回答', 'details': '方案及限制已说明。',
              'notification_reason': 'answer_ready'}

    def fake_run(argv, **kwargs):
        instructions, raw = kwargs['input'].split('\n\n输入 JSON：\n', 1)
        observed.update(instructions=instructions, context=json.loads(raw))
        Path(argv[argv.index('--output-last-message') + 1]).write_text(
            json.dumps(result, ensure_ascii=False), encoding='utf-8')
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self, _limit):
            return json.dumps({'status': 'completed', 'output_text': json.dumps(result)}).encode()

    class Opener:
        def open(self, request, timeout):
            payload = json.loads(request.data)
            observed.update(instructions=payload['input'][0]['content'],
                            context=json.loads(payload['input'][1]['content']))
            return Response()

    monkeypatch.setattr('progress_wx.summarizer.shutil.which', lambda _: 'codex')
    monkeypatch.setattr('progress_wx.summarizer.subprocess.run', fake_run)
    monkeypatch.setattr('progress_wx.summarizer.urllib.request.build_opener', lambda *_: Opener())
    local = SummaryConfig(mode=mode, endpoint='http://127.0.0.1:11434/v1', model='local',
                          api_key_env='UNUSED_TEST_KEY', min_interval_seconds=0,
                          max_input_chars=1000)
    report = ProgressSummarizer(local).summarize(
        event, context=NotificationContext(user_request='有办法解决吗？'))
    material = observed['context']['completed_assistant_response']
    assert material.index('实质答案') < material.index(correction)
    assert '…' in material
    assert len(material) == (1000 if mode == 'codex_cli' else 50000)
    assert observed['context']['notification_context']['user_request'] == '有办法解决吗？'
    assert 'answer_ready' in observed['instructions']
    assert '更正' in observed['instructions']
    assert '只要回复还表示' not in observed['instructions']
    assert report.notification_reason == 'answer_ready'
