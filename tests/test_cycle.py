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
    assert not (run.run_dir / "循环搜索结果.xlsx").exists()
    candidate = modes.get("cycle").read_save(run.run_dir / "复核失败-最后候选.xlsx")
    assert tuple(map(tuple, candidate.frames)) == run.job.best.axis
    assert json.loads((run.run_dir / "state.json").read_text("utf8"))["best"]
    assert run.summary["status"] == "verification_failed"



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


def test_polish_can_gain_when_two_independent_one_frame_edits_are_flat(tmp_path):
    class Linked(Battles):
        def evaluate(self, axes):
            results = super().evaluate(axes)
            return [replace(r, metric=120, damage=120) if tuple(a[0]) == (299,) and tuple(a[1]) == (399,)
                    else replace(r, metric=100, damage=100) for a, r in zip(axes, results)]

    save = make_save(tmp_path, BASE)
    settings = CycleSettings(depth=1, width=1, polish_stalls=2, candidates=64)
    session = CycleSession(save, settings, tmp_path/"联动", client=Linked(save.unit_ids), log=lambda _: None)
    assert session.run() == 0
    assert session.summary["final"] == 120
    assert session.job.history[0]["damage"] == 120
    assert [r["gain"] for r in session.summary["polish_history"]] == [0, 0]


def test_polish_generates_three_way_permutations_with_boss_row_preserved():
    from PcrTimelineTester.cycle import polish_moves
    axis = ((500,), (500.001,), (500.002,), (), (), (900,))
    trace = Trace({"UBTime": axis[:5], "TpChangeEvents": []}, list(range(5)))
    found = polish_moves(axis, trace, 5399, 64, 0)
    assert ((500.002,), (500,), (500.001,), (), (), (900,)) in found


def test_burst_requires_matching_target_and_damage_type():
    from PcrTimelineTester.cycle import burst_moves
    units = list(range(1, 6))
    axis = ((201,), (310,), (), (), (), (500,))
    buff = dict(IsBuff=False, BuffParamKindName="MAGIC_DEF", SourceUnitId=1,
                TargetUnitId=100, StartFrame=205, EndFrame=309, Value=-42)
    hit = dict(Frame=311, SourceUnitId=2, TargetUnitId=100, Damage=42, DamageTypeName="MGC")
    stats = dict(UBTime=axis[:5], TpChangeEvents=[], BuffDebuffEvents=[buff], HitTraceEvents=[hit])
    assert len(burst_moves(axis, Trace(stats, units))) == 3
    for change in (dict(TargetUnitId=101), dict(DamageTypeName="ATK")):
        assert not burst_moves(axis, Trace(dict(stats, HitTraceEvents=[dict(hit, **change)]), units))


def test_combinations_preserve_disjoint_edits_and_boss_lane():
    from PcrTimelineTester.cycle import Branch, combined_axes
    parent = Branch(((300, 400), (), (), (), (), (800,)), 100, (), ("原轴",))
    a = replace(parent, axis=((299, 400), *parent.axis[1:]), path=("提前第一刀",))
    b = replace(parent, axis=((300, 401), *parent.axis[1:]), path=("推迟第二刀",))
    c = replace(parent, axis=((298, 400), *parent.axis[1:]), path=("同刀冲突",))
    assert list(combined_axes(parent, [a, b], 1))[0][0] == ((299, 401), *parent.axis[1:])
    assert not list(combined_axes(parent, [a, c], 1))
