"""默认复用 Codex 最终答复；可选调用兼容 OpenAI Responses API 的语义分类器。"""

from __future__ import annotations

import json
import hashlib
import inspect
import os
from pathlib import Path
import re
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
    NOTIFICATION_CONTEXT_MAX_CHARS,
    NOTIFICATION_FACT_MAX_CHARS,
    NOTIFICATION_MAX_FACTS,
    NOTIFICATION_REASON_MAX_CHARS,
    NOTIFICATION_REQUEST_MAX_CHARS,
    NotificationReason,
    NotificationContext,
    POLICY_NOTIFICATION_REASONS,
    ProgressReport,
    TurnEvent,
    structural_report,
)


SUMMARY_DETAILS_MAX_CHARS = 280
FALLBACK_DETAILS_MAX_CHARS = 220
POLICY_REASON_VALUES = (
    NotificationReason.SILENT.value,
    NotificationReason.ANSWER_READY.value,
    NotificationReason.REVIEW_READY.value,
    NotificationReason.IMPORTANT_UPDATE.value,
    NotificationReason.USER_ACTION_REQUIRED.value,
    NotificationReason.TASK_COMPLETE.value,
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
            "maxLength": SUMMARY_DETAILS_MAX_CHARS,
        },
        "notification_reason": {
            "type": "string",
            "enum": list(POLICY_REASON_VALUES),
        },
        "reason": {
            "type": "string",
            "minLength": 1,
            "maxLength": NOTIFICATION_REASON_MAX_CHARS,
        },
        "request": {
            "type": "string",
            "maxLength": NOTIFICATION_REQUEST_MAX_CHARS,
        },
        "new_facts": {
            "type": "array",
            "maxItems": NOTIFICATION_MAX_FACTS,
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": NOTIFICATION_FACT_MAX_CHARS,
            },
        },
    },
    "required": [
        "status",
        "details",
        "notification_reason",
        "reason",
        "request",
        "new_facts",
    ],
    "additionalProperties": False,
}

