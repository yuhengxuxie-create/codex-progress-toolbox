"""Approved guide resources: exact order, bytes and lossless PNG integrity."""
import hashlib
import struct
import zlib

from progress_wx.usage import USAGE_VERSION, feishu_usage_images, feishu_usage_text


def test_approved_four_page_resources_are_exact_valid_pngs():
    expected = (
        ("01-important-reminder.png", "7c21074005089aade8313fdd515c1d179378e071c5c3bb0f85223c470ff95f14"),
        ("02-start-task-approved-v7.png", "f7f9cf5eaf84b61e7f6d533acec73add277807a832f0b863e2235ec8b0bed0d4"),
        ("03-find-task-and-send-image.png", "ee18cfa8e639a67caa3e2bbf01dceb0c43f359ae92970e269702577502234f2c"),
        ("04-monitor-notifications-and-alerts-v3.png", "f28a1d2d93bd03aaa26adbbad3ed1d7a84c7fa949ed0e2879c711779ba3f2d84"),
    )
    images = feishu_usage_images()
    assert tuple((name, hashlib.sha256(data).hexdigest()) for name, data in images) == expected
    for name, data in images:
        assert data.startswith(b"\x89PNG\r\n\x1a\n"), name
        offset, compressed, dimensions, ended = 8, bytearray(), None, False
        while offset < len(data):
            size = struct.unpack_from(">I", data, offset)[0]
            kind = data[offset+4:offset+8]
            payload = data[offset+8:offset+8+size]
            crc = struct.unpack_from(">I", data, offset+8+size)[0]
            assert zlib.crc32(kind+payload) & 0xffffffff == crc
            if kind == b"IHDR":
                dimensions = struct.unpack(">IIBBBBB", payload)
            elif kind == b"IDAT":
                compressed.extend(payload)
            elif kind == b"IEND":
                ended = True
            offset += size+12
        assert ended and offset == len(data) and dimensions is not None
        width, height, depth, color, compression, filtering, interlace = dimensions
        assert width > 0 and height > 0 and depth == 8 and interlace == 0
        channels = {0: 1, 2: 3, 4: 2, 6: 4}[color]
        decoded = zlib.decompress(compressed)
        assert len(decoded) == height*(1+width*channels)
        assert all(decoded[row*(1+width*channels)] <= 4 for row in range(height))


def test_text_guide_revision_and_current_entry_names():
    assert USAGE_VERSION == "2026-09-08"
    guide = feishu_usage_text()
    assert "四页漫画课堂" in guide and "六页" not in guide
    for label in ("功能中心", "指令使用", "查看指令列表", "重置预警状态", "最近预警"):
        assert label in guide
    assert "收到提醒不代表你的个人额度已重置" in guide
