from types import SimpleNamespace
import pytest
from progress_wx.remote_control import PreparedRemoteSession
from progress_wx.codex_rpc import CodexRPCRejected
from progress_wx.codex_store import ThreadRecord, _resolve_thread_title, public_thread_title, independent_thread_title

@pytest.mark.parametrize('result,expected',[
    ({'thread':{'model':'model-new','reasoningEffort':'medium'}},('model-new','medium')),
    ({'thread':{},'model':'legacy','reasoningEffort':'high'},('legacy','high')),
    ({'thread':{'model':None,'reasoningEffort':None},'model':'stale','reasoningEffort':'high'},('','')),
    ({'thread':{}},('','')),
    ({'thread':{'model':'new','reasoningEffort':'low'},'model':'old'},('new','low')),
])
def test_runtime_reads_official_nested_settings_without_guessing(result,expected):
    rpc=SimpleNamespace(read_thread=lambda *a,**k:{'result':result})
    session=PreparedRemoteSession(rpc,'synthetic',frozenset(),lambda:None)
    snapshot=session.runtime_snapshot()
    assert (snapshot.model,snapshot.effort)==expected

@pytest.mark.parametrize('code',[-32601,-32602,-32603,-32600,123,None,True,'-32602'])
def test_rejection_exposes_only_numeric_code(code):
    exc=CodexRPCRejected('rejected',error={'code':code,'message':'SECRET /private/path','data':{'token':'SECRET'}})
    assert 'SECRET' not in str(exc)+exc.safe_detail+repr(exc.__dict__)
    assert exc.code == (code if type(code) is int else None)

def test_explicit_indexed_name_equal_to_first_request_is_not_hidden():
    title,source=_resolve_thread_title(name='',sqlite_title='整理示例项目',session_title='整理示例项目',preview='整理示例项目')
    record=ThreadRecord('synthetic',title=title,preview=title,title_source=source,raw={'title':title,'preview':title})
    assert source=='session_index_name'
    assert public_thread_title(record)[0]==title

def test_authoritative_desktop_title_equal_to_request_is_not_hidden():
    record=ThreadRecord('synthetic',title='整理示例项目',preview='整理示例项目',raw={'preview':'整理示例项目'})
    assert independent_thread_title(record,'整理示例项目')=='整理示例项目'
    assert public_thread_title(record)[1]=='unavailable'

def test_long_truncated_prompt_without_an_explicit_name_stays_unavailable():
    prompt='请分析这个合成程序并修复错误和完成测试。'*10
    title,source=_resolve_thread_title(name='',sqlite_title=prompt,session_title=prompt[:30]+'…',preview=prompt)
    record=ThreadRecord('synthetic',title=title,preview=prompt,title_source=source,raw={'preview':prompt})
    assert public_thread_title(record)[1]=='unavailable'

def test_official_desktop_name_is_preserved_even_if_it_resembles_a_prompt():
    prompt='请检查这个合成工具并修复问题。'*10
    title=prompt[:30]+'…'
    record=ThreadRecord('synthetic',title=title,preview=prompt,raw={'preview':prompt})
    assert independent_thread_title(record,title)==title

def test_global_skills_never_loads_or_executes_a_thread():
    from progress_wx.remote_control import AppServerRemoteControl
    from pathlib import Path
    calls=[]
    class RPC:
        def initialize(self): calls.append('initialize')
        def request(self,method,params):
            calls.append(method)
            assert method=='skills/list'
            assert params['forceReload'] is True
            assert params['cwds']==[str(Path.home())]
            return {'result':{'data':[{'cwd':str(Path.home()),'errors':[],'skills':[
                {'name':'personal','path':'/synthetic/skill','enabled':True,'scope':'user'},
                {'name':'project','path':'/synthetic/project','enabled':True,'scope':'repo'},
                {'name':'disabled','path':'/synthetic/disabled','enabled':False,'scope':'user'},
            ]}]}}
        def close(self):calls.append('close')
    result=AppServerRemoteControl(RPC).global_skills()
    assert [s.name for s in result]==['personal']
    assert calls==['initialize','skills/list','close']

@pytest.mark.parametrize('command',['/skills','Skills','/commands','斜杠指令'])
def test_browse_entry_does_not_resolve_or_select_current_thread(tmp_path,command):
    from test_codex_management import _controller,_message
    controller,state,tools,sender=_controller(tmp_path)
    controller.remote_control=SimpleNamespace(global_skills=lambda:())
    def forbidden(*a,**k):raise AssertionError('browse must not resolve a thread')
    controller._resolve_current_thread=forbidden
    controller._open_desktop=forbidden
    try:
        controller._handle_top(_message('browse',command),contextual=True)
        assert sender.messages
    finally:state.close()