_INSTRUCTIONS = f"""你是给离开电脑的用户阅读的 Codex 远程通知编辑器。请理解完整回复的实际含义，禁止通过关键词、正则或字符串匹配判断任务状态。
同轮正式答复可能有多段，材料按时间先后排列。综合这些答复，后续明确更正优先；内部收尾或“已记录”不能抹掉此前已经给用户的实质答案。不要把内部协作当成用户新请求。
notification_reason 只能是：
- answer_ready：当前用户问题已经得到明确直接答案，且这条答案值得现在告知；
- review_ready：用户要求的方案、提示词、草稿或可审阅成果已经准备好；即使原文请用户“确认”，只要确认的是这份可审阅成果，优先使用 review_ready，不要把审阅确认写成 user_action_required；
- important_update：出现影响用户决定、数据、使用或计划的实际失败、偏差、纠正或新事实；单纯“测试通过”“正在检查”“只完成构建”“安装/回滚尚未验证”或等待下一阶段，不属于 important_update，保持 silent；
- user_action_required：现在明确需要用户操作、选择、批准、提供信息或协助，发一条行动消息；
- task_complete：当前用户委托已经实际交付，且没有剩余必需工作，发一次最终总结；
- silent：除此之外全部静默，包括中间进度、自动续跑、检查点、测试结果、台账更新、继续施工、等待代理内部工作、重复成功通知，以及仅仅一轮 Codex 回复结束。
直接问题要求再次解释时，不按历史重复通知过滤；goal continuation、代理转交和内部协作不算新的用户请求。宁可选择 silent，也不要把不确定的阶段性回复打扰用户。不要因为回复里出现“完成”“通过”“总结”就判为 task_complete；必须结合当前用户请求、可核实任务状态和剩余工作确认。user_action_required 必须能从原回复中指出一个现在需要用户做的具体动作，不能自行创造“请确认”。

status 要直接说明整个任务当前处于什么阶段。只有“停滞、阻塞、路线选择、完成、待人工测试、待审批”能准确表达时才使用；否则拟定不超过 20 个 Unicode 字符的具体状态，例如“问题原因已确认”或“补丁完成，等待安装验证”。不要因为这一轮回复结束就笼统写“完成”。

用户可能完全看不到电脑上的 Codex 对话，只能看到 details。请把回复提炼成手机上一眼能看懂的大白话：
- 先找出做了什么、得到什么结果、还有什么没完成，以及是否明确需要用户操作或决定；没有的内容不要写。
- 原回复较长时必须重新凝练，不能按原顺序摘抄大段正文，也不能把方案、解释、路径、哈希值和长命令整段搬过去。
- 测试是否通过、服务是否重启、变更是否影响配置或数据等重要事实必须保留。
- 本地完整路径、完整哈希值和长命令默认省略；只有执行下一步确实必需时，才保留文件名或最短命令。
- 使用日常说法，禁止写成“本轮完成、关键结果、路线选择、进行交付”一类机器化复述，也不要把简单原话改得更抽象或正式。
- 只有原回复明确要求用户执行或决定时，才在最后另起一行写“需要你处理：”并说明一个具体动作；不要自己创造“待确认”。
- 没有用户操作时不要写“需要你处理”，也不要写“剩余事项：无”之类的废话。
- reason 用 1～2 句说明选择该分类的事实依据；request 只摘取本次实际用户问题或委托的简短要点；new_facts 只列本轮相对最近成功通知新增且可核实的事实，最多 {NOTIFICATION_MAX_FACTS} 条，没有就填空数组。
- 不能把代理协作、内部进度或上一轮已成功通知的事实写进 new_facts；模型调用失败时由程序记录错误，不得返回 silent 伪装成功。

不得编造回复中没有的信息。通常控制在 80～220 个中文字符，简单内容可以更短，复杂内容最多 {SUMMARY_DETAILS_MAX_CHARS} 个 Unicode 字符。"""
_CLI_INSTRUCTIONS = _INSTRUCTIONS + """
只分析下面提供的 JSON，不得调用工具、读取文件、访问网络或检查工作区。
判断的是整个用户任务当前所处阶段，不是“这一轮回答是否结束”：
- 已交付且没有剩余必需工作时为“完成”；
- 明确要求用户实际操作并反馈结果时为“待人工测试”；
- 必须由用户在多个方向中作决定时为“路线选择”；
- 必须得到权限、批准或高影响操作确认时为“待审批”；
- 因外部条件无法继续时为“阻塞”；长期没有有效推进时为“停滞”。
只有内部过程、继续施工、继续验证或等待内部结果，而没有值得告知的直接答案、可审成果或重要变化时，才使用 silent；按上述六类规则判断。只有整项工作确实交付完毕才为 task_complete；只有用户现在必须介入才为 user_action_required。
缺少助手正文属于监测数据异常，不能据此把用户项目判为“阻塞”；正常服务会在调用你之前暂缓这种事件。
details 要让用户不打开电脑也能知道结论和下一步，但不要用项目管理术语重新包装。适合列举时最多使用 3 条简短项目符号。"""
SUMMARY_CACHE_MAX_ENTRIES = 256


