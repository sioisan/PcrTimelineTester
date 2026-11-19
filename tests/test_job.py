"""The per-item driver: baseline, two gates, in-memory accumulation."""

from __future__ import annotations

from dataclasses import replace

import pytest

import PcrTimelineTester.job as job_mod
from PcrTimelineTester.client import TuneError
from PcrTimelineTester.job import JobSettings, TuneJob
from PcrTimelineTester.search import SearchSettings

from conftest import FakeClient, hills, make_save

SEARCH = SearchSettings(step=5, max_offset=20, chunk=4)


def build(tmp_path, five_rows, cells, items, *, verify=False, rows=None):
    save = make_save(tmp_path, rows or five_rows)
    client = FakeClient(hills(cells))
    job = TuneJob(
        save, JobSettings(items=frozenset(items), verify=verify), SEARCH,
        log=lambda _: None, client=client,
    )
    return save, job, client


def test_writes_the_best_frame_once_at_the_end(tmp_path, five_rows) -> None:
    save, job, _ = build(tmp_path, five_rows, {(0, 0): (480, 1000.0)}, {(1, 1)})
    summary = job.run()
    assert job.frames[0][0] == 480.0
    assert job.written == 1 and job.skipped == 0 and job.failed == 0
    assert job.accepted[0].old_metric == pytest.approx(900.0)
    assert summary["accepted"][0]["new_raw"] == 480.0
    assert summary["written"] == 1 and summary["cancelled"] is False
    # 存档本身没被动机，写回是另一件事（由 cli 在跑完后做）
    assert save.frames[0][0] == 470.0


def test_gate_one_blocks_a_step_backwards(tmp_path, five_rows, monkeypatch) -> None:
    """An item whose best is below the best already written is refused outright."""
    save, job, _ = build(tmp_path, five_rows, {(0, 0): (480, 1000.0)}, {(1, 1), (1, 2)})
    real = job_mod.search_run
    calls: list[int] = []

    def fake(settings, baseline_frame, baseline_metric, book, **kwargs):
        result = real(settings, baseline_frame, baseline_metric, book, **kwargs)
        calls.append(baseline_frame)
        if len(calls) == 2:  # 第二项：人为压低峰值，模拟"越调越差"
            return replace(result, peak_metric=job.last_best_metric - 1.0)
        return result

    monkeypatch.setattr(job_mod, "search_run", fake)
    job.run()
    assert job.written == 1
    assert job.failed == 1
    assert job.frames[1][1] == 1100.0
    assert job.items[1]["decision"] == "fail"
    assert job.items[1]["decision_text"] == "退步保护"


def test_gate_two_skips_when_the_frame_is_already_best(tmp_path, five_rows) -> None:
    rows = [[480.0, 1000.0], [500.0, 1100.0], [600.0, 1200.0], [700.0, 1300.0], [800.0, 1400.0]]
    save, job, _ = build(tmp_path, five_rows, {(0, 0): (480, 1000.0)}, {(1, 1)}, rows=rows)
    job.run()
    assert job.skipped == 1
    assert job.written == 0
    assert job.frames[0][0] == 480.0
    assert job.items[0]["decision_text"].startswith("跳过")


def test_cancel_writes_nothing(tmp_path, five_rows) -> None:
    save, job, _ = build(tmp_path, five_rows, {(0, 0): (480, 1000.0)}, {(1, 1)})
    job.cancel()
    summary = job.run()
    assert job.cancelled is True
    assert job.written == 0
    assert job.frames == five_rows
    assert summary["cancelled"] is True


def test_baseline_failure_aborts_the_whole_run(tmp_path, five_rows) -> None:
    save = make_save(tmp_path, five_rows)

    class Broken(FakeClient):
        def evaluate(self, candidates):
            raise TuneError("接口请求失败（ConnectError）")

    job = TuneJob(
        save, JobSettings(items=frozenset({(1, 1)})), SEARCH,
        log=lambda _: None, client=Broken(lambda table: 0.0),
    )
    with pytest.raises(TuneError, match="接口请求失败"):
        job.run()


def test_verify_reports_the_final_metric(tmp_path, five_rows) -> None:
    save, job, _ = build(tmp_path, five_rows, {(0, 0): (480, 1000.0)}, {(1, 1)}, verify=True)
    summary = job.run()
    assert job.final is not None
    assert summary["final"]["metric"] == pytest.approx(1000.0)
    assert summary["final"]["metric"] > summary["baseline"]["metric"]