@pytest.mark.parametrize('case',['rotation','old_generation','conflict','identity_change'])
def test_discovery_uses_live_pipe_generation_not_mtime(tmp_path,case):
    from progress_wx.codex_app_tools import DesktopAppToolsClient,DesktopAppToolsUnavailable
    from datetime import datetime,timezone
    born=datetime(2026,9,12,tzinfo=timezone.utc).timestamp()
    calls=[]
    def identity():
        calls.append(1)
        return (123,born+1) if case=='identity_change' and len(calls)>1 else (123,born)
    session=SimpleNamespace(source_pipe_path='synthetic-current-pipe',pipe=SimpleNamespace(server_identity=identity),close=lambda:None)
    client=DesktopAppToolsClient(tmp_path,live_pipe_names=lambda:[])
    client.open_verified=lambda **k:session
    stamp='2026-09-11T00:00:00Z' if case=='old_generation' else '2026-09-12T00:00:01Z'
    line=stamp+' info browser_use_runtime_paths_selected codexCliPath=C:\\official\\v1\\codex.exe codexCliPathSource=bundled-or-dev\n'
    (tmp_path/'codex-desktop-synthetic-123-t0-rotation.log').write_text(line,encoding='utf-8')
    if case=='conflict':
        (tmp_path/'codex-desktop-synthetic-123-t0-other.log').write_text(line.replace('v1','v2'),encoding='utf-8')
    if case=='rotation':
        assert client.discover_current_codex_cli()==r'C:\official\v1\codex.exe'
    else:
        with pytest.raises(DesktopAppToolsUnavailable):client.discover_current_codex_cli()

def test_global_catalog_card_pages_are_owner_bound_and_have_no_execution_form(tmp_path):
    import json
    from test_codex_management import _controller,_message,CaptureCardSender
    from progress_wx.remote_control import SkillSnapshot
    from progress_wx.codex_management import ManagementUserError
    cards=CaptureCardSender(); reads=[]
    skills=tuple(SkillSnapshot(name=f'skill-{n}',path=f'/private/{n}',description='Synthetic description',display_name='') for n in range(11))
    def read(): reads.append(1);return skills
    controller,state,tools,sender=_controller(tmp_path,card_sender=cards,remote_control=SimpleNamespace(global_skills=read))
    def forbidden(*a,**k):raise AssertionError('no thread operation while browsing')
    controller._resolve_current_thread=forbidden
    controller._open_desktop=forbidden
    try:
        controller.handle(_message('open','.Skills'))
        message_id,card,_=cards.cards[-1]
        text=json.dumps(card,ensure_ascii=False)
        assert 'skill_start_form' not in text and '/private/' not in text
        assert 'Synthetic description' in text
        context=state.management_context_record_for_message(message_id)
        assert context.context_kind=='skills_catalog'
        assert context.sender_id=='ou_owner'
        controller.handle(_message('next','/skills page 2',reply_to=message_id))
        assert len(reads)==2
        with pytest.raises(ManagementUserError):
            controller.handle(_message('wrong-owner','/skills page 1',reply_to=message_id,sender_id='ou_other'))
        with pytest.raises(ManagementUserError):
            controller.handle(_message('expired','/skills page 1',reply_to='missing-card'))
        assert len(reads)==2
        assert state.current_thread('ou_owner','oc_private') is None
    finally:state.close()

def test_rpc_transport_preserves_code_without_raw_error_content():
    import sys
    from progress_wx.codex_rpc import CodexAppServer
    script='''import sys,json
for line in sys.stdin:
 m=json.loads(line)
 if 'id' not in m: continue
 if m.get('method')=='initialize': r={'result':{}}
 else:r={'error':{'code':-32602,'message':'SECRET /private/fixture','data':{'token':'SECRET'}}}
 r['id']=m['id'];print(json.dumps(r),flush=True)
'''
    rpc=CodexAppServer([sys.executable,'-u','-c',script],timeout_seconds=2)
    try:
        rpc.initialize()
        with pytest.raises(CodexRPCRejected) as caught:
            rpc.request('thread/settings/update',{'threadId':'synthetic','model':'synthetic'})
        assert caught.value.code==-32602
        assert 'SECRET' not in str(caught.value)+repr(caught.value.__dict__)
    finally:rpc.close()
