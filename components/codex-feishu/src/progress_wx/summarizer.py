"""默认复用 Codex 最终答复；可选调用兼容 OpenAI Responses API 的语义分类器。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from urllib.parse import urlsplit
from typing import Any, Callable, Mapping

from .config import SummaryConfig
from .models import (
    CUSTOM_STATUS_MAX_CHARS,
    PROGRESS_DETAILS_MAX_CHARS,
    ProgressReport,
    TurnEvent,
    structural_report,
)


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "minLength": 1,
            "maxLength": CUSTOM_STATUS_MAX_CHARS,
        },
        "details": {
            "type": "string",
            "minLength": 1,
            "maxLength": PROGRESS_DETAILS_MAX_CHARS,
        },
    },
    "required": ["status", "details"],
    "additionalProperties": False,
}

_INSTRUCTIONS = f"""你是给离开电脑的用户阅读的 Codex 远程进度摘要编辑器。请理解完整回复的实际含义，禁止通过关键词、正则或字符串匹配判断。
status 优先选择：停滞、阻塞、路线选择、完成、待人工测试、待审批。若这些词均不能准确描述，可拟定不超过 20 个 Unicode 字符的简短状态，或使用 */*。

用户可能完全看不到电脑上的 Codex 对话，只能看到 details。details 的首要原则是“不把简单的大白话搞复杂”：
- 原回复已经简短清楚时，优先保留原句和原有语气，只删除寒暄或重复，不得换成更抽象、更正式的说法。
- 例如“先发图片，再发文字，机器人会合并”必须保持这种直白表达，不能改写成“提出原生分开发送并转交同一会话的方案”。
- 原回复较长时，按原有顺序摘取或轻度删减最有用的 2～5 句；优先删减，而不是重新概括。
- 不强制使用“本轮完成、关键结果、剩余事项、需要你处理”或其他汇报框架；没有必要的栏目就不写。
- 只有原回复明确要求用户执行或决定时，才保留那个具体动作；不要自己创造“待确认”。

不得编造回复中没有的信息。保留具体名称、数字、测试结果和必要的决定理由，删除流水账。通常控制在 80～280 个中文字符，简单内容可以更短，但不得超过 {PROGRESS_DETAILS_MAX_CHARS} 个 Unicode 字符。"""
_CLI_INSTRUCTIONS = _INSTRUCTIONS + """
只分析下面提供的 JSON，不得调用工具、读取文件、访问网络或检查工作区。
判断的是整个用户任务当前所处阶段，不是“这一轮回答是否结束”：
- 已交付且没有剩余必需工作时为“完成”；
- 明确要求用户实际操作并反馈结果时为“待人工测试”；
- 必须由用户在多个方向中作决定时为“路线选择”；
- 必须得到权限、批准或高影响操作确认时为“待审批”；
- 因外部条件无法继续时为“阻塞”；长期没有有效推进时为“停滞”。
缺少助手正文属于监测数据异常，不能据此把用户项目判为“阻塞”；正常服务会在调用你之前暂缓这种事件。
details 要保留整段回复里用户真正需要知道的事，但不要用项目管理术语重新包装。适合列举时使用逐行的“- ”项目符号。"""
SUMMARY_CACHE_MAX_ENTRIES = 256
DIRECT_FINAL_MIN_CHARS = 40
DIRECT_FINAL_MAX_CHARS = 520


class SummaryError(RuntimeError):
    """外部语义摘要未返回严格结构化结果。"""


class SummaryCancelled(SummaryError):
    """服务停止时中断尚未开始的限频等待。"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """拒绝重定向，防止 Authorization 被带到配置之外的主机。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _direct_final_details(value: object) -> str | None:
    """原回复本身已适合手机阅读时直接保留，避免二次改写。"""

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if (
        not DIRECT_FINAL_MIN_CHARS <= len(text) <= DIRECT_FINAL_MAX_CHARS
        or "```" in text
    ):
        return None
    return text


def _output_text(data: Mapping[str, Any]) -> str:
    direct = data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    output = data.get("output")
    if not isinstance(output, list):
        raise SummaryError("Responses 结果缺少 output")
    fragments: list[str] = []
    for item in output:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if isinstance(part, Mapping) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                fragments.append(part["text"])
    if not fragments:
        raise SummaryError("Responses 结果缺少 output_text")
    return "".join(fragments)


class ProgressSummarizer:
    """线程安全、带最小调用间隔的摘要器。"""

    def __init__(self, config: SummaryConfig):
        self.config = config
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._cache: OrderedDict[str, ProgressReport] = OrderedDict()

    def summarize(
        self,
        event: TurnEvent,
        *,
        wait: Callable[[float], bool] | None = None,
    ) -> ProgressReport:
        """本地默认路径不发出任何网络请求。"""

        if self.config.mode in {"codex_final", "disabled"}:
            return structural_report(event)
        # 这些状态已有可靠结构化来源，无需额外消耗模型额度。
        if event.status in {"failed", "interrupted", "waitingOnApproval"}:
            return structural_report(event)
        with self._lock:
            cached = self._cache.get(event.dedupe_key)
            if cached:
                self._cache.move_to_end(event.dedupe_key)
                return cached
            remaining = self.config.min_interval_seconds - (time.monotonic() - self._last_call)
            if remaining > 0:
                if wait is None:
                    time.sleep(remaining)
                elif wait(remaining):
                    raise SummaryCancelled("服务停止，已取消尚未开始的摘要调用")
            report = self._request(event)
            direct = _direct_final_details(event.final_message)
            if direct is not None:
                report = ProgressReport(report.status, direct)
            self._last_call = time.monotonic()
            self._cache[event.dedupe_key] = report
            self._cache.move_to_end(event.dedupe_key)
            while len(self._cache) > SUMMARY_CACHE_MAX_ENTRIES:
                self._cache.popitem(last=False)
            return report

    def _request(self, event: TurnEvent) -> ProgressReport:
        if self.config.mode == "codex_cli":
            return self._request_codex_cli(event)
        return self._request_openai_compatible(event)

    def _request_codex_cli(self, event: TurnEvent) -> ProgressReport:
        """通过临时、只读、无用户配置的 Codex CLI 会话做低成本分类。"""

        configured = self.config.codex_command.strip()
        explicit = Path(configured).expanduser()
        command = str(explicit) if explicit.is_file() else shutil.which(configured)
        if not command:
            raise SummaryError("Codex CLI 分类命令不存在")
        context = {
            "codex_turn_status": event.status,
            "thread_title": event.display_title,
            "completed_assistant_response": event.final_message[
                -self.config.max_input_chars :
            ],
            "structured_error": event.error_message[-2000:],
        }
        prompt = _CLI_INSTRUCTIONS + "\n\n输入 JSON：\n" + json.dumps(
            context,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        child_env = os.environ.copy()
        # 强制复用 ChatGPT/Codex 登录额度，避免意外切换成 API Key 计费。
        child_env.pop("OPENAI_API_KEY", None)
        child_env.pop("CODEX_API_KEY", None)
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with tempfile.TemporaryDirectory(prefix="progress-wx-summary-") as directory:
            root = Path(directory)
            schema_path = root / "schema.json"
            output_path = root / "result.json"
            schema_path.write_text(
                json.dumps(_SCHEMA, ensure_ascii=False),
                encoding="utf-8",
            )
            argv = [
                command,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--model",
                self.config.model,
                "--config",
                f'model_reasoning_effort="{self.config.reasoning_effort}"',
                "--color",
                "never",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "--cd",
                str(root),
                "-",
            ]
            try:
                completed = subprocess.run(
                    argv,
                    input=prompt,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdin=None,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.config.timeout_seconds,
                    check=False,
                    env=child_env,
                    startupinfo=startupinfo,
                    creationflags=creationflags,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise SummaryError(
                    f"Codex CLI 分类失败：{type(exc).__name__}"
                ) from exc
            if completed.returncode != 0:
                raise SummaryError(
                    f"Codex CLI 分类失败：退出码 {completed.returncode}"
                )
            try:
                raw = output_path.read_bytes()
            except OSError as exc:
                raise SummaryError("Codex CLI 未生成分类结果") from exc
            if len(raw) > 65536:
                raise SummaryError("Codex CLI 分类结果超过 64 KiB 上限")
            try:
                parsed = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SummaryError("Codex CLI 分类结果不是 JSON") from exc
        return _validated_report(parsed)

    def _request_openai_compatible(self, event: TurnEvent) -> ProgressReport:
        api_key = os.environ.get(self.config.api_key_env, "")
        endpoint_parts = urlsplit(self.config.endpoint)
        loopback = (endpoint_parts.hostname or "").casefold() in {"localhost", "127.0.0.1", "::1"}
        if not api_key and not loopback:
            raise SummaryError(f"环境变量 {self.config.api_key_env} 未配置")
        endpoint = self.config.endpoint.rstrip("/")
        url = endpoint if endpoint.endswith("/responses") else endpoint + "/responses"
        context = {
            "codex_turn_status": event.status,
            "thread_title": event.display_title,
            "completed_assistant_response": event.final_message[-50_000:],
            "structured_error": event.error_message[-4_000:],
        }
        payload = {
            "model": self.config.model,
            "input": [
                {"role": "developer", "content": _INSTRUCTIONS},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "codex_progress_report",
                    "strict": True,
                    "schema": _SCHEMA,
                }
            },
            "max_output_tokens": 800,
            "store": False,
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            handlers: list[Any] = [_NoRedirect]
            if loopback:
                # 本地模型端点不得意外绕到系统代理。
                handlers.insert(0, urllib.request.ProxyHandler({}))
            opener = urllib.request.build_opener(*handlers)
            with opener.open(request, timeout=60) as response:
                raw = response.read(1_048_577)
                if len(raw) > 1_048_576:
                    raise SummaryError("Responses 响应超过 1 MiB 上限")
                data = json.loads(raw.decode("utf-8"))
        except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            raise SummaryError(f"语义摘要请求失败：{type(exc).__name__}") from exc
        if not isinstance(data, Mapping) or data.get("status") not in {None, "completed"}:
            raise SummaryError("Responses API 未正常完成")
        try:
            parsed = json.loads(_output_text(data))
        except json.JSONDecodeError as exc:
            raise SummaryError("结构化摘要不是 JSON") from exc
        return _validated_report(parsed)


def _validated_report(parsed: object) -> ProgressReport:
    """统一校验 CLI 与 Responses API 的严格结构化输出。"""

    if not isinstance(parsed, Mapping) or set(parsed) != {"status", "details"}:
        raise SummaryError("结构化摘要字段不匹配")
    status, details = parsed.get("status"), parsed.get("details")
    if (
        not isinstance(status, str)
        or not status.strip()
        or len(" ".join(status.split())) > CUSTOM_STATUS_MAX_CHARS
        or not isinstance(details, str)
        or not details.strip()
        or len(details) > PROGRESS_DETAILS_MAX_CHARS
    ):
        raise SummaryError("结构化摘要内容无效")
    return ProgressReport(status, details)
