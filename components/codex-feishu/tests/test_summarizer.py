"""摘要器的本地默认、限频取消和有界缓存测试。"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from progress_wx.config import SummaryConfig
from progress_wx.models import NotificationReason, ProgressReport, TurnEvent
from progress_wx.summarizer import (
    FALLBACK_DETAILS_MAX_CHARS,
    SUMMARY_DETAILS_MAX_CHARS,
    SUMMARY_CACHE_MAX_ENTRIES,
    ProgressSummarizer,
    SummaryCancelled,
    SummaryError,
    fallback_report,
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


def test_short_clear_final_response_still_requires_notification_decision(monkeypatch) -> None:
    summarizer = ProgressSummarizer(config("openai_compatible"))
    calls = 0

    def decide(_event):
        nonlocal calls
        calls += 1
        return ProgressReport(
            "完成",
            "整项任务已经完成。",
            NotificationReason.CONVERSATION_COMPLETE,
        )

    monkeypatch.setattr(
        summarizer,
        "_request",
        decide,
    )
    original = "先发图片，再发文字，机器人会自动把两条消息合并到同一个 Codex 会话。"

    report = summarizer.summarize(
        TurnEvent("thread-1", "turn-plain", "completed", final_message=original)
    )

    assert calls == 1
    assert report.status == "完成"
    assert report.notification_reason == "conversation_complete"


def test_detailed_final_response_keeps_semantic_summary_instead_of_original(monkeypatch) -> None:
    summarizer = ProgressSummarizer(config("openai_compatible"))
    concise = (
        "心跳通知补丁已完成并通过测试。朋友退出百宝箱后运行安装脚本即可；"
        "补丁会自动备份、自检并恢复服务，不影响原有配置和数据。"
    )
    monkeypatch.setattr(
        summarizer,
        "_request",
        lambda _event: ProgressReport("补丁完成，等待安装验证", concise),
    )
    original = (
        "小补丁已经做好，可以直接发给朋友。\n"
        "下载路径：D:/Software/Tool/share/codex-feishu-hotfix.zip\n"
        "SHA-256：1dbda1bc1a4b47eeea70a66445ae43ca1bedee4095b182654884d3fc066fa37b\n"
        "朋友需要先退出百宝箱，然后解压补丁并运行 apply-hotfix.ps1。"
        "脚本会自动备份、自检并恢复服务，不会改动原有配置和数据。"
    )

    report = summarizer.summarize(
        TurnEvent("thread-1", "turn-detailed", "completed", final_message=original)
    )

    assert report.status == "补丁完成，等待安装验证"
    assert report.details == concise
    assert "D:/Software" not in report.details
    assert "SHA-256" not in report.details


@pytest.mark.parametrize(
    "original",
    [
        "补丁位于 D:/Software/Tool/hotfix.zip，请运行安装脚本。",
        "请运行 `apply-hotfix.ps1`，完成后回复结果。",
        "- 已生成补丁\n- 请安装后测试",
    ],
)
def test_short_but_technical_response_is_still_summarized(
    monkeypatch,
    original: str,
) -> None:
    summarizer = ProgressSummarizer(config("openai_compatible"))
    monkeypatch.setattr(
        summarizer,
        "_request",
        lambda _event: ProgressReport("等待安装验证", "补丁已就绪，请安装后测试。"),
    )

    report = summarizer.summarize(
        TurnEvent("thread-1", "turn-technical", "completed", final_message=original)
    )

    assert report.details == "补丁已就绪，请安装后测试。"


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


def test_running_codex_cli_summary_is_terminated_when_service_stops(
    monkeypatch,
) -> None:
    local = config("codex_cli")
    summarizer = ProgressSummarizer(local)
    observed: dict[str, object] = {}

    class Stream:
        def close(self) -> None:
            observed.setdefault("closed", 0)
            observed["closed"] = int(observed["closed"]) + 1

    class Process:
        returncode = None

        def __init__(self, *_args, **_kwargs) -> None:
            self.stdin = Stream()
            self.stdout = Stream()
            self.stderr = Stream()
            self.terminated = False
            observed["process"] = self

        def communicate(self, *, input=None, timeout=None):
            del input, timeout
            raise subprocess.TimeoutExpired("codex", 0.25)

        def poll(self):
            return None if not self.terminated else 0

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            del timeout
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.terminated = True

    monkeypatch.setattr("progress_wx.summarizer.shutil.which", lambda _name: "codex")
    monkeypatch.setattr("progress_wx.summarizer.subprocess.Popen", Process)

    with pytest.raises(SummaryCancelled, match="正在运行"):
        summarizer.summarize(
            TurnEvent(
                "thread-cancel-running",
                "turn-cancel-running",
                "completed",
                final_message="需要模型处理的复杂技术答复，包含脚本和路径 D:/tmp/run.ps1。",
            ),
            wait=lambda _seconds: True,
        )

    process = observed["process"]
    assert isinstance(process, Process)
    assert process.terminated is True
    assert observed["closed"] == 3


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
                        {
                            "status": "待人工测试",
                            "details": "请运行本地验收。",
                            "notification_reason": "user_action_required",
                        },
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
        TurnEvent(
            "thread-1",
            "turn-1",
            "completed",
            final_message="需要语义摘要的复杂技术结果：`apply-hotfix.ps1` 已生成。",
        )
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
    from progress_wx.summarizer import _SCHEMA

    status_schema = _SCHEMA["properties"]["status"]
    assert "enum" not in status_schema
    assert status_schema["maxLength"] == 20
    assert _SCHEMA["properties"]["details"]["maxLength"] == SUMMARY_DETAILS_MAX_CHARS
    assert _SCHEMA["properties"]["notification_reason"]["enum"] == [
        "silent",
        "answer_ready",
        "review_ready",
        "important_update",
        "user_action_required",
        "task_complete",
    ]
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
                {
                    "status": "待人工测试",
                    "details": "- 请运行验收\n- 回复结果",
                    "notification_reason": "user_action_required",
                },
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
    assert report.notification_reason == "user_action_required"
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
    assert "手机上一眼能看懂的大白话" in kwargs["input"]
    assert "原回复较长时必须重新凝练" in kwargs["input"]
    assert "不要因为这一轮回复结束就笼统写“完成”" in kwargs["input"]
    assert "通常控制在 80～220 个中文字符" in kwargs["input"]


def test_overlong_summary_details_are_rejected() -> None:
    from progress_wx.summarizer import _validated_report

    with pytest.raises(SummaryError, match="内容无效"):
        _validated_report(
            {
                "status": "完成",
                "details": "甲" * (SUMMARY_DETAILS_MAX_CHARS + 1),
                "notification_reason": "conversation_complete",
            }
        )


def test_fallback_report_is_short_and_removes_long_technical_details() -> None:
    report = fallback_report(
        TurnEvent(
            "thread-1",
            "turn-fallback",
            "completed",
            final_message=(
                "补丁已生成。\n"
                "路径：D:/Software/Tool/share/codex-feishu-hotfix.zip\n"
                "链接：https://example.invalid/download\n"
                "SHA-256：1dbda1bc1a4b47eeea70a66445ae43ca1bedee4095b182654884d3fc066fa37b\n"
                + "详细说明" * 100
            ),
        )
    )

    assert report.status == "*/*"
    assert len(report.details) <= FALLBACK_DETAILS_MAX_CHARS
    assert "D:/Software" not in report.details
    assert "https://" not in report.details
    assert "1dbda1bc" not in report.details


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
