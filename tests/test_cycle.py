"""循环搜索的多步提分、实际刀数、无轴起步与断点语义。"""
from dataclasses import replace
import json

from conftest import make_save
from PcrTimelineTester.cycle import CycleSession, CycleSettings, Trace
from PcrTimelineTester.client import BattleOutcome
from PcrTimelineTester import modes

BASE = [[300], [400], [], [], [], [500]]


class Battles:
    def __init__(self, units):
        self.units = units
        self.battles = 0
        self.cut = None

    def openings(self, boss):
        return [("XXXXX", ((300,), (400,), (), (), ()))]

    def evaluate(self, axes):
        results = []
        for axis in axes:
            self.battles += 1
            shifted = tuple(axis[0]) == (301,)
            extra = shifted and 480 in axis[1]
            score = 140 if extra else 85 if shifted else 100 if tuple(axis[0]) == (300,) else 10
            results.append(BattleOutcome(metric=score, damage=score, battle_frames=5399, result=2,
                                         seed=666, ub_frames=tuple(map(tuple, axis[:5]))))
        if self.cut:
            self.cut()
        return results

    def trace(self, axis, units, duration):
        self.battles += 1
        events = []
        if tuple(axis[0]) == (301,):
            events.append(dict(Frame=480, SourceUnitId=units[0], TargetUnitId=units[1],
                               TargetLaneIndex=1, CrossedReady=True, EnergyBefore=850, EnergyAfter=1000,
                               EnergyCap=1000, IsIncrease=True, OverflowDelta=0))
        return Trace(dict(UBTime=axis[:5], TpChangeEvents=events,
                          HitTraceEvents=[dict(Frame=300, SourceUnitId=units[0], Damage=10)]), units)

    def verify_export(self, exported, settings):
        return self.evaluate([exported.frames])[0]

    def close(self):
        pass


def test_losing_first_step_then_extra_ub_wins_and_reloads(tmp_path):
    save = make_save(tmp_path, BASE)
    job = CycleSession(save, CycleSettings(depth=2,width=4), tmp_path/'run',
                       client=Battles(save.unit_ids), log=lambda _:None)
    assert job.run() == 0
    assert job.summary["baseline"] == 100 and job.summary["final"] == 140
    assert job.summary["ub_after"] == (1,2,0,0,0)
    assert list(modes.get("cycle").read_save(job.workbook).frames[-1]) == [500]


def test_cancel_resumes_complete_frontier(tmp_path):
    save = make_save(tmp_path, BASE)
    folder = tmp_path/'run'
    client = Battles(save.unit_ids)
    settings = CycleSettings(depth=3)
    def log(line):
        if line.startswith("第 1 层测试"):
            run.cancel()
    run = CycleSession(save,settings,folder,client=client,log=log)
    assert run.run() == 130
    assert not list(folder.glob("*.xlsx"))
    saved = json.loads((folder/'state.json').read_text('utf8'))
    resumed = CycleSession(save,settings,folder,client=Battles(save.unit_ids),resume_state=saved,log=lambda _:None)
    assert resumed.run() == 0
    assert resumed.summary["final"] == 140


def test_empty_team_workbook_can_seed_from_actual_auto_frames(tmp_path):
    from conftest import timeline_raw, write_save
    raw = timeline_raw([[] for _ in range(5)])
    save = modes.get("cycle").read_save(write_save(tmp_path/"无轴.xlsx",raw))
    job = CycleSession(save,CycleSettings(depth=1),tmp_path/"起轴",client=Battles(save.unit_ids),log=lambda _:None)
    assert job.run() == 0
    assert job.job.baseline.path == ("XXXXX 开场转为手动帧轴",)


def test_mismatched_replay_never_publishes_result(tmp_path):
    import pytest
    from PcrTimelineTester.client import TuneError
    save = make_save(tmp_path,BASE)
    client=Battles(save.unit_ids)
    def mismatch(line):
        if line.startswith("第 1 层测试"):
            evaluate=client.evaluate
            client.evaluate=lambda axes:[replace(out,metric=0) for out in evaluate(axes)]
    run = CycleSession(save,CycleSettings(depth=1),tmp_path/"run",client=client,log=mismatch)
    with pytest.raises(TuneError,match="复核"):
        run.run()
    assert not list(run.run_dir.glob("*.xlsx"))



def test_subsequent_replay_can_add_another_ub(tmp_path):
    class Repeat(Battles):
        def trace(self, axis, units, duration):
            t = super().trace(axis, units, duration)
            if 480 in axis[1]:
                t.ready = (t.ready[0], (480, 650), *t.ready[2:])
            return t

        def evaluate(self, axes):
            scores = super().evaluate(axes)
            return [replace(score, metric=160, damage=160) if 650 in axis[1] and score.metric == 140 else score
                    for axis, score in zip(axes, scores)]

    save = make_save(tmp_path, BASE)
    session = CycleSession(save, CycleSettings(depth=3), tmp_path/"连续补刀",
                           client=Repeat(save.unit_ids), log=lambda _: None)
    assert session.run() == 0
    assert session.summary["ub_after"] == (1, 3, 0, 0, 0)
    assert session.summary["final"] == 160


def test_removing_a_ub_can_be_the_best_score(tmp_path):
    class Skip(Battles):
        def evaluate(self, axes):
            scores = super().evaluate(axes)
            return [replace(score, metric=180, damage=180) if not axis[0] else score
                    for axis, score in zip(axes, scores)]
    save = make_save(tmp_path, BASE)
    session = CycleSession(save,CycleSettings(depth=1),tmp_path/"少开",
                           client=Skip(save.unit_ids),log=lambda _:None)
    assert session.run() == 0
    assert session.summary["ub_after"] == (0, 1, 0, 0, 0)
    assert session.summary["final"] == 180
