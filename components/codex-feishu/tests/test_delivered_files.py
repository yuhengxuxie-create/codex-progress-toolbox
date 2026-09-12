from pathlib import Path

from progress_wx.delivered_files import (
    discover_delivered_files,
    make_final_agent_item,
)


def _final(item_id: str, text: str, *, turn_id: str = "turn-1") -> dict[str, object]:
    return {
        "thread_id": "thread-1",
        "turn_id": turn_id,
        "item_id": item_id,
        "item_json": make_final_agent_item(item_id=item_id, text=text),
        "item_type": "agentMessage",
    }


def test_explicit_delivery_supports_windows_spaces_chinese_angle_and_file_uri(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "成果 文件 中文.weird-extension"
    artifact.write_bytes(b"verified artifact")
    uri = artifact.as_uri()
    text = f"交付文件：[{artifact.name}](<{artifact}>)；下载副本：<{uri}>"

    result = discover_delivered_files(
        [_final("item-final", text)],
        turn_id="turn-1",
        final_agent_item_id="item-final",
    )

    assert len(result.ready) == 1
    candidate = result.ready[0]
    assert candidate.path == artifact
    assert candidate.display_name == artifact.name
    assert candidate.size == len(b"verified artifact")
    assert candidate.sha256
    assert candidate.candidate_id.startswith("artifact-")


def test_local_markdown_link_is_delivery_without_keyword(tmp_path: Path) -> None:
    artifact = tmp_path / "report.Keynote"
    artifact.write_bytes(b"presentation")
    result = discover_delivered_files(
        [_final("item-final", f"[报告](<{artifact}>)")],
        turn_id="turn-1",
        final_agent_item_id="item-final",
    )
    assert len(result.ready) == 1
    assert result.ready[0].path == artifact


def test_url_encoded_windows_path_is_supported(tmp_path: Path) -> None:
    artifact = tmp_path / "空格 名称.bin"
    artifact.write_bytes(b"binary")
    result = discover_delivered_files(
        [_final("item-final", f"下载：<{artifact.as_uri()}>")],
        turn_id="turn-1",
        final_agent_item_id="item-final",
    )
    assert len(result.ready) == 1
    assert result.ready[0].path == artifact


def test_protocol_delivery_enum_is_retained_in_bounded_provenance(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "async-output.bin"
    artifact.write_bytes(b"output")
    item = _final("item-final", f"[交付](<{artifact}>)")
    item["item_json"] = make_final_agent_item(
        item_id="item-final", text=f"[交付](<{artifact}>)", delivery="async"
    )
    result = discover_delivered_files(
        [item], turn_id="turn-1", final_agent_item_id="item-final"
    )
    assert result.ready[0].provenance["delivery"] == "async"


def test_source_citation_and_line_reference_never_become_delivery(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("print('ok')", encoding="utf-8")
    result = discover_delivered_files(
        [_final("item-final", f"参考源码：<{source}:42>")],
        turn_id="turn-1",
        final_agent_item_id="item-final",
    )

    assert not result.ready
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.source_kind == "source_citation"
    assert candidate.delivery_requested is False
    assert candidate.reason == "line_reference"


def test_explicit_code_delivery_has_no_extension_whitelist(tmp_path: Path) -> None:
    source = tmp_path / "deploy.script-without-extension"
    source.write_text("echo verified", encoding="utf-8")
    result = discover_delivered_files(
        [_final("item-final", f"交付代码：<{source}>")],
        turn_id="turn-1",
        final_agent_item_id="item-final",
    )
    assert len(result.ready) == 1
    assert result.ready[0].path == source


def test_missing_explicit_delivery_is_preserved_as_typed_candidate(tmp_path: Path) -> None:
    missing = tmp_path / "not-created.zip"
    result = discover_delivered_files(
        [_final("item-final", f"下载文件：<{missing}>")],
        turn_id="turn-1",
        final_agent_item_id="item-final",
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].status == "missing"
    assert result.candidates[0].reason == "missing"
    assert any(error.code == "missing" for error in result.errors)


def test_logs_and_keys_are_allowed_when_explicitly_delivered(tmp_path: Path) -> None:
    log_path = tmp_path / "result.log"
    key_path = tmp_path / "private.key"
    log_path.write_text("diagnostic", encoding="utf-8")
    key_path.write_text("secret", encoding="utf-8")
    result = discover_delivered_files(
        [_final("item-final", f"交付日志：<{log_path}>；交付密钥：<{key_path}>")],
        turn_id="turn-1",
        final_agent_item_id="item-final",
    )

    assert {candidate.path for candidate in result.ready} == {log_path, key_path}


def test_code_blocks_are_ignored_and_settings_saved_has_no_path_error(
    tmp_path: Path,
) -> None:
    code_artifact = tmp_path / "inside-code.txt"
    code_artifact.write_text("not an output", encoding="utf-8")
    fence = chr(96) * 3
    text = (
        "设置已保存，日志已记录。\n"
        + fence
        + "text\n"
        + f"下载文件：<{code_artifact}>\n"
        + fence
        + "\nHTTP参考：https://example.invalid/report.zip"
    )
    result = discover_delivered_files(
        [_final("item-final", text)],
        turn_id="turn-1",
        final_agent_item_id="item-final",
    )
    assert result.candidates == ()
    assert result.errors == ()


def test_saved_settings_with_ordinary_web_reference_is_silent() -> None:
    result = discover_delivered_files(
        [
            _final(
                "item-final",
                "Settings saved. [Reference](https://example.com/documentation)",
            )
        ],
        turn_id="turn-1",
        final_agent_item_id="item-final",
    )
    assert result.candidates == ()
    assert result.errors == ()


def test_delivery_keyword_does_not_upgrade_citation_in_another_paragraph(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("print('ok')", encoding="utf-8")
    output = tmp_path / "output.bin"
    output.write_bytes(b"output")
    result = discover_delivered_files(
        [
            _final(
                "item-final",
                f"参考源码：<{source}:12>\n\n交付文件：<{output}>",
            )
        ],
        turn_id="turn-1",
        final_agent_item_id="item-final",
    )
    assert len(result.ready) == 1
    assert result.ready[0].path == output
    citations = [candidate for candidate in result.candidates if candidate.path == source]
    assert len(citations) == 1
    assert citations[0].delivery_requested is False


def test_only_selected_final_item_can_create_text_delivery(tmp_path: Path) -> None:
    artifact = tmp_path / "commentary.txt"
    artifact.write_text("artifact", encoding="utf-8")
    commentary = {
        "item_id": "item-commentary",
        "item_type": "agentMessage",
        "turn_id": "turn-1",
        "item_json": make_final_agent_item(
            item_id="item-commentary",
            text=f"下载：<{artifact}>",
            phase="commentary",
        ),
    }
    final = _final("item-final", "本轮已完成。")
    result = discover_delivered_files(
        [commentary, final],
        turn_id="turn-1",
        final_agent_item_id="item-final",
    )
    assert result.candidates == ()
    assert result.errors == ()


def test_successful_same_turn_resource_link_is_verified(tmp_path: Path) -> None:
    artifact = tmp_path / "tool-output.data"
    artifact.write_bytes(b"tool output")
    tool = {
        "item_id": "tool-item",
        "item_type": "mcpToolCall",
        "turn_id": "turn-1",
        "item_json": {
            "type": "mcpToolCall",
            "id": "tool-item",
            "server": "synthetic",
            "tool": "artifact_tool",
            "status": "completed",
            "error": None,
            "result": {
                "content": [
                    {
                        "type": "resource_link",
                        "uri": artifact.as_uri(),
                        "name": artifact.name,
                    }
                ],
                "structuredContent": None,
                "_meta": None,
            },
        },
    }
    result = discover_delivered_files(
        [tool, _final("item-final", "完成。")],
        turn_id="turn-1",
        final_agent_item_id="item-final",
    )
    assert len(result.ready) == 1
    assert result.ready[0].source_kind == "tool_resource"
    assert result.ready[0].provenance["source"] == "mcp_resource_link"


def test_failed_or_wrong_turn_resource_link_is_retained_without_ready_delivery(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "failed.data"
    artifact.write_bytes(b"tool output")
    tool = {
        "item_id": "tool-item",
        "item_type": "mcpToolCall",
        "turn_id": "other-turn",
        "item_json": {
            "type": "mcpToolCall",
            "id": "tool-item",
            "status": "failed",
            "error": {"kind": "tool-error"},
            "result": {"content": [{"type": "resource_link", "uri": artifact.as_uri()}]},
        },
    }
    result = discover_delivered_files(
        [tool],
        turn_id="turn-1",
        final_agent_item_id="item-final",
    )
    assert not result.ready
    assert any(error.code == "wrong_turn" for error in result.errors)

    tool["turn_id"] = "turn-1"
    result = discover_delivered_files([tool], turn_id="turn-1")
    assert len(result.candidates) == 1
    assert result.candidates[0].status == "unverified"
    assert result.candidates[0].reason == "tool_failed"
    assert any(error.code == "tool_failed" for error in result.errors)


def test_completed_tool_with_error_result_is_not_ready(tmp_path: Path) -> None:
    artifact = tmp_path / "error-result.data"
    artifact.write_bytes(b"tool output")
    tool = {
        "item_id": "tool-item",
        "item_type": "mcpToolCall",
        "turn_id": "turn-1",
        "item_json": {
            "type": "mcpToolCall",
            "id": "tool-item",
            "status": "completed",
            "error": None,
            "result": {
                "isError": True,
                "content": [{"type": "resource_link", "uri": artifact.as_uri()}],
            },
        },
    }
    result = discover_delivered_files([tool], turn_id="turn-1")
    assert not result.ready
    assert result.candidates[0].reason == "tool_failed"
    assert any(error.code == "tool_failed" for error in result.errors)


def test_mcp_text_mention_is_not_treated_as_resource_link(tmp_path: Path) -> None:
    artifact = tmp_path / "mentioned.data"
    artifact.write_bytes(b"not delivered")
    tool = {
        "item_id": "tool-item",
        "item_type": "mcpToolCall",
        "turn_id": "turn-1",
        "item_json": {
            "type": "mcpToolCall",
            "status": "completed",
            "error": None,
            "result": {
                "content": [
                    {"type": "text", "text": f"resource_link: {artifact}"}
                ]
            },
        },
    }
    result = discover_delivered_files([tool], turn_id="turn-1")
    assert result.candidates == ()
    assert result.errors == ()


def test_confirmed_external_temp_path_is_allowed_and_turn_ids_stabilise_identity(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external" / "temp output.video"
    external.parent.mkdir()
    external.write_bytes(b"video")
    first = discover_delivered_files(
        [],
        turn_id="turn-one",
        confirmed_paths=[{"path": str(external), "item_id": "tool-1"}],
    )
    second = discover_delivered_files(
        [],
        turn_id="turn-two",
        confirmed_paths=[{"path": str(external), "item_id": "tool-1"}],
    )
    assert len(first.ready) == 1
    assert len(second.ready) == 1
    assert first.ready[0].candidate_id != second.ready[0].candidate_id
    assert first.ready[0].path == external


def test_stat_only_inspection_returns_identity_without_content_digest(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "large-output.bin"
    artifact.write_bytes(b"verified bytes")
    result = discover_delivered_files(
        [_final("item-final", f"交付文件：<{artifact}>")],
        turn_id="turn-1",
        final_agent_item_id="item-final",
        inspect_content=False,
    )
    assert len(result.ready) == 1
    candidate = result.ready[0]
    assert candidate.sha256 == ""
    assert candidate.provenance["inspection"] == "stat"
    assert candidate.provenance["identity_size"] == str(artifact.stat().st_size)
    assert candidate.provenance["identity_mtime_ns"]
    assert candidate.provenance["identity_ino"]
    assert "identity_ctime_ns" not in candidate.provenance


def test_user_attachment_is_explicitly_blocked_from_auto_delivery(tmp_path: Path) -> None:
    attachment = tmp_path / "user-upload.bin"
    attachment.write_bytes(b"user data")
    item = {
        "type": "userMessage",
        "id": "user-item",
        "attachments": [{"path": str(attachment)}],
    }
    result = discover_delivered_files([item], turn_id="turn-1")
    assert len(result.candidates) == 1
    assert result.candidates[0].source_kind == "user_attachment"
    assert result.candidates[0].delivery_requested is False
    assert not result.ready
