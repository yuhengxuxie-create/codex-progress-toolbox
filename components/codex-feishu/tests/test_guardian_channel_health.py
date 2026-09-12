from test_guardian import FakeChannel, config
from progress_wx.guardian import Guardian, guardian_status


class ObservedChannel(FakeChannel):
    def __init__(self, snapshot):
        super().__init__()
        self.online = False
        self.snapshot = snapshot

    def connection_snapshot(self):
        return self.snapshot


def test_fatal_dependency_is_failed_without_exception_content(tmp_path):
    ch = ObservedChannel({'state': 'failed', 'last_failure_type': 'FeishuDependencyError',
                          'thread_alive': False, 'exception': 'private token'})
    g = Guardian(config(tmp_path), ch)
    try:
        g.tick()
        state = g.store.get('channel')
        assert state['state'] == 'failed'
        assert state['last_error_code'] == 'channel_dependency_failed'
        assert 'private token' not in str(state)
        assert guardian_status(config(tmp_path))['last_error_code'] == 'channel_dependency_failed'
        row = g.store.db.execute("select payload from outgoing where key like 'system:disconnected:%'").fetchone()
        assert '正在尝试恢复' not in row[0]
        assert '连接已停止' in row[0]
    finally:
        g.store.close()


def test_dead_retry_thread_is_failed(tmp_path):
    g = Guardian(config(tmp_path), ObservedChannel({'state': 'offline', 'thread_alive': False}))
    try:
        g.tick()
        assert g.store.get('channel')['state'] == 'failed'
        assert g.store.get('channel')['last_error_code'] == 'channel_thread_exited'
    finally:
        g.store.close()


def test_actual_retry_keeps_reconnecting_and_recovery_clears_channel_error(tmp_path):
    ch = ObservedChannel({'state': 'offline', 'thread_alive': True})
    g = Guardian(config(tmp_path), ch)
    try:
        g.tick()
        assert g.store.get('channel')['state'] == 'reconnecting'
        notice_count = g.store.db.execute("select count(*) from outgoing").fetchone()[0]
        row = g.store.db.execute("select payload from outgoing where key like 'system:disconnected:%'").fetchone()
        assert '正在尝试恢复' in row[0]
        ch.snapshot.update(state='failed', thread_alive=False)
        g.tick()
        assert g.store.get('channel')['state'] == 'failed'
        assert g.store.db.execute("select count(*) from outgoing").fetchone()[0] == notice_count
        ch.snapshot.update(state='online', thread_alive=True)
        ch.online = True
        g.tick()
        assert g.store.get('channel')['state'] == 'online'
        assert g.store.get('channel')['last_error_code'] is None
    finally:
        g.store.close()
