"""Notification policy context, persistence, and retry safety tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from progress_wx.codex_store import CodexStore, StorePaths
from progress_wx.models import (
    NotificationContext,
    NotificationReason,
    ProgressReport,
    TurnEvent,
)
from progress_wx.state import StateStore
from progress_wx.summarizer import (
    ProgressSummarizer,
    _model_context,
    _validated_report,
    fallback_report,
)
from progress_wx.config import SummaryConfig


def _event(thread_id: str, turn_id: str) -> TurnEvent:
    return TurnEvent(
        thread_id,
        turn_id,
        "completed",
        title="策略测试",
        final_message="当前用户问题已经得到明确答复，并完成了核验。",
    )


def test_policy_schema_accepts_six_categories_and_audit_fields() -> None:
    report = _validated_report(
        {
            "status": "完成",
            "details": "问题已经回答。",
            "notification_reason": "answer_ready",
            "reason": "本轮直接回答了用户问题。",
            "request": "用户询问通知规则。",
            "new_facts": ["六分类已启用。"],
        }
    )
    assert report.notification_reason == "answer_ready"
    assert report.decision_reason == "本轮直接回答了用户问题。"
    assert report.matched_request == "用户询问通知规则。"
    assert report.new_facts == ("六分类已启用。",)
    assert report.is_task_complete is False


def test_fallback_report_marks_model_failure_instead_of_a_silent_decision() -> None:
    report = fallback_report(_event("thread-fallback", "turn-fallback"))
    assert report.notification_reason == NotificationReason.SILENT.value
    assert report.has_model_error is True
    assert report.model_error == "semantic_summary_unavailable"
    assert "模型分类失败" in report.decision_reason


def test_model_context_redacts_secrets_paths_urls_and_hashes() -> None:
    context = NotificationContext(
        user_request=(
            "请检查 D:/private/answer.txt，token=super-secret，"
            "https://example.invalid/a，"
            "0123456789abcdef0123456789abcdef"
        ),
        task_state="completed",
        recent_successful_notifications=("上一条摘要",),
    )
    payload = _model_context(context)
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "super-secret" not in encoded
    assert "D:/private" not in encoded
    assert "https://" not in encoded
    assert "0123456789abcdef0123456789abcdef" not in encoded
    assert payload["recent_successful_notifications"] == ["上一条摘要"]


def test_notification_judgment_is_bounded_idempotent_and_retains_failures(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    try:
        event = _event("thread-policy", "turn-policy")
        store.reserve_notification_summary_only(event, "PCWX-TEST", 72)
        context = NotificationContext(
            user_request="请回答这个问题。",
            task_state="turn_status=completed; exact_turn=true",
            recent_successful_notifications=("之前已成功通知。",),
        )
        first = store.record_notification_judgment(
            event.dedupe_key,
            ProgressReport(
                "完成",
                "已回答。",
                NotificationReason.ANSWER_READY,
                decision_reason="直接回答了当前问题。",
                matched_request="请回答这个问题。",
                new_facts=("答案已形成。",),
            ),
            context,
            now=100,
        )
        assert first.model_attempts == 1
        assert first.input_digest and len(first.input_digest) == 64
        assert first.recent_successful_notifications == ("之前已成功通知。",)

        second = store.record_notification_judgment(
            event.dedupe_key,
            ProgressReport(
                "未知",
                "分类暂时失败。",
                NotificationReason.SILENT,
                decision_reason="模型失败，保留重试。",
                model_error="timeout",
            ),
            context,
            now=101,
        )
        assert second.created_at == 100
        assert second.updated_at == 101
        assert second.model_attempts == 2
        assert second.model_error == "timeout"
        assert second.notification_reason == "silent"
        assert store.notification_judgment(event.dedupe_key) == second
    finally:
        store.close()


def test_recent_successful_notification_context_excludes_pending_and_current(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    try:
        delivered = _event("thread-history", "turn-delivered")
        pending = _event("thread-history", "turn-pending")
        current = _event("thread-history", "turn-current")
        for event, code in (
            (delivered, "PCWX-D"),
            (pending, "PCWX-P"),
            (current, "PCWX-C"),
        ):
            store.reserve_notification_summary_only(event, code, 72)
        claim = store.claim_notification_summary(delivered.dedupe_key)
        assert claim is not None
        assert store.prepare_notification_summary(delivered.dedupe_key, "已成功摘要")
        assert store.mark_notification_summary_submitted(delivered.dedupe_key, ["om-d"])
        assert store.mark_notification_summary_delivered(delivered.dedupe_key, ["om-d"])
        assert store.recent_successful_notification_context(
            "thread-history", current_event_key=current.dedupe_key
        ) == ("已成功摘要",)
    finally:
        store.close()


def test_summarizer_passes_policy_context_and_rekeys_cache(monkeypatch) -> None:
    config = SummaryConfig(
        mode="openai_compatible",
        endpoint="http://127.0.0.1:11434/v1",
        model="local-test",
        api_key_env="MISSING",
        min_interval_seconds=0,
    )
    summarizer = ProgressSummarizer(config)
    captured: list[NotificationContext] = []

    def request(_event, *, context):
        captured.append(context)
        return ProgressReport(
            "完成",
            "回答已准备。",
            NotificationReason.ANSWER_READY,
            decision_reason="直接答复。",
        )

    monkeypatch.setattr(summarizer, "_request", request)
    event = _event("thread-cache", "turn-cache")
    context = NotificationContext(
        user_request="用户原问题",
        task_state="completed",
        recent_successful_notifications=("旧摘要",),
    )
    report = summarizer.summarize(event, context=context)
    assert report.notification_reason == "answer_ready"
    assert captured == [context]
    assert any(key.startswith(event.dedupe_key + ":ctx-") for key in summarizer._cache)


def test_codex_store_notification_context_stops_at_exact_turn(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-policy.jsonl"
    rows = [
        {"type": "event_msg", "payload": {"type": "user_message", "turn_id": "turn-1", "message": "首轮问题"}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1"}},
        {"type": "event_msg", "payload": {"type": "user_message", "turn_id": "turn-2", "message": "后续问题不应混入"}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-2"}},
    ]
    rollout.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    state = codex_home / "state_5.sqlite"
    history = codex_home / "thread_history_1.sqlite"
    with sqlite3.connect(state) as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, name TEXT, cwd TEXT, updated_at_ms INTEGER, created_at_ms INTEGER, archived INTEGER, preview TEXT, source TEXT, thread_source TEXT, rollout_path TEXT)"
        )
        connection.execute(
            "INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("thread-policy", "策略测试", "", "D:/work", 2, 1, 0, "", "", "user", str(rollout)),
        )
    with sqlite3.connect(history) as connection:
        connection.execute(
            "CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, rollout_ordinal INTEGER, status TEXT, error_json TEXT, started_at INTEGER, completed_at INTEGER, duration_ms INTEGER, final_agent_item_id TEXT)"
        )
        connection.executemany(
            "INSERT INTO thread_turns VALUES(?,?,?,?,?,?,?,?,?)",
            [
                ("thread-policy", "turn-1", 1, "completed", None, 1, 2, None, None),
                ("thread-policy", "turn-2", 2, "completed", None, 3, 4, None, None),
            ],
        )
    store = CodexStore(
        paths=StorePaths(state, history, codex_home / "session_index.jsonl")
    )
    context = store.notification_context("thread-policy", "turn-1")
    assert context.user_request == "首轮问题"
    assert "后续问题不应混入" not in context.user_request
    assert "turn_status=completed" in context.task_state
