import pytest
from progress_wx.delivered_files import discover_delivered_files,make_final_agent_item

def discover(text):
 return discover_delivered_files([{'turn_id':'turn','item_id':'final','item_type':'agentMessage','item_json':make_final_agent_item(item_id='final',text=text)}],turn_id='turn',final_agent_item_id='final')

@pytest.mark.parametrize('url',[
 'https://techcrunch.com/2026/08/25/fitbit-founders-launch-luffu-link-an-lte-health-and-safety-band/',
 'https://genhealth.ai/download/piedmont-genhealth-case-study',
 'https://luffu.com/products/luffu-link',
 'https://file.example/result?send=1&link=download&output=file',
])
@pytest.mark.parametrize('form',['[来源]({})','参考：<{}>','参考：`{}`','参考：{}'])
def test_uri_words_do_not_request_file_delivery(url,form):
 r=discover(form.format(url))
 assert not any(c.delivery_requested for c in r.candidates)

@pytest.mark.parametrize('text',[
 '请下载附件：[报告](https://example.com/download/report)',
 '[Download report](https://example.com/report)',
 '交付文件：<https://example.com/export>',
])
def test_explicit_remote_file_still_has_typed_failure(text):
 r=discover(text);assert any(c.delivery_requested and c.reason=='unverified_remote_reference' for c in r.candidates)

def test_local_delivery_and_web_citation_remain_separate(tmp_path):
 p=tmp_path/'report.docx';p.write_bytes(b'local')
 r=discover(f'[成稿](<{p}>)\n参考：[来源](https://example.com/download/source?file=1)')
 assert len(r.ready)==1
 assert all(not c.delivery_requested for c in r.candidates if c.uri.startswith('https:'))

@pytest.mark.parametrize('label',['参考链接','来源','参考资料','reference','source'])
def test_reference_word_is_not_a_delivery_request(label):
 assert not any(c.delivery_requested for c in discover(f'{label}：[来源](https://example.com/news)').candidates)

def test_same_line_local_delivery_does_not_taint_source(tmp_path):
 p=tmp_path/'report.docx';p.write_bytes(b'local')
 r=discover(f'已生成[成稿](<{p}>)；参考[新闻](https://example.com/news)')
 assert len(r.ready)==1
 assert all(not c.delivery_requested for c in r.candidates if c.uri.startswith('https:'))
