"""摘要器的本地默认、限频取消和有界缓存测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from progress_wx.config import SummaryConfig
from progress_wx.models import ProgressReport, TurnEvent
from progress_wx.summarizer import (
    SUMMARY_CACHE_MAX_ENTRIES,
    ProgressSummarizer,
    SummaryCancelled,
    SummaryError,
)


def config(mode: str, *, interval: float = 0) -> SummaryConfig:
    return SummaryConfig(
        mode=mode,
        endpoint="https://example.invalid/v1",
        model="local-test",
        api_key_env="TEST_KEY",
        min_interval_seconds=interval,
    )


def test_local_default_never_calls_external_request(monkeypatch) -> None:
    summarizer = ProgressSummarizer(config("codex_final"))
    monkeypatch.setattr(
        summarizer,
        "_request",
        lambda _event: (_ for _ in ()).throw(AssertionError("不得访问外部摘要")),
    )

    report = summarizer.summarize(TurnEvent("thread-1", "turn-1", "completed"))

    assert report.status == "*/*"


def test_external_summary_cache_is_bounded(monkeypatch) -> None:
    summarizer = ProgressSummarizer(config("openai_compatible"))
    monkeypatch.setattr(
        summarizer,
        "_request",
        lambda event: ProgressReport("完成", event.turn_id),
    )

    for index in range(SUMMARY_CACHE_MAX_ENTRIES + 20):
        summarizer.summarize(
            TurnEvent("thread-1", f"turn-{index}", "completed")
        )

    assert len(summarizer._cache) == SUMMARY_CACHE_MAX_ENTRIES
    assert "thread-1:turn-0:completed" not in summarizer._cache
    assert f"thread-1:turn-{SUMMARY_CACHE_MAX_ENTRIES + 19}:completed" in summarizer._cache


def test_short_clear_final_response_is_preserved_instead_of_rewritten(monkeypatch) -> None:
    summarizer = ProgressSummarizer(config("openai_compatible"))
    rewritten = (
        "本轮完成：提出移动端原生分开发送方案。\n"
        "关键结果：图片与文字转交同一会话。\n"
        "剩余事项：尚待确认。"
    )
    monkeypatch.setattr(
        summarizer,
        "_request",
        lambda _event: ProgressReport("路线选择", rewritten),
    )
    original = (
        "手机端可以分开发：先引用机器人消息发送图片，再直接发送文字说明，"
        "机器人会自动把图片和文字合并到同一个 Codex 会话。"
        "如果只有图片就发“.发送”，不想发了就发“.取消”。"
    )

    report = summarizer.summarize(
        TurnEvent("thread-1", "turn-plain", "completed", final_message=original)
    )

    assert report.status == "路线选择"
    assert report.details == original
    assert "本轮完成" not in report.details


def test_rate_limit_wait_can_be_cancelled_before_network(monkeypatch) -> None:
    summarizer = ProgressSummarizer(config("openai_compatible", interval=60))
    summarizer._last_call = 100.0
    monkeypatch.setattr("progress_wx.summarizer.time.monotonic", lambda: 101.0)
    monkeypatch.setattr(
        summarizer,
        "_request",
        lambda _event: (_ for _ in ()).throw(AssertionError("取消后不得发请求")),
    )

    with pytest.raises(SummaryCancelled, match="已取消"):
        summarizer.summarize(
            TurnEvent("thread-1", "turn-2", "completed"),
            wait=lambda _seconds: True,
        )


def test_loopback_responses_request_is_strict_and_not_stored(monkeypatch) -> None:
    local = SummaryConfig(
        mode="openai_compatible",
        endpoint="http://127.0.0.1:11434/v1",
        model="local-model",
        api_key_env="MISSING_LOCAL_KEY",
        min_interval_seconds=0,
    )
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit: int) -> bytes:
            return json.dumps(
                {
                    "status": "completed",
                    "output_text": json.dumps(
                        {"status": "待人工测试", "details": "请运行本地验收。"},
                        ensure_ascii=False,
                    ),
                },
                ensure_ascii=False,
            ).encode("utf-8")

    class Opener:
        def open(self, request, timeout: int):
            captured["url"] = request.full_url
            captured["authorization"] = request.get_header("Authorization")
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return Response()

    monkeypatch.setattr(
        "progress_wx.summarizer.urllib.request.build_opener",
        lambda *_handlers: Opener(),
    )

    report = ProgressSummarizer(local).summarize(
        TurnEvent("thread-1", "turn-1", "completed", final_message="结果")
    )

    assert report.status == "待人工测试"
    assert captured["url"] == "http://127.0.0.1:11434/v1/responses"
    assert captured["authorization"] is None
    assert captured["timeout"] == 60
    payload = captured["payload"]
    assert payload["store"] is False
    assert payload["text"]["format"]["strict"] is True


def test_remote_summary_requires_environment_api_key(monkeypatch) -> None:
    monkeypatch.delenv("TEST_KEY", raising=False)
    summarizer = ProgressSummarizer(config("openai_compatible"))

    with pytest.raises(SummaryError, match="TEST_KEY"):
        summarizer.summarize(TurnEvent("thread-1", "turn-1", "completed"))


def test_custom_status_is_allowed_by_summary_schema() -> None:
    from progress_wx.models import PROGRESS_DETAILS_MAX_CHARS
    from progress_wx.summarizer import _SCHEMA

    status_schema = _SCHEMA["properties"]["status"]
    assert "enum" not in status_schema
    assert status_schema["maxLength"] == 20
    assert _SCHEMA["properties"]["details"]["maxLength"] == PROGRESS_DETAILS_MAX_CHARS
    assert ProgressReport("等待第三方响应", "详细说明").status == "等待第三方响应"


def test_codex_cli_uses_isolated_luna_and_strict_schema(monkeypatch) -> None:
    cli_config = SummaryConfig(
        mode="codex_cli",
        endpoint="",
        model="gpt-5.6-luna",
        api_key_env="OPENAI_API_KEY",
        min_interval_seconds=0,
        codex_command="codex",
        reasoning_effort="low",
        timeout_seconds=45,
        max_input_chars=1000,
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "progress_wx.summarizer.shutil.which",
        lambda _name: "C:/tools/codex.exe",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-child")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-reach-child")

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        output = Path(argv[argv.index("--output-last-message") + 1])
        output.write_text(
            json.dumps(
                {"status": "待人工测试", "details": "- 请运行验收\n- 回复结果"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("progress_wx.summarizer.subprocess.run", fake_run)
    report = ProgressSummarizer(cli_config).summarize(
        TurnEvent(
            "thread-1",
            "turn-1",
            "completed",
            final_message="旧内容" * 1000 + "请进行验收",
        )
    )

    assert report.status == "待人工测试"
    assert report.details == "- 请运行验收\n- 回复结果"
    argv = captured["argv"]
    assert argv[argv.index("--model") + 1] == "gpt-5.6-luna"
    assert 'model_reasoning_effort="low"' in argv
    assert {"--ephemeral", "--ignore-user-config", "--ignore-rules"} <= set(argv)
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    kwargs = captured["kwargs"]
    assert kwargs["timeout"] == 45
    assert "OPENAI_API_KEY" not in kwargs["env"]
    assert "CODEX_API_KEY" not in kwargs["env"]
    assert "旧内容" * 600 not in kwargs["input"]
    assert "请进行验收" in kwargs["input"]
    assert "不把简单的大白话搞复杂" in kwargs["input"]
    assert "优先删减，而不是重新概括" in kwargs["input"]
    assert "不强制使用“本轮完成、关键结果、剩余事项、需要你处理”" in kwargs["input"]
    assert "通常控制在 80～280 个中文字符" in kwargs["input"]


def test_overlong_summary_details_are_rejected() -> None:
    from progress_wx.models import PROGRESS_DETAILS_MAX_CHARS
    from progress_wx.summarizer import _validated_report

    with pytest.raises(SummaryError, match="内容无效"):
        _validated_report(
            {
                "status": "完成",
                "details": "甲" * (PROGRESS_DETAILS_MAX_CHARS + 1),
            }
        )


def test_structured_approval_does_not_consume_codex_cli(monkeypatch) -> None:
    summarizer = ProgressSummarizer(config("codex_cli"))
    monkeypatch.setattr(
        summarizer,
        "_request",
        lambda _event: (_ for _ in ()).throw(AssertionError("审批状态不得调用模型")),
    )

    report = summarizer.summarize(
        TurnEvent("thread-1", "turn-rpc", "waitingOnApproval", final_message="请求批准")
    )

    assert report.status == "待审批"
