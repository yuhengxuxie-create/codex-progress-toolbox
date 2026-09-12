"""Synthetic database fixture for the real v1.5.0 package upgrade test."""
from pathlib import Path
import json
import sqlite3
import sys

mode, root_text = sys.argv[1:3]
root = Path(root_text).resolve()
sys.path.insert(0, str(root / "components/codex-feishu/src"))
from progress_wx.state import StateStore

database = root / "components/codex-feishu/.state/progress-wx.sqlite"
if mode == "seed":
    StateStore(database).close()
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "10"
        for i, delivered in ((1, 103), (2, None)):
            db.execute("INSERT INTO notifications(event_key,code,thread_id,turn_id,message_text,created_at,expires_at,channel_message_id,consumed_at,reply_fingerprint,reply_text,claimed_at,delivered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (f"e{i}", f"code{i}", f"thread{i}", f"turn{i}", "synthetic history", 100, 9999999999, f"om{i}", 101, f"fp{i}", "synthetic reply", 102, delivered))
            db.execute("INSERT INTO notification_message_ids VALUES(?,?,?)", (f"om{i}", f"e{i}", 100))
        db.execute("INSERT INTO processed_turns VALUES(?,?)", ("old-completed", 100))
        db.execute("INSERT INTO monitor_subscriptions VALUES(?,?,?,?,?)", ("synthetic-task", "manual", 100, 101, 9999999999))
        db.execute("INSERT INTO management_contexts VALUES(?,?,?,?,?)", ("old-context", "threads", '{"synthetic":true}', 100, 9999999999))
        db.execute("INSERT INTO meta VALUES(?,?)", ("synthetic-owner-marker", "preserved"))
elif mode == "check":
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "23"
        columns = {row[1] for row in db.execute("PRAGMA table_info(artifact_file_deliveries)")}
        assert {"media_kind", "notice_key", "snapshot_json", "state"} <= columns
        assert db.execute("SELECT value FROM meta WHERE key='synthetic-owner-marker'").fetchone()[0] == "preserved"
        assert db.execute("SELECT parent_code,claimed_at,delivered_at,receipt_required,receipt_sent_at FROM reply_deliveries ORDER BY parent_code").fetchall() == [("code1",102,103,0,103),("code2",102,None,0,None)]
        assert db.execute("SELECT count(*) FROM monitor_subscriptions").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM management_contexts").fetchone()[0] == 1
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    StateStore(database).close()
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM reply_deliveries").fetchone()[0] == 2
else:
    raise ValueError(mode)
print(json.dumps({"mode": mode, "result": "passed"}))
