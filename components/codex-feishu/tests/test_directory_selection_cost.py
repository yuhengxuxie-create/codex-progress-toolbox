import sqlite3
import json
from pathlib import Path
from contextlib import closing
import pytest
import test_codex_store as fixtures


@pytest.fixture
def catalog():
    case = fixtures.CodexStoreTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def test_directory_selection_does_not_resolve_unread_rollout_paths(catalog, monkeypatch):
    def unexpected(_):
        pytest.fail('Directory-only selection must not resolve rollout file ownership')
    monkeypatch.setattr(catalog.store, '_rollout_path_key', unexpected)
    assert catalog.store.get_thread('thread-a').title == '支付回调'
    assert catalog.store.select_threads(title='支付回调')[0].thread_id == 'thread-a'
    for name in ('find_threads', 'find', 'select', 'query_threads'):
        assert getattr(catalog.store, name)(thread_id='thread-a')[0].thread_id == 'thread-a'
    catalog.store.require_readable()


@pytest.mark.parametrize('reader', ['snapshot','latest_turn','get_turn','latest_terminal_turn','latest_completed_result_turn'])
def test_directory_then_actual_rollout_reader_keeps_shared_alias_guard(catalog, reader):
    sessions=Path(catalog.temp_dir.name)/'sessions'
    (sessions/'sub').mkdir(parents=True)
    rollout=sessions/'shared.jsonl'
    rollout.write_text(json.dumps({'type':'event_msg','payload':{'type':'task_complete','turn_id':'turn-shared','last_agent_message':'private test body'}})+'\n',encoding='utf-8')
    with closing(sqlite3.connect(catalog.state)) as db, db:
        for tid,path in [('owner-a',rollout),('owner-b',sessions/'sub'/'..'/'shared.jsonl')]:
            db.execute('INSERT INTO threads(id,title,cwd,rollout_path) VALUES(?,?,?,?)',(tid,'共享','D:/repo',str(path)))
    assert catalog.store.get_thread('owner-a') is not None
    method=getattr(catalog.store,reader)
    value=method('owner-a','turn-shared') if reader=='get_turn' else method('owner-a')
    if reader=='snapshot':
        assert value.latest_turn is None
    else:
        assert value is None
    with pytest.raises(fixtures.CodexStoreReadError):
        catalog.store.require_readable()
    assert 'private test body' not in repr(value)


def test_selection_observes_immediate_rename_discovery_and_archive(catalog):
    assert catalog.store.get_thread('thread-a').title == '支付回调'
    with closing(sqlite3.connect(catalog.state)) as db, db:
        db.execute("UPDATE threads SET name='人工新标题' WHERE id='thread-a'")
        db.execute("INSERT INTO threads(id,title,cwd,archived) VALUES('new','新任务','D:/repo',0)")
        db.execute("UPDATE threads SET archived=1 WHERE id='thread-b'")
    assert catalog.store.get_thread('thread-a').title == '人工新标题'
    assert catalog.store.get_thread('new').title == '新任务'
    assert 'thread-b' not in {r.thread_id for r in catalog.store.select_threads()}


def test_snapshot_rebuilds_ownership_after_directory_selection(catalog, monkeypatch):
    calls=[]
    original=catalog.store._rollout_path_key
    def counted(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(catalog.store, '_rollout_path_key', counted)
    catalog.store.select_threads()
    calls.clear()
    catalog.store.snapshot('thread-a').require_readable()
    assert len(calls) >= 4


def test_repeated_failed_directory_read_never_becomes_healthy(catalog):
    with closing(sqlite3.connect(catalog.state)) as db, db:
        db.execute('ALTER TABLE threads RENAME TO unavailable_threads')
    for _ in range(2):
        assert catalog.store.get_thread('thread-a') is None
        with pytest.raises(fixtures.CodexStoreReadError):
            catalog.store.require_readable()
    with closing(sqlite3.connect(catalog.state)) as db, db:
        db.execute('ALTER TABLE unavailable_threads RENAME TO threads')
    assert catalog.store.get_thread('thread-a') is not None
    catalog.store.require_readable()
