from __future__ import annotations

from datetime import datetime, timedelta
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from progress_wx.codex_store import ThreadRecord, ThreadStatus, TurnRecord
from progress_wx.models import ProgressReport, ProgressStatus
from progress_wx.session_search import (
    AtomicProgressFile,
    BEIJING,
    SearchRequest,
    SemanticAssessment,
    SessionSearchCancelled,
    SessionSearchEngine,
    SessionSearchError,
    parse_time_hint,
)
from progress_wx.state import StateStore


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=BEIJING)


class FakeCodexStore:
    def __init__(self, records, turns, *, broken=()):
        self.records = list(records)
        self.turns = dict(turns)
        self.broken = set(broken)
        self._last_errors = ()

    @property
    def last_errors(self):
        return self._last_errors

    def require_readable(self, _operation):
        if self._last_errors:
            raise AssertionError("目录级读取不应在合成样例中失败")

    def select_threads(self, *, include_archived=False):
        assert include_archived is True
        self._last_errors = ()
        return list(self.records)

    def latest_terminal_turn(self, thread_id):
        if thread_id in self.broken:
            self._last_errors = ("synthetic_read_error",)
            return None
        self._last_errors = ()
        return self.turns.get(thread_id)

    def latest_completed_result_turn(self, thread_id):
        return self.latest_terminal_turn(thread_id)

    def get_thread(self, thread_id, *, include_archived=False):
        assert include_archived is True
        self._last_errors = ()
        return next((item for item in self.records if item.thread_id == thread_id), None)


class FakeSummarizer:
    def __init__(self):
        self.calls = 0

    def summarize(self, event):
        self.calls += 1
        return ProgressReport(ProgressStatus.COMPLETED, f"已完成：{event.final_message}")


class FakeJudge:
    def __init__(self, scores=None, *, fail=False):
        self.scores = dict(scores or {})
        self.fail = fail
        self.call_count = 0

    def judge(self, query, candidates):
        self.call_count += 1
        if self.fail:
            raise SessionSearchError("synthetic model failure")
        results = []
        for item in candidates:
            score = float(self.scores.get(item.record.thread_id, 0.1))
            results.append(
                SemanticAssessment(
                    item.record.thread_id,
                    score,
                    "high" if score >= 0.8 else "medium" if score >= 0.55 else "low",
                    "strong_match" if score >= 0.8 else "possible_match" if score >= 0.55 else "unlikely",
                    f"{item.record.title} 的大白话说明",
                    f"概括{item.record.thread_id}",
                    f"合成证据支持 {item.record.thread_id}，线索为 {query[:20]}",
                )
            )
        return results