def _completed_answer_input(event: TurnEvent, limit: int) -> str:
    """Give each exact final part bounded space, including later corrections."""
    parts=event.final_answer_parts
    if len(parts)<=1:
        return event.final_message[-limit:]
    headers=[f'【正式答复 {i}，按时间先后】\n' for i in range(1,len(parts)+1)]
    available=limit-sum(len(h) for h in headers)-2*(len(parts)-1)
    if available<len(parts):
        raise SummaryError('正式答复材料超出可安全容纳的输入范围')
    budgets = [0] * len(parts)
    pending = set(range(len(parts)))
    while pending and available:
        share = max(1, available // len(pending))
        for index in sorted(pending):
            allocated = min(share, len(parts[index]) - budgets[index], available)
            budgets[index] += allocated
            available -= allocated
            if budgets[index] == len(parts[index]):
                pending.remove(index)
    values = [
        part if len(part) <= budget else part[:max(0, budget - 1)] + '…'
        for part, budget in zip(parts, budgets)
    ]
    return '\n\n'.join(header+value for header,value in zip(headers,values))

_MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_LOCAL_PATH_PATTERN = re.compile(
    r"(?:[A-Za-z]:[\\/]|\\\\)[^\s，。；：、]+",
    re.IGNORECASE,
)
_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_LONG_HASH_PATTERN = re.compile(r"\b[0-9a-f]{24,}\b", re.IGNORECASE)
_SECRET_PATTERN = re.compile(
    r"(?i)(app[_ -]?secret|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|secret|password|密码|密钥|令牌)"
    r"(\s*[:=：]\s*)([^\s,，;；]+)"
)


class SummaryError(RuntimeError):
    """外部语义摘要未返回严格结构化结果。"""


class SummaryCancelled(SummaryError):
    """服务停止时中断尚未开始的限频等待。"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """拒绝重定向，防止 Authorization 被带到配置之外的主机。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _compact_fallback_details(value: object) -> str:
    """模型摘要连续失败时生成有界、可读且不暴露长技术细节的正文。"""

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return "本轮已经结束，但没有可展示的回复内容。"
    text = _MARKDOWN_IMAGE_PATTERN.sub("图片", text)
    text = _MARKDOWN_LINK_PATTERN.sub(r"\1", text)
    text = _URL_PATTERN.sub("链接", text)
    text = _LOCAL_PATH_PATTERN.sub("本地文件", text)
    text = _LONG_HASH_PATTERN.sub("校验值", text)
    text = text.replace("```", "").replace("`", "").replace("**", "")
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = " ".join(raw_line.strip().split())
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^(?:[-*•·]|\d+[.)、])\s+", "", line)
        if line and line not in lines:
            lines.append(line)
    compact = " ".join(lines) or "本轮已经结束，但摘要暂时无法生成。"
    if len(compact) > FALLBACK_DETAILS_MAX_CHARS:
        compact = compact[: FALLBACK_DETAILS_MAX_CHARS - 1].rstrip() + "…"
    return compact


def _redact_model_context(value: object, limit: int) -> str:
    """Bound context before handing it to the local/remote policy model."""

    text = str(value or "").replace("\x00", "").strip()
    text = _MARKDOWN_IMAGE_PATTERN.sub("图片", text)
    text = _URL_PATTERN.sub("链接", text)
    text = _LOCAL_PATH_PATTERN.sub("本地文件", text)
    text = _LONG_HASH_PATTERN.sub("校验值", text)
    text = _SECRET_PATTERN.sub(r"\1\2已隐藏", text)
    text = "\n".join(" ".join(line.split()) for line in text.splitlines())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _model_context(context: NotificationContext) -> dict[str, object]:
    """Return a privacy-bounded JSON-safe policy context."""

    return {
        "user_request": _redact_model_context(
            context.user_request, NOTIFICATION_REQUEST_MAX_CHARS
        ),
        "task_state": _redact_model_context(
            context.task_state, NOTIFICATION_CONTEXT_MAX_CHARS
        ),
        "recent_successful_notifications": [
            _redact_model_context(item, NOTIFICATION_CONTEXT_MAX_CHARS)
            for item in context.recent_successful_notifications
        ],
    }


