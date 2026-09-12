from pathlib import Path
import pytest
from progress_wx.delivered_files import _text_references, discover_delivered_files, make_final_agent_item

@pytest.mark.parametrize("prefix", [":", "::"])
@pytest.mark.parametrize("reverse", [False, True])
def test_output(tmp_path, prefix, reverse):
    p = tmp_path / "report 中文.docx"
    p.write_bytes(b"report")
    attrs = [f'path="{p.as_posix()}"', 'purpose="output"']
    if reverse: attrs.reverse()
    text = prefix + "codex-file-citation{" + " ".join(attrs) + "}"
    refs, explicit = _text_references(text)
    assert explicit and len(refs) == 1 and refs[0].raw == p.as_posix()
    item = {"thread_id":"t", "turn_id":"turn", "item_id":"f", "item_type":"agentMessage", "item_json":make_final_agent_item(item_id="f", text=text)}
    result = discover_delivered_files([item], turn_id="turn", final_agent_item_id="f")
    assert len(result.ready) == 1 and result.ready[0].path == p

@pytest.mark.parametrize("body", [
    'path="D:/private/report.docx" purpose="input"',
    'path="D:/private/report.docx"',
    'path="D:/private/report.docx" purpose="output" purpose="input"',
    'path="D:/private/report.docx" junk purpose="output"',
])
def test_non_output_or_invalid_is_masked(body):
    assert _text_references(':codex-file-citation{' + body + '}')[0] == []

def test_followup_paths_not_artifacts():
    assert _text_references('::follow-up{prompt="下载 D:/private/report.docx"}')[0] == []

def test_other_link_preserved(tmp_path):
    p = tmp_path / "report.docx"
    refs, _ = _text_references('::follow-up{prompt="下载 D:/private/other.docx"}\n[report](<' + str(p) + '>)')
    assert len(refs) >= 1 and all(r.raw == str(p) for r in refs)

def test_fenced_directive_ignored():
    assert _text_references('```\n:codex-file-citation{path="D:/private/report.docx" purpose="output"}\n```')[0] == []

@pytest.mark.parametrize("prefix", [":", "::"])
def test_actual_followup_label_masks_prompt(prefix):
    text = prefix + 'codex-followup[继续处理]{prompt="下载 D:/private/report.docx 并编辑"}'
    assert _text_references(text)[0] == []