def _session(codex_home: Path, thread_id: str, title: str, days_ago: int, text: str, *, archived=False):
    sessions = codex_home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    rollout = sessions / f"{thread_id}.jsonl"
    rows = [
        {"type": "event_msg", "payload": {"type": "user_message", "message": text}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": f"完成了{text}"}},
    ]
    rollout.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    completed = int((NOW - timedelta(days=days_ago)).timestamp())
    record = ThreadRecord(
        thread_id=thread_id,
        title=title,
        preview=text,
        archived=archived,
        thread_source="user",
        rollout_path=str(rollout),
    )
    turn = TurnRecord(
        thread_id=thread_id,
        turn_id=f"turn-{thread_id}",
        status=ThreadStatus.COMPLETED,
        completed_at=completed,
        final_message=f"最终结果 {title}",
    )
    return record, turn


@pytest.fixture
def state(tmp_path: Path):
    store = StateStore(tmp_path / "state.sqlite")
    try:
        yield store
    finally:
        store.close()


def _engine(tmp_path, state, specs, scores=None, *, broken=(), judge=None):
    codex_home = tmp_path / "codex-home"
    sessions = [_session(codex_home, *spec) for spec in specs]
    records = [item[0] for item in sessions]
    turns = {item[1].thread_id: item[1] for item in sessions}
    chosen_judge = judge or FakeJudge(scores)
    summarizer = FakeSummarizer()
    engine = SessionSearchEngine(
        state=state,
        codex_store=FakeCodexStore(records, turns, broken=broken),
        codex_home=codex_home,
        summarizer=summarizer,
        judge=chosen_judge,
        now=lambda: NOW,
        summary_sleep=lambda _delay: None,
    )
    return engine, chosen_judge, summarizer


def test_time_parser_supports_beijing_natural_and_soft_exact_time():
    recent = parse_time_hint("这几天", now=NOW)
    assert recent is not None
    assert recent.label == "最近5天（“这几天”）"
    assert recent.start == NOW - timedelta(days=5)
    exact = parse_time_hint("2026-08-20 09:30 左右", now=NOW)
    assert exact is not None
    assert exact.start == datetime(2026, 8, 19, 9, 30, tzinfo=BEIJING)
    assert exact.end == datetime(2026, 8, 21, 9, 30, tzinfo=BEIJING)


def test_request_treats_all_non_empty_fields_as_joint_clues_and_allows_empty():
    request = SearchRequest.from_mapping(
        {"schema_version": 1, "name": "标题线索", "description": "时间也可能填错", "last_activity": "这几天"}
    )
    assert request.semantic_clues == ("标题线索", "时间也可能填错", "这几天")
    assert SearchRequest.from_mapping({"schema_version": 1}).query_text == ""
    with pytest.raises(ValueError, match="未知字段"):
        SearchRequest.from_mapping({"schema_version": 1, "private_text": "no"})


def test_unique_result_uses_last_completed_time_and_progress_summary(tmp_path: Path, state):
    engine, judge, summarizer = _engine(
        tmp_path,
        state,
        [("wanted", "额度查询", 2, "查了现在的剩余额度"), ("other", "无关任务", 1, "整理文档")],
        {"wanted": 0.98, "other": 0.1},
    )
    result = engine.search(SearchRequest(description="查了查我现在的剩余额度", last_activity="这几天"))
    assert result.status == "found"
    assert result.matches[0].thread_id == "wanted"
    assert result.matches[0].last_activity_at_beijing == "2026-08-26 12:00"
    assert result.matches[0].last_result == "已完成：最终结果 额度查询"
    assert judge.call_count == 1
    assert summarizer.calls == 1


def test_ambiguous_result_keeps_all_plausible_candidates_sorted(tmp_path: Path, state):
    engine, _, _ = _engine(
        tmp_path,
        state,
        [("a", "选题检索", 2, "只做了选题没有正文"), ("b", "文章写作", 3, "选题后继续写了正文")],
        {"a": 0.83, "b": 0.79},
    )
    result = engine.search(SearchRequest(description="找了选题但没有继续生成正文"))
    assert result.status == "ambiguous"
    assert [item.thread_id for item in result.matches] == ["a", "b"]


def test_no_match_never_promotes_random_candidate(tmp_path: Path, state):
    engine, _, summarizer = _engine(
        tmp_path, state, [("random", "完全无关", 1, "修复窗体")], {"random": 0.2}
    )
    result = engine.search(SearchRequest(description="雅培医疗贴片速递"))
    assert result.status == "not_found"
    assert result.matches == ()
    assert summarizer.calls == 0


def test_empty_form_lists_three_most_recent_and_names_prompt_titles_in_same_batch(
    tmp_path: Path, state
):
    specs = [(f"t{i}", f"请处理合成任务{i}并给出结果", i, f"内容{i}") for i in range(1, 6)]
    engine, judge, _ = _engine(tmp_path, state, specs)
    engine.codex_store.records = [
        replace(
            record,
            raw={"title": record.title, "name": "", "preview": record.title},
        )
        for record in engine.codex_store.records
    ]
    result = engine.search(SearchRequest())
    assert result.status == "ambiguous"
    assert [item.thread_id for item in result.matches] == ["t1", "t2", "t3"]
    assert [item.title for item in result.matches] == ["概括t1", "概括t2", "概括t3"]
    assert result.model_call_count == 1
    assert result.semantic_candidate_count == 3
    assert judge.call_count == 1

    cached = engine.search(SearchRequest())
    assert [item.title for item in cached.matches] == ["概括t1", "概括t2", "概括t3"]
    assert cached.model_call_count == 0
    assert judge.call_count == 1


def test_expansion_warning_counts_sessions_outside_current_scope(tmp_path: Path, state):
    engine, _, _ = _engine(
        tmp_path,
        state,
        [("recent", "最近", 2, "最近任务"), ("older", "较早", 100, "较早任务")],
        {"recent": 0.1},
    )
    result = engine.search(SearchRequest(description="不存在的线索"))
    assert result.scope == "recent_30d"
    assert result.next_scope == "recent_180d"
    assert "预计检查 2 个会话" in result.cost_warning
    all_result = engine.search(SearchRequest(description="不存在", scope="all"))
    assert all_result.can_expand is False
    assert all_result.next_scope is None
    assert "不能再扩大范围" in all_result.cost_warning


def test_identical_search_reuses_persistent_judgment_and_summary_cache(tmp_path: Path, state):
    engine, judge, summarizer = _engine(
        tmp_path, state, [("cached", "缓存测试", 1, "缓存语义搜索")], {"cached": 0.99}
    )
    first = engine.search(SearchRequest(description="缓存语义搜索"))
    second = engine.search(SearchRequest(description="缓存语义搜索"))
    assert first.status == second.status == "found"
    assert first.model_call_count == 1
    assert second.model_call_count == 0
    assert judge.call_count == 1
    assert summarizer.calls == 1
    assert first.matches[0].title == second.matches[0].title == "缓存测试"


def test_prompt_derived_metadata_uses_same_assessment_generated_display_title(
    tmp_path: Path, state
) -> None:
    codex_home = tmp_path / "codex-home"
    prompt = "请检查合成目录里的自动任务脚本为什么不能生成新目录，并完成修复。"
    record, turn = _session(codex_home, "prompt-title", prompt[:24] + "…", 1, prompt)
    record = replace(
        record,
        preview=prompt,
        raw={"title": prompt, "name": "", "preview": prompt},
        title_source="prompt_fallback",
    )
    judge = FakeJudge({"prompt-title": 0.99})
    engine = SessionSearchEngine(
        state=state,
        codex_store=FakeCodexStore([record], {turn.thread_id: turn}),
        codex_home=codex_home,
        summarizer=FakeSummarizer(),
        judge=judge,
        now=lambda: NOW,
        summary_sleep=lambda _delay: None,
    )

    first = engine.search(SearchRequest(description="修复自动创建目录的脚本"))
    second = engine.search(SearchRequest(description="修复自动创建目录的脚本"))

    assert first.status == second.status == "found"
    assert first.matches[0].title == second.matches[0].title == "概括prompt-title"
    assert first.matches[0].title_origin == "recovered_summary"
    assert first.model_call_count == 1
    assert second.model_call_count == 0
    assert judge.call_count == 1
    assert record.title != first.matches[0].title


def test_repair_missing_titles_is_bounded_persistent_and_reused(
    tmp_path: Path, state: StateStore
) -> None:
    codex_home = tmp_path / "codex-home"
    prompt = "请修复一个历史自动化工具并验证全部流程。" * 8
    records = []
    turns = {}
    for index in range(7):
        record, turn = _session(
            codex_home,
            f"damaged-{index}",
            prompt,
            index,
            prompt,
        )
        record = replace(
            record,
            title_source="prompt_fallback",
            raw={"title": prompt, "name": "", "preview": prompt},
        )
        records.append(record)
        turns[record.thread_id] = turn
    judge = FakeJudge()
    engine = SessionSearchEngine(
        state=state,
        codex_store=FakeCodexStore(records, turns),
        codex_home=codex_home,
        summarizer=FakeSummarizer(),
        judge=judge,
    )

    first = engine.repair_missing_titles(max_model_calls=1)
    assert first["total_anomalies"] == 7
    assert first["recovered"] == 6
    assert first["remaining"] == 1
    assert first["model_call_count"] == 1
    assert judge.call_count == 1

    second = engine.repair_missing_titles(max_model_calls=1)
    assert second["already_recovered"] == 6
    assert second["recovered"] == 1
    assert second["remaining"] == 0
    assert second["model_call_count"] == 1
    assert judge.call_count == 2

    third = engine.repair_missing_titles(max_model_calls=1)
    assert third["already_recovered"] == 7
    assert third["recovered"] == 0
    assert third["model_call_count"] == 0
    assert judge.call_count == 2


def test_independent_current_title_wins_over_generated_display_title(
    tmp_path: Path, state
) -> None:
    codex_home = tmp_path / "codex-home"
    prompt = "请处理合成任务中的脚本问题。"
    record, turn = _session(codex_home, "renamed", "用户重命名后的标题", 1, prompt)
    record = replace(
        record,
        raw={"title": prompt, "name": "用户重命名后的标题", "preview": prompt},
        title_source="manual_name",
    )
    engine = SessionSearchEngine(
        state=state,
        codex_store=FakeCodexStore([record], {turn.thread_id: turn}),
        codex_home=codex_home,
        summarizer=FakeSummarizer(),
        judge=FakeJudge({"renamed": 0.99}),
        now=lambda: NOW,
        summary_sleep=lambda _delay: None,
    )

    result = engine.search(SearchRequest(description="脚本问题"))

    assert result.status == "found"
    assert result.matches[0].title == "用户重命名后的标题"
    assert result.matches[0].title_origin == "codex_manual"


def test_legacy_judgment_without_display_title_is_reassessed_in_normal_batch(
    tmp_path: Path, state
) -> None:
    engine, judge, _ = _engine(
        tmp_path,
        state,
        [("legacy", "缓存迁移", 1, "缓存迁移语义")],
        {"legacy": 0.99},
    )
    request = SearchRequest(description="缓存迁移语义")
    first = engine.search(request)
    assert first.model_call_count == 1
    with state._lock, state._connection:
        state._connection.execute(
            "UPDATE session_search_judgments SET display_title='' WHERE thread_id='legacy'"
        )

    second = engine.search(request)

    assert second.status == "found"
    assert second.model_call_count == 1
    assert judge.call_count == 2
    cached = state.session_search_judgment(
        request.query_hash,
        "legacy",
        next(
            row[0]
            for row in state._connection.execute(
                "SELECT content_hash FROM session_search_judgments WHERE thread_id='legacy'"
            )
        ),
    )
    assert cached is not None
    assert cached["display_title"] == "概括legacy"


def test_model_failure_is_reported_instead_of_returning_candidate(tmp_path: Path, state):
    engine, _, _ = _engine(
        tmp_path,
        state,
        [("candidate", "候选", 1, "某项工作")],
        judge=FakeJudge(fail=True),
    )
    with pytest.raises(SessionSearchError, match="synthetic model failure"):
        engine.search(SearchRequest(description="某项工作"))


def test_all_scope_continues_past_first_twelve_for_abstract_target(tmp_path: Path, state):
    decoys = [
        (f"decoy-{index:02d}", f"相似标题 {index}", index % 5, "选题 正文 检索 流程 但这是无关样例")
        for index in range(1, 14)
    ]
    target = (
        "abstract-target",
        "候选整理",
        4,
        "收集资料后整理成主题候选清单，到此停止，没有制作后续交付物",
    )
    scores = {item[0]: 0.1 for item in decoys}
    scores["abstract-target"] = 0.99
    engine, judge, _ = _engine(tmp_path, state, [*decoys, target], scores)
    result = engine.search(
        SearchRequest(description="找了选题但没有继续生成正文", scope="all")
    )
    assert result.status == "found"
    assert result.matches[0].thread_id == "abstract-target"
    assert result.semantic_candidate_count == 14
    assert judge.call_count == 3


def test_local_narrowing_understands_prior_step_without_downstream_artifact(
    tmp_path: Path, state
):
    specs = [
        (
            f"completed-article-{index:02d}",
            f"候选选题 {index}",
            1,
            "完成选题检索，随后生成并保存了正文文档",
        )
        for index in range(1, 18)
    ]
    specs.append(
        (
            "stopped-after-topics",
            "整理候选主题清单",
            2,
            "收集资料并整理出候选主题，到此停止等待选择",
        )
    )
    scores = {item[0]: 0.1 for item in specs}
    scores["stopped-after-topics"] = 0.99
    engine, _, _ = _engine(tmp_path, state, specs, scores)
    result = engine.search(
        SearchRequest(description="只完成了选题检索，没有继续生成正文文档")
    )
    assert result.status == "found"
    assert result.matches[0].thread_id == "stopped-after-topics"


def test_all_scope_may_stop_only_when_remaining_upper_bound_cannot_catch_up(
    tmp_path: Path, state
):
    specs = [("certain", "明确目标", 1, "完全吻合的特殊线索")]
    specs.extend(
        (f"other-{index:02d}", f"其他 {index}", 2, "完全无关")
        for index in range(1, 14)
    )
    scores = {item[0]: 0.05 for item in specs}
    scores["certain"] = 1.0
    engine, judge, _ = _engine(tmp_path, state, specs, scores)
    result = engine.search(SearchRequest(description="完全吻合的特殊线索", scope="all"))
    assert result.status == "found"
    assert result.matches[0].thread_id == "certain"
    assert result.semantic_candidate_count == 6
    assert judge.call_count == 1


def test_read_errors_are_exposed_as_structured_warnings(tmp_path: Path, state):
    engine, _, _ = _engine(
        tmp_path,
        state,
        [("ok", "正常", 1, "正常任务"), ("broken", "损坏", 1, "损坏任务")],
        {"ok": 0.1},
        broken={"broken"},
    )
    result = engine.search(SearchRequest(description="不存在"))
    warning = next(item for item in result.warnings if item["thread_id"] == "broken")
    assert warning["code"] == "codex_read_error"
    assert warning["details"] == ["synthetic_read_error"]


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length 路径语义")
def test_windows_extended_rollout_path_remains_inside_sessions(tmp_path: Path, state):
    codex_home = tmp_path / "codex-home"
    record, turn = _session(codex_home, "extended", "扩展路径", 1, "路径内证据")
    record = replace(record, rollout_path="\\\\?\\" + record.rollout_path)
    judge = FakeJudge({"extended": 0.99})
    engine = SessionSearchEngine(
        state=state,
        codex_store=FakeCodexStore([record], {turn.thread_id: turn}),
        codex_home=codex_home,
        summarizer=FakeSummarizer(),
        judge=judge,
        now=lambda: NOW,
        summary_sleep=lambda _delay: None,
    )
    result = engine.search(SearchRequest(description="路径内证据"))
    assert result.status == "found"
    assert result.matches[0].thread_id == "extended"
    assert not any(item["code"] == "rollout_outside_sessions" for item in result.warnings)


def test_progress_file_is_atomic_json_and_cancel_file_stops_before_search(tmp_path: Path, state):
    progress_path = tmp_path / "progress.json"
    progress = AtomicProgressFile(progress_path)
    progress.write("reading", 2, 7, "正在读取")
    assert json.loads(progress_path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "phase": "reading",
        "current": 2,
        "total": 7,
        "message": "正在读取",
    }
    cancel = tmp_path / "cancel"
    cancel.touch()
    engine, judge, _ = _engine(tmp_path, state, [("a", "任务", 1, "内容")])
    with pytest.raises(SessionSearchCancelled):
        engine.search(SearchRequest(description="内容"), progress=progress, cancel_file=cancel)
    assert judge.call_count == 0


def test_public_cli_contract_describes_exhaustive_all_scope_and_result_turn_time() -> None:
    contract = (Path(__file__).parents[1] / "docs" / "SESSION_SEARCH_CLI.md").read_text(
        encoding="utf-8"
    )

    assert '"status":"not_found","scope":"all"' in contract
    assert '"examined_count":76,"semantic_candidate_count":76' in contract
    assert "最后一个 `completed` 且确实包含最终答复或生成图片" in contract
    assert "较新的失败、中断、空完成轮次" in contract
    assert "`search_id`：本次调用生成的不透明唯一字符串" in contract
    assert "`classification`：Luna 的语义分类" in contract
    assert "`origin` 在已监测时为 `manual` 或 `auto`" in contract
    assert "不得因出现未知 code 而使整个搜索失败" in contract
    assert "同一轮 Luna 语义评分顺带生成" in contract
    assert "不会写回 Codex 会话元数据" in contract
    assert "序号｜《title》" in contract
    assert "书名号只存在于飞书正文" in contract
