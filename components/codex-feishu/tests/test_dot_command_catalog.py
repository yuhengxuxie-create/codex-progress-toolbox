"""Explicit public entry vocabulary; do not normalize input in a helper."""
from types import SimpleNamespace
import pytest
from progress_wx.channel import ChannelReply
from progress_wx.codex_management import CodexManagementController, LEGACY_TEXT_COMMANDS
from progress_wx.state import StateStore


NAMES = (
    'Codex 管理','Codex管理','Skills','个人会话','使用说明','关闭自动监测',
    '切换会话','切换当前会话','剩余额度','功能中心','开启自动监测','当前会话',
    '指令使用','搜索会话','文字版使用说明','斜杠指令','新建个人会话','新建项目',
    '最近预警','查看当前会话','查看指令列表','查看监测','查询个人会话','查询会话',
    '查询剩余额度','查询监测列表','查询项目列表','添加监测','添加监测任务',
    '清除会话绑定','清除当前会话','监测设置','移除监测','移除监测任务','远程控制',
    '重置预警状态','项目会话','项目新会话','预警状态',
)


@pytest.fixture
def controller(tmp_path):
    store=StateStore(tmp_path/'synthetic.sqlite')
    sent=[]
    def send(text,key):
        sent.append(text)
        return (f'synthetic-{len(sent)}',)
    def forbidden(*args,**kwargs):
        raise AssertionError('deprecated or malformed text reached Desktop')
    manager=CodexManagementController(store=store,codex_store=SimpleNamespace(),
        desktop_client=SimpleNamespace(open_verified=forbidden),project_registry=SimpleNamespace(),
        source_thread_ids=('synthetic',),send_text=send)
    try:
        yield manager,store,sent
    finally:
        store.close()


@pytest.mark.parametrize('name',NAMES)
def test_every_old_direct_alias_only_explains_required_dot(controller,name):
    manager,store,sent=controller
    old=ChannelReply('synthetic-owner',name,message_id='old',chat_id='synthetic-chat')
    new=ChannelReply('synthetic-owner','.'+name,message_id='new',chat_id='synthetic-chat')
    assert manager.accepts(old) and manager.accepts(new)
    manager.handle(old)
    manager.handle(old)
    assert len(sent)==1
    assert '请发送“.'+name+'”' in sent[0] and '没有执行' in sent[0]
    assert store.management_inbound_status('old',sender_id=old.sender_id,content=name)=='accepted'


def test_explicit_vocabulary_matches_all_natural_aliases():
    assert set(NAMES)==set(LEGACY_TEXT_COMMANDS)


@pytest.mark.parametrize('text',['.未知指令','..功能中心','.功能中心\n正文','.功能中心 ','.发送','.取消','.'])
def test_unknown_or_malformed_dot_is_not_a_prompt(controller,text):
    manager,_,sent=controller
    message=ChannelReply('synthetic-owner',text,message_id='invalid',chat_id='synthetic-chat')
    assert manager.accepts(message)
    manager.handle(message)
    assert len(sent)==1 and '没有向 Codex 发送正文' in sent[0]


@pytest.mark.parametrize('text',['正文中间有.功能中心','请帮我新建个人会话',' .功能中心','．功能中心','。功能中心'])
def test_nonliteral_ascii_prefix_never_becomes_a_bot_entry(controller,text):
    manager,_,_=controller
    assert not manager.accepts(ChannelReply('synthetic-owner',text,message_id='ordinary',chat_id='synthetic-chat'))


# Expected destinations are explicit: exercise handle and its persisted output,
# not just membership in the router's accepted vocabulary.
DESTINATIONS = {
    'Codex 管理':'remote_control_menu', 'Codex管理':'remote_control_menu',
    'Skills':'remote_control_menu', '个人会话':'personal_list',
    '使用说明':'usage_guide', '关闭自动监测':'monitor_settings',
    '切换会话':'remote_control_menu', '切换当前会话':'remote_control_menu',
    '剩余额度':'account_rate_limits', '功能中心':'feature_center',
    '开启自动监测':'monitor_settings', '当前会话':'current_binding',
    '指令使用':'remote_control_menu', '搜索会话':'session_search_form',
    '文字版使用说明':'usage_guide', '斜杠指令':'slash_catalog',
    '新建个人会话':'new_personal_thread_form', '新建项目':'new_project_form',
    '最近预警':'reset_alert_recent', '查看当前会话':'current_binding',
    '查看指令列表':'slash_catalog', '查看监测':'monitor_list',
    '查询个人会话':'personal_list', '查询会话':'session_query_menu',
    '查询剩余额度':'account_rate_limits', '查询监测列表':'monitor_list',
    '查询项目列表':'project_list', '添加监测':'monitor_add_form',
    '添加监测任务':'monitor_add_form', '清除会话绑定':'current_binding',
    '清除当前会话':'current_binding', '监测设置':'monitor_settings',
    '移除监测':'monitor_remove_form', '移除监测任务':'monitor_remove_form',
    '远程控制':'remote_control_menu', '重置预警状态':'reset_alert_status',
    '项目会话':'project_list', '项目新会话':'project_list',
    '预警状态':'reset_alert_status',
}


@pytest.mark.parametrize('name', NAMES)
def test_every_new_dot_alias_reaches_its_actual_destination(tmp_path, name):
    from test_codex_management import (
        _controller, CaptureCardSender, CaptureImageSender, FakeRemoteControl,
    )
    from progress_wx.codex_account import CodexAccountError

    def unavailable_account():
        raise CodexAccountError('synthetic offline account')

    cards = CaptureCardSender()
    manager, state, desktop, sender = _controller(
        tmp_path, card_sender=cards, image_sender=CaptureImageSender(),
        account_reader=SimpleNamespace(read=unavailable_account),
        session_search=SimpleNamespace(), remote_control=FakeRemoteControl(),
    )
    try:
        message = ChannelReply('ou_owner', '.' + name,
                               message_id='new-dot', chat_id='oc_private')
        manager.handle(message)
        outputs = cards.cards + sender.messages
        assert outputs
        contexts = [state.management_context_record_for_message(row[0]) for row in outputs]
        assert {context.context_kind for context in contexts if context} == {DESTINATIONS[name]}
        if name == 'Skills':
            assert contexts[-1].payload['pending_command'] == '/skills'
        if name == '项目新会话':
            assert contexts[-1].payload['selection_mode'] == 'new_task_choose'
        count = len(cards.cards) + len(sender.messages)
        manager.handle(message)
        assert len(cards.cards) + len(sender.messages) == count
        assert desktop.created == [] and desktop.sent_prompts == []
    finally:
        state.close()