def test_state_round_trip_resumes_from_the_checkpoint(tmp_path, five_rows) -> None:
    save, job, _ = build(tmp_path, five_rows, {(0, 0): (480, 1000.0)}, {(1, 1)})
    job.run()
    state = job.state()
    assert state["accepted"][0]["new_raw"] == 480.0
    assert state["done"] == [[1, "0:0"]]

    resumed_client = FakeClient(hills({(0, 0): (480, 1000.0)}))
    resumed = TuneJob(
        make_save(tmp_path / "二次", five_rows), JobSettings(items=frozenset({(1, 1)})), SEARCH,
        log=lambda _: None, client=resumed_client,
    )
    resumed.apply_state(state)
    assert resumed.frames[0][0] == 480.0
    summary = resumed.run()
    assert summary["written"] == 1
    assert resumed.items[0]["decision"] == "done"
    assert resumed.written == 1  # 沿用状态里的计数，不重复累加


def test_second_pass_restarts_from_the_current_best(tmp_path) -> None:
    """循环模式：每一轮都用上一轮的写回结果当起点再跑一遍。"""
    cells = {(0, 0): (480, 100.0), (1, 0): (620, 100.0)}
    save = make_save(tmp_path, [[470, 1000], [600, 1100], [700, 1200], [800, 1300], [900, 1400]])
    client = FakeClient(hills(cells))
    job = TuneJob(
        save, JobSettings(items=frozenset({(1, 1), (2, 1)}), verify=False, passes=2),
        SearchSettings(step=5, max_offset=30, chunk=8),
        log=lambda _: None, client=client,
    )
    summary = job.run()
    assert summary["passes"] == 2
    assert [entry["pass"] for entry in job.items] == [1, 1, 2, 2]
    assert [entry["decision"] for entry in job.items] == ["write", "write", "skip", "skip"]
    assert summary["written"] == 2 and summary["skipped"] == 2
    assert job.frames[0][0] == 480.0 and job.frames[1][0] == 620.0
    # 第二轮是在第一轮的两个最优之上重跑的，所以第二轮两项都判「相同且未变」
    assert [round(entry["baseline_metric"]) for entry in job.items[2:]] == [200, 200]


def test_passes_round_trips_through_settings(tmp_path) -> None:
    data = JobSettings(passes=3).to_dict()
    assert JobSettings.from_dict(data).passes == 3


def test_job_walks_the_items_in_frame_order(tmp_path) -> None:
    # 第二行的那一刀最早，应该第一个调；行内顺序不再是执行顺序。
    save = make_save(tmp_path, [[900], [100], [500], [700], [800]])
    job = TuneJob(
        save, JobSettings(endpoint="http://127.0.0.1:9/simulate", verify=False),
        SearchSettings(step=10, max_offset=10),
        log=lambda _line: None, client=FakeClient(lambda table: 0.0),
    )
    assert [target.frame for target in job.targets] == [100, 500, 700, 800, 900]
    assert [target.row for target in job.targets][0] == 1


def test_joint_search_crosses_two_losing_single_moves_and_resumes(tmp_path):
    save = make_save(tmp_path, [[500], [505], [820], [], []])
    def damage(table):
        pair = table[0][0], table[1][0]
        return 120 if pair == (501, 506) else 100 if pair == (500, 505) else 90
    client = FakeClient(damage)
    selected = JobSettings(items=frozenset({(1, 1), (2, 1)}))
    job = TuneJob(save, selected, SEARCH, client=client, log=lambda _: None)
    # 在首批联动成绩出来后停止，保留尚未采纳的联合最高分和下一批位置。
    class StopAfterBatch(FakeClient):
        def evaluate(self, candidates):
            scores = super().evaluate(candidates)
            if any(score.metric == 120 for score in scores):
                job.cancel()
            return scores
    job._client = StopAfterBatch(damage)
    assert job.run()["cancelled"]
    state = job.state()
    assert state["effect_progress"]["metric"] == 120
    resumed = TuneJob(save, selected, SEARCH, client=client, log=lambda _: None)
    resumed.apply_state(state)
    result = resumed.run()
    assert result["final"]["metric"] == 120
    assert resumed.frames == [[501], [506], [820], [], []]
    assert sum(a.new_metric - a.old_metric for a in resumed.accepted) == 20


def test_effect_events_and_same_frame_order():
    from PcrTimelineTester.effects import effect_frames, proposals
    from PcrTimelineTester.frames import build_targets
    frames = [[501.001], [501], [], [], []]
    targets = build_targets(frames, list(range(5)))
    events = effect_frames({"HitTraceEvents": [{"SourceUnitId": 0, "Frame": 505}],
                            "BuffDebuffEvents": [{"TargetUnitId": 1, "StartFrame": 400, "EndFrame": 490}],
                            "BossDefEvents": [{"Frame": 510}]}, list(range(5)))
    assert events[:2] == [{505, 510}, {400, 490, 510}]
    a, b = targets
    edits = list(proposals(frames, a, b, events, SEARCH))
    assert all(((a, v),) in edits for v in (504.001, 505.001, 506.001))
    assert ((a, 501), (b, 501.001)) in edits
