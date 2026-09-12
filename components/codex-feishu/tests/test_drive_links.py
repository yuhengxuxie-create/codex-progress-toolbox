import os
from pathlib import Path
import pytest
from progress_wx.delivered_files import _Reference, _resolve_reference, discover_delivered_files, make_final_agent_item

@pytest.mark.skipif(os.name != "nt", reason="Windows drive syntax")
@pytest.mark.parametrize("encoded", [False, True])
def test_drive_link_exact_file(tmp_path, encoded):
    from urllib.parse import quote
    p = tmp_path / "report 中文？.txt"
    p.write_bytes(b"report")
    target = "/" + p.as_posix()
    if encoded: target = quote(target, safe="/:")
    text = "[report](<" + target + ">)"
    item={"thread_id":"t","turn_id":"turn","item_id":"f","item_type":"agentMessage","item_json":make_final_agent_item(item_id="f",text=text)}
    result=discover_delivered_files([item],turn_id="turn",final_agent_item_id="f")
    assert len(result.ready)==1 and result.ready[0].path==p

@pytest.mark.parametrize("target", ["https://host/C:/report.txt", "sandbox:/C:/report.txt", "remote:/C:/report.txt"])
def test_remote_not_local(target):
    assert _resolve_reference(_Reference(target)).path is None

@pytest.mark.skipif(os.name != "nt", reason="Windows drive syntax")
def test_unc_not_rewritten():
    target="//server/share/report.txt"
    assert _resolve_reference(_Reference(target)).path is None
    native=chr(92)*2+"server"+chr(92)+"share"+chr(92)+"report.txt"
    assert _resolve_reference(_Reference(native)).path==Path(native)

@pytest.mark.skipif(os.name != "nt", reason="Windows drive syntax")
def test_no_fuzzy_filename_matching(tmp_path):
    actual=tmp_path/"report？.txt"
    actual.write_bytes(b"report")
    missing=tmp_path/"report.txt"
    resolved=_resolve_reference(_Reference("/"+missing.as_posix()))
    assert resolved.path==missing and not resolved.path.exists()
