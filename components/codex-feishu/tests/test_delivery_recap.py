import pytest

from progress_wx.delivered_files import discover_delivered_files
from test_delivered_files import _final


@pytest.mark.parametrize('text',[
    '四个附件已经实际发送成功，旧失败记录仍然保留。',
    '附件已送达，之前的失败已修复。',
    'Files were delivered successfully. Previous failed records remain.',
    '附件投递功能已修复，现有记录保持。',
    '这次没有生成附件，也没有交付文件。',
])
def test_delivery_success_recap_or_no_delivery_is_not_a_missing_attachment(text):
    result=discover_delivered_files([_final('recap',text)],turn_id='turn-1')
    assert not result.candidates and not result.errors


@pytest.mark.parametrize('text',[
    '文件已生成，请下载附件。',
    '已导出成果文件，下载链接如下：',
    '附件如下，请查收。',
    'I generated the file. Download the attachment below.',
    '旧附件已经发送成功。本轮已生成新文件，请下载。',
])
def test_actual_new_delivery_without_reference_still_reports_missing_path(text):
    result=discover_delivered_files([_final('new',text)],turn_id='turn-1')
    assert [e.code for e in result.errors]==['delivery_without_path']


def test_recap_does_not_hide_a_new_real_file_reference(tmp_path):
    path=tmp_path/'new.bin';path.write_bytes(b'new output')
    result=discover_delivered_files([_final('new',f'旧附件已经发送成功。本轮文件：[下载](<{path}>)')],turn_id='turn-1')
    assert len(result.ready)==1 and result.ready[0].path==path
