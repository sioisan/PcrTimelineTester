"""The shared session layer: one run must leave exactly the same files, whoever drives it."""

from __future__ import annotations

import json

from openpyxl import load_workbook

from PcrTimelineTester.job import JobSettings
from PcrTimelineTester.report import CONFIG, REPORT_JSON, REPORT_MD, RESULT_WORKBOOK, STATE
from PcrTimelineTester.savefile import read_save
from PcrTimelineTester.search import SearchSettings
from PcrTimelineTester.session import CANCELLED, OK, TuneSession, workbook_summary

from conftest import FakeClient, hills, make_save


def _session(tmp_path, frames, cells, *, search=None, client=None, logs=None):
    """One session over the first cell only.

    The session-level tests deliberately pin ``items={(1, 1)}``: a flat metric
    makes the search drift every other cell to the earliest frame (stage ⑧ picks
    the smallest frame on ties), which is the plugin's behaviour but noisy here.
    """
    save = make_save(tmp_path, frames)
    collected: list[str] = logs if logs is not None else []
    session = TuneSession(
        save,
        JobSettings(battle_time=30, seed=666, items=frozenset({(1, 1)})),
        search or SearchSettings(step=5, max_offset=30, outer_probes=3, max_battles=200),
        tmp_path / "run",
        log=collected.append,
        client=client or FakeClient(hills(cells)),
    )
    return save, session, collected


def test_session_writes_the_whole_run_folder(tmp_path):
    frames = [[470, 1000], [500, 1100], [600, 1200], [700, 1300], [800, 1400]]
    save, session, logs = _session(tmp_path, frames, {(0, 0): (480, 1000.0)})
    assert session.run() == OK
    session.close()

    run_dir = session.run_dir
    assert (run_dir / CONFIG).is_file()
    assert (run_dir / STATE).is_file()
    assert (run_dir / REPORT_JSON).is_file()
    assert (run_dir / REPORT_MD).is_file()
    assert session.workbook == run_dir / RESULT_WORKBOOK
    assert session.workbook.is_file()

    summary = json.loads((run_dir / REPORT_JSON).read_text(encoding="utf-8"))
    assert summary["written"] == 1
    assert summary["final"]["damage"] == 1000
    assert summary["result_workbook"] == str(session.workbook)
    assert any("结果轴" in line for line in logs)

    tuned = read_save(session.workbook)
    assert tuned.frames[0][0] == 480
    assert tuned.raw["totalDamage"] == 1000
    workbook = load_workbook(session.workbook)
    try:
        assert workbook["savePage"].sheet_state == "hidden"
        assert workbook["调帧结果"]["A1"].value == "PCR 调帧结果"
    finally:
        workbook.close()


def test_cancelled_session_writes_reports_but_no_workbook(tmp_path):
    frames = [[470, 1000], [500, 1100], [600, 1200], [700, 1300], [800, 1400]]
    save, session, _logs = _session(tmp_path, frames, {(0, 0): (480, 1000.0)})
    session.cancel()
    assert session.run() == CANCELLED
    session.close()

    run_dir = session.run_dir
    assert (run_dir / REPORT_JSON).is_file()
    assert (run_dir / REPORT_MD).is_file()
    assert not (run_dir / RESULT_WORKBOOK).exists()
    assert session.workbook is None
    summary = json.loads((run_dir / REPORT_JSON).read_text(encoding="utf-8"))
    assert summary["cancelled"] is True
    assert summary["written"] == 0
    # the axis on disk is untouched
    assert read_save(save.path).frames == frames


def test_session_reports_the_cancelled_flag_and_resume_state(tmp_path):
    frames = [[470, 1000], [500, 1100], [600, 1200], [700, 1300], [800, 1400]]
    cells = {(0, 0): (480, 1000.0)}
    save, session, _logs = _session(tmp_path, frames, cells)
    assert session.run() == OK
    session.close()
    stored = json.loads((session.run_dir / STATE).read_text(encoding="utf-8"))
    assert stored["written"] == 1
    assert stored["done"] == [[1, "0:0"]]

    # resuming the same folder keeps the previously accepted frame and writes again
    client = FakeClient(hills(cells))
    resumed = TuneSession(
        save, JobSettings(battle_time=30, seed=666, items=frozenset({(1, 1)})),
        SearchSettings(step=5, max_offset=30, outer_probes=3, max_battles=200),
        session.run_dir, log=lambda _message: None, resume_state=stored, client=client,
    )
    assert resumed.run() == OK
    resumed.close()
    summary = json.loads((resumed.run_dir / REPORT_JSON).read_text(encoding="utf-8"))
    assert summary["written"] == 1
    assert read_save(resumed.workbook).frames[0][0] == 480


def test_workbook_summary_rows_are_plain_values(tmp_path):
    frames = [[470, 1000], [500, 1100], [600, 1200], [700, 1300], [800, 1400]]
    save, session, _logs = _session(tmp_path, frames, {(0, 0): (480, 1000.0)})
    assert session.run() == OK
    rows = workbook_summary(session.summary, session.job)
    session.close()
    assert rows[0][0] == "PCR 调帧结果"
    assert [row[0] for row in rows[:8]] == [
        "PCR 调帧结果", "接口", "搜索参数", "循环轮数", "基线期望伤害", "调帧后期望伤害",
        "实战伤害（基线 → 最终）", "写回 / 跳过 / 失败",
    ]
    assert rows[3][1] == 1
    header = rows[9]
    assert header == ["项", "轮", "原帧 → 新帧", "期望伤害（基线 → 本项最优）"]
    first = rows[10]
    assert first[0] == "角色1 UB1"
    assert first[1] == 1
    assert first[2] == "470 → 480"