def fallback_report(event: TurnEvent) -> ProgressReport:
    """摘要服务不可用时保守留队列，并显式记录模型失败。

    ``notification_reason`` remains ``silent`` so a fallback cannot be sent as
    if it were a model decision.  The non-empty ``model_error`` marker is part
    of the persisted judgment and lets the service retry/release instead of
    discarding the event as an intentional silent result.
    """

    structural = structural_report(event)
    source = structural.details if event.status != "completed" else event.final_message
    if event.status != "completed":
        return structural
    return ProgressReport(
        structural.status,
        _compact_fallback_details(source),
        NotificationReason.SILENT,
        decision_reason="模型分类失败，未形成可发送的策略判定；事件必须保留并受控重试。",
        model_error="semantic_summary_unavailable",
    )


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
        context: NotificationContext | None = None,
        wait: Callable[[float], bool] | None = None,
    ) -> ProgressReport:
        """本地默认路径不发出任何网络请求。"""

        policy_context = self._normalise_context(event, context)
        immediate = self.immediate_report(event)
        if immediate is not None:
            return immediate
        cache_key = self._cache_key(event, policy_context)
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached:
                self._cache.move_to_end(cache_key)
                return cached
            remaining = self.config.min_interval_seconds - (time.monotonic() - self._last_call)
            if remaining > 0:
                if wait is None:
                    time.sleep(remaining)
                elif wait(remaining):
                    raise SummaryCancelled("服务停止，已取消尚未开始的摘要调用")
            report = (
                self._request_with_context(event, policy_context)
                if wait is None
                else self._request_with_context(
                    event, policy_context, wait=wait
                )
            )
            self._last_call = time.monotonic()
            self._cache[cache_key] = report
            self._cache.move_to_end(cache_key)
            while len(self._cache) > SUMMARY_CACHE_MAX_ENTRIES:
                self._cache.popitem(last=False)
            return report

    def _request_with_context(
        self,
        event: TurnEvent,
        context: NotificationContext,
        *,
        wait: Callable[[float], bool] | None = None,
    ) -> ProgressReport:
        """Call old test/integration doubles that predate ``context=`` safely."""

        try:
            signature = inspect.signature(self._request)
            parameters = signature.parameters
            accepts_var_kwargs = any(
                item.kind is inspect.Parameter.VAR_KEYWORD
                for item in parameters.values()
            )
            accepts_context = "context" in parameters or accepts_var_kwargs
            accepts_wait = "wait" in parameters or accepts_var_kwargs
        except (TypeError, ValueError):
            accepts_context = True
            accepts_wait = True
        if accepts_context:
            kwargs: dict[str, Any] = {"context": context}
            if wait is not None and accepts_wait:
                kwargs["wait"] = wait
            return self._request(event, **kwargs)
        if wait is None or not accepts_wait:
            return self._request(event)  # type: ignore[call-arg]
        return self._request(event, wait=wait)  # type: ignore[call-arg]

    @staticmethod
    def _normalise_context(
        event: TurnEvent, context: NotificationContext | None
    ) -> NotificationContext:
        if context is None:
            return NotificationContext(task_state=event.status)
        if not isinstance(context, NotificationContext):
            raise TypeError("摘要 context 必须是 NotificationContext")
        if context.task_state:
            return context
        return NotificationContext(
            user_request=context.user_request,
            task_state=event.status,
            recent_successful_notifications=context.recent_successful_notifications,
        )

    @staticmethod
    def _cache_key(event: TurnEvent, context: NotificationContext) -> str:
        if not context.user_request and not context.recent_successful_notifications:
            return event.dedupe_key
        encoded = json.dumps(
            context.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
        return f"{event.dedupe_key}:ctx-{digest}"

    def immediate_report(self, event: TurnEvent) -> ProgressReport | None:
        """返回无需模型即可可靠生成的通知；复杂完成答复返回 ``None``。

        这项判定必须发生在启动 Codex/Luna 子进程之前。过去在模型返回后才检查
        短答，导致本可直接发送的简单回复也白等几十秒。
        """

        if self.config.mode in {"codex_final", "disabled"}:
            return structural_report(event)
        # 结构化终态已有可靠含义，无需额外消耗模型额度。
        if event.status in {
            "failed",
            "interrupted",
            "waitingOnApproval",
            "waitingOnUserInput",
        }:
            return fallback_report(event)
        # ``completed`` 只代表一轮结束。即使正文很短，也必须经过语义判定，
        # 不能再用字数捷径把自动续跑或阶段性回复直接发给用户。
        return None

    def _request(
        self,
        event: TurnEvent,
        *,
        context: NotificationContext,
        wait: Callable[[float], bool] | None = None,
    ) -> ProgressReport:
        if self.config.mode == "codex_cli":
            return self._request_codex_cli(event, context=context, wait=wait)
        return self._request_openai_compatible(event, context=context)

    def _request_codex_cli(
        self,
        event: TurnEvent,
        *,
        context: NotificationContext,
        wait: Callable[[float], bool] | None = None,
    ) -> ProgressReport:
        """通过临时、只读、无用户配置的 Codex CLI 会话做低成本分类。"""

        configured = self.config.codex_command.strip()
        explicit = Path(configured).expanduser()
        command = str(explicit) if explicit.is_file() else shutil.which(configured)
        if not command:
            raise SummaryError("Codex CLI 分类命令不存在")
        context = {
            "codex_turn_status": event.status,
            "thread_title": event.display_title,
            "notification_context": _model_context(context),
            "completed_assistant_response": _completed_answer_input(event,self.config.max_input_chars),
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
            if wait is None:
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
                return_code = completed.returncode
            else:
                process: subprocess.Popen[str] | None = None
                try:
                    process = subprocess.Popen(
                        argv,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=child_env,
                        startupinfo=startupinfo,
                        creationflags=creationflags,
                    )
                    deadline = time.monotonic() + self.config.timeout_seconds
                    first_input: str | None = prompt
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(argv, self.config.timeout_seconds)
                        try:
                            process.communicate(
                                input=first_input,
                                timeout=min(0.25, remaining),
                            )
                            break
                        except subprocess.TimeoutExpired:
                            # Python guarantees communicate() may be retried after a timeout;
                            # input must only be supplied on the first call.
                            first_input = None
                            if wait(0):
                                raise SummaryCancelled(
                                    "服务停止，已取消正在运行的摘要调用"
                                )
                    return_code = int(process.returncode or 0)
                except SummaryCancelled:
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=2)
                    raise
                except (OSError, subprocess.TimeoutExpired) as exc:
                    if process is not None and process.poll() is None:
                        process.kill()
                        process.wait(timeout=2)
                    raise SummaryError(
                        f"Codex CLI 分类失败：{type(exc).__name__}"
                    ) from exc
                finally:
                    if process is not None:
                        for stream in (process.stdin, process.stdout, process.stderr):
                            if stream is not None:
                                stream.close()
            if return_code != 0:
                raise SummaryError(
                    f"Codex CLI 分类失败：退出码 {return_code}"
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

    def _request_openai_compatible(
        self, event: TurnEvent, *, context: NotificationContext
    ) -> ProgressReport:
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
            "notification_context": _model_context(context),
            "completed_assistant_response": _completed_answer_input(event,50_000),
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

    if not isinstance(parsed, Mapping):
        raise SummaryError("结构化摘要字段不匹配")
    required = {"status", "details", "notification_reason"}
    optional = {"reason", "request", "new_facts"}
    if not required <= set(parsed) or not set(parsed) <= required | optional:
        raise SummaryError("结构化摘要字段不匹配")
    status, details = parsed.get("status"), parsed.get("details")
    notification_reason = parsed.get("notification_reason")
    reason = parsed.get("reason", "")
    request = parsed.get("request", "")
    new_facts = parsed.get("new_facts", ())
    if new_facts is None:
        new_facts = ()
    if (
        not isinstance(status, str)
        or not status.strip()
        or len(" ".join(status.split())) > CUSTOM_STATUS_MAX_CHARS
        or not isinstance(details, str)
        or not details.strip()
        or len(details) > SUMMARY_DETAILS_MAX_CHARS
        or notification_reason not in POLICY_REASON_VALUES
        and notification_reason != NotificationReason.CONVERSATION_COMPLETE.value
        or not isinstance(reason, str)
        or len(reason) > NOTIFICATION_REASON_MAX_CHARS
        or not isinstance(request, str)
        or len(request) > NOTIFICATION_REQUEST_MAX_CHARS
        or not isinstance(new_facts, (list, tuple))
        or len(new_facts) > NOTIFICATION_MAX_FACTS
        or any(
            not isinstance(item, str)
            or not item.strip()
            or len(item) > NOTIFICATION_FACT_MAX_CHARS
            for item in new_facts
        )
    ):
        raise SummaryError("结构化摘要内容无效")
    return ProgressReport(
        status,
        details,
        notification_reason,
        decision_reason=reason,
        matched_request=request,
        new_facts=tuple(new_facts),
    )
