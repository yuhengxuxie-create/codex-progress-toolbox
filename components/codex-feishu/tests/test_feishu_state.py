"""飞书消息 ID 与一次性 HMAC 通知的持久关联测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from progress_wx.models import TurnEvent
from progress_wx.state import CorrelationCodec, StateError, StateStore


def _codec() -> CorrelationCodec:
    return CorrelationCodec(b"x" * 32)


def test_channel_message_mapping_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    event = TurnEvent("thread", "turn", "completed")
    codec = _codec()
    code = codec.issue()
    store = StateStore(database)
    store.reserve_notification(event, code, "正文", 72)
    store.bind_channel_message(event.dedupe_key, "om_notice")
    store.mark_sent(event.dedupe_key)
    store.close()

    reopened = StateStore(database)
    try:
        assert reopened.code_for_channel_message("om_notice") == code
        assert reopened.peek_reply(code, codec) == ("thread", "turn", "turn")
    finally:
        reopened.close()


def test_channel_message_id_cannot_be_rebound_or_shared(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    first = TurnEvent("thread", "turn-1", "completed")
    second = TurnEvent("thread", "turn-2", "completed")
    codec = _codec()
    try:
        store.reserve_notification(first, codec.issue(), "一", 72)
        store.reserve_notification(second, codec.issue(), "二", 72)
        store.bind_channel_message(first.dedupe_key, "om_one")
        with pytest.raises(StateError):
            store.bind_channel_message(first.dedupe_key, "om_other")
        with pytest.raises(StateError):
            store.bind_channel_message(second.dedupe_key, "om_one")
    finally:
        store.close()


def test_all_message_chunks_route_to_same_notification_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    event = TurnEvent("thread", "long-turn", "completed")
    codec = _codec()
    code = codec.issue()
    store = StateStore(database)
    store.reserve_notification(event, code, "长正文", 72)
    store.bind_channel_messages(
        event.dedupe_key,
        ("om_chunk_1", "om_chunk_2", "om_chunk_3"),
    )
    store.mark_sent(event.dedupe_key)
    store.close()

    reopened = StateStore(database)
    try:
        assert reopened.code_for_channel_message("om_chunk_1") == code
        assert reopened.code_for_channel_message("om_chunk_2") == code
        assert reopened.code_for_channel_message("om_chunk_3") == code
    finally:
        reopened.close()


def test_message_chunk_cannot_be_shared_between_notifications(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    first = TurnEvent("thread", "long-1", "completed")
    second = TurnEvent("thread", "long-2", "completed")
    codec = _codec()
    try:
        store.reserve_notification(first, codec.issue(), "一", 72)
        store.reserve_notification(second, codec.issue(), "二", 72)
        store.bind_channel_messages(first.dedupe_key, ("om_first", "om_shared"))
        with pytest.raises(StateError, match="另一条通知"):
            store.bind_channel_messages(second.dedupe_key, ("om_second", "om_shared"))
    finally:
        store.close()


def test_management_context_survives_restart_and_all_chunks_resolve(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    store = StateStore(database)
    context_id = store.create_management_context(
        "project_list", {"projects": [{"label": "A01", "project_id": "p1"}]}
    )
    store.bind_management_messages(context_id, ("om_page_1", "om_page_2"))
    store.close()

    reopened = StateStore(database)
    try:
        expected = ("project_list", {"projects": [{"label": "A01", "project_id": "p1"}]})
        assert reopened.management_context_for_message("om_page_1") == expected
        assert reopened.management_context_for_message("om_page_2") == expected
        assert reopened.management_context_for_message("om_page_1", now=4_000_000_000) == expected
        reopened.prune(now=4_000_000_000)
        assert reopened.management_context_for_message("om_page_2", now=4_000_000_000) == expected
    finally:
        reopened.close()


def test_management_inbound_message_is_reserved_once(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    try:
        assert store.reserve_management_inbound("om_in", "ou_owner", "新建个人会话") is True
        assert store.reserve_management_inbound("om_in", "ou_owner", "被重投的不同正文") is False
        store.complete_management_inbound("om_in")
    finally:
        store.close()
