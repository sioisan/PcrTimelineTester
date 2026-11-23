"""0 押轴（TP 满帧判据 + 逐刀退火）的单元测试。"""

import pytest

from conftest import UNIT_IDS, make_save

from PcrTimelineTester import modes
from PcrTimelineTester.zero import (
    STATS_BITS,
    TEAM_SIZE,
    ZeroJob,
    ZeroScore,
    ZeroSettings,
    build_lanes,
    full_plan,
    mutate,
    normalise,
    plan_rows,
    plan_text,
    temperatures,
    tp_ready_frames,
    zero_top,
)


def payload(rows):
    """造一个只带 TpChangeEvents 的回包。"""
    events = []
    for lane, frames in enumerate(rows):
        for frame in frames:
            events.append({
                "Frame": frame,
                "TargetLaneIndex": lane,
                "TargetUnitId": UNIT_IDS[lane],
                "EnergyBefore": 900,
                "EnergyAfter": 1000,
                "EnergyCap": 1000,
                "CrossedReady": True,
            })
    return {"Stats": {"TpChangeEvents": events}}


def test_tp_ready_frames_are_grouped_by_lane():
    got = tp_ready_frames(payload([[10, 20], [30], [], [40], [50, 60]]))
    assert got == ((10, 20), (30,), (), (40,), (50, 60))


def test_tp_ready_frames_fall_back_to_the_energy_crossing():
    events = [
        {"Frame": 5, "TargetLaneIndex": 0, "EnergyBefore": 800, "EnergyAfter": 1000, "EnergyCap": 1000},
        {"Frame": 9, "TargetLaneIndex": 0, "EnergyBefore": 100, "EnergyAfter": 200, "EnergyCap": 1000},
        {"Frame": 7, "TargetLaneIndex": 9, "EnergyBefore": 900, "EnergyAfter": 1000, "EnergyCap": 1000},
    ]
    got = tp_ready_frames({"Stats": {"TpChangeEvents": events}})
    assert got[0] == (5,)
    assert all(row == () for row in got[1:])


def test_build_lanes_only_writes_the_frames_that_are_pressed():
    opportunities = ((100, 200, 300), (110, 210), (120,), (130,), (140, 240))
    plan = (
        (True, False, True),
        (False, True),
        (),
        (True,),
        (True, True),
    )
    lanes = build_lanes(plan, opportunities)
    assert lanes == [[100.0, 300.0], [210.0], [], [130.0], [140.0, 240.0]]


def test_build_lanes_skips_depths_past_the_last_tp_full_frame():
    lanes = build_lanes(((True, True, True),) + ((),) * 4, ((10, 20), (), (), (), ()))
    assert lanes[0] == [10.0, 20.0]
    assert all(not lane for lane in lanes[1:])


def test_full_plan_presses_every_tp_full_frame_up_to_the_limit():
    plan = full_plan(((1, 2, 3), (4, 5), (), (6,), (7,) * 20), limit=12)
    assert [len(row) for row in plan] == [3, 2, 0, 1, 12]
    assert all(flag for row in plan for flag in row)
    assert plan[4] == (True,) * 12


def test_normalise_pads_and_trims_rows():
    plan = normalise(((True,), (False, False), (True, True, True)), depth=(2, 1, 3, 0, 0))
    assert len(plan) == TEAM_SIZE
    assert plan[0] == (True,)
    assert plan[1] == (False,)
    assert plan[4] == ()


def test_mutate_keeps_the_plan_inside_the_tp_windows():
    import random

    rng = random.Random(7)
    opportunities = ((10, 20, 30), (11,), (12, 22), (13,), (14,))
    plan = full_plan(opportunities, limit=3)
    for _ in range(300):
        plan = mutate(plan, rng, opportunities=opportunities, limit=3)
        assert len(plan) == TEAM_SIZE
        for index, row in enumerate(plan):
            assert len(row) <= max(3, len(opportunities[index]))
            assert all(isinstance(flag, bool) for flag in row)


def test_plan_text_and_rows_describe_the_tail():
    plan = ((True, False, True), (False,), (), (True, True), (False, False))
    assert plan_text(plan).split("、")[0] == "1 OXO（之后一律 O）"
    assert plan_rows(plan)[1] == "2: X（之后一律 X）"
    assert plan_rows(plan)[2] == "3: -（之后一律 X）"


def test_temperatures_cool_down_geometrically():
    settings = ZeroSettings(initial_temperature=0.5, final_temperature=0.01)
    values = temperatures(settings, 5)
    assert values[0] == pytest.approx(0.5)
    assert values[-1] == pytest.approx(0.01)
    assert all(a > b for a, b in zip(values, values[1:]))


def test_settings_round_trip_and_validation():
    settings = ZeroSettings(seed=666, samples=3, iterations=10, batch=2, ub_limit=8)
    assert settings.seeds() == (666, 667, 668)
    again = ZeroSettings.from_dict(settings.to_dict())
    assert again == settings
    with pytest.raises(ValueError):
        ZeroSettings(battle_time=0)
    with pytest.raises(ValueError):
        ZeroSettings(batch=0)
    with pytest.raises(ValueError):
        ZeroSettings(ub_limit=0)
    with pytest.raises(ValueError):
        ZeroSettings.from_dict({"nope": 1})


def test_request_for_presses_only_the_listed_frames(tmp_path):
    save = make_save(tmp_path, [[100, 200], [110], [120], [130], [140]])
    settings = ZeroSettings(seed=666, to_max_attr=True)
    from PcrTimelineTester.zero import ZeroEvaluator

    evaluator = ZeroEvaluator(save, settings, log=lambda _message: None)
    try:
        body = evaluator.request_for([[100.0, 200.0], [], [120.0], [130.0], []])
    finally:
        evaluator.close()
    assert body["UBExecTimeList"] == [[100.0, 200.0], [], [120.0], [130.0], []]
    assert body["SetToggles"] == [False] * TEAM_SIZE
    assert body["IsAutoMode"] is True
    assert body["IsSetMode"] is False
    assert body["NeedOnceResult"] is True
    assert body["CalcStats"] & 256 == 256
    assert body["UBExecTimeList"] != [list(row) for row in save.frames] or save.frames == []
    assert body["Settings"]["RandomSeed"] == 666


def test_cache_uses_actual_frames_and_verification_replays_all_seeds(tmp_path):
    from PcrTimelineTester.zero import ZeroEvaluator
    save = make_save(tmp_path, [[100]] * 5)
    seeds = []
    with ZeroEvaluator(save, ZeroSettings(samples=2)) as evaluator:
        def fetch(requests):
            replies = []
            for body in requests:
                seed = body["Settings"]["RandomSeed"]
                seeds.append(seed)
                replies.append({"Success": True, "CurrentSeed": seed, "Stats": {"TotalDamage": seed}})
            return replies

        evaluator._fetch = fetch
        plan = ((True,),) + ((),) * 4
        windows = ((100,),) + ((),) * 4
        longer = ((True, True),) + ((),) * 4
        scores = evaluator.evaluate([plan, longer], windows)
        assert [s.plan for s in scores] == [plan, longer]
        assert all(s.damage == 666.5 for s in scores)
        assert evaluator.evaluate([plan], windows)[0] == scores[0]
        assert seeds == [666, 667]
        evaluator.evaluate([plan], ((110,),) + ((),) * 4)
        evaluator.evaluate_lanes([scores[0].lanes])
        assert seeds == [666, 667] * 3


def test_request_pure_toggles_every_slot(tmp_path):
    save = make_save(tmp_path, [[100, 200], [110], [120], [130], [140]])
    from PcrTimelineTester.zero import ZeroEvaluator

    evaluator = ZeroEvaluator(save, ZeroSettings(), log=lambda _message: None)
    try:
        pure_set = evaluator.request_pure(set_mode=True)
        pure_auto = evaluator.request_pure(set_mode=False)
    finally:
        evaluator.close()
    assert pure_set["SetToggles"] == [True] * TEAM_SIZE
    assert pure_auto["SetToggles"] == [False] * TEAM_SIZE
    assert pure_set["IsAutoMode"] is True
    assert pure_set["UBExecTimeList"] == []


class FakeZero:
    def anchor_search(self):
        return None

    """假评估器：伤害随「按了几刀」线性上升。"""

    def __init__(self, save):
        self.save = save
        self.units = tuple(int(unit) for unit in save.unit_ids)
        self.battles = 0
        self.batches = 0

    def baseline(self):
        self.battles += 2
        return {
            "set_damage": 1000.0,
            "set_frames": ((533, 889), (554,), (562,), (575,), (588,)),
            "auto_damage": 800.0,
            "auto_frames": ((539,), (554,), (562,), (575,), (588,)),
            "opportunities": ((533, 889, 1285), (554, 1259), (562,), (575,), (588, 1139)),
        }

    def evaluate(self, plans, opportunities):
        self.batches += 1
        lanes = [build_lanes(plan, opportunities) for plan in plans]
        return self.evaluate_lanes(lanes, plans=plans)

    def evaluate_lanes(self, lanes, *, plans=None):
        out = []
        for index, lane in enumerate(lanes):
            plan = plans[index] if plans is not None else ()
            pressed = sum(len(row) for row in lane)
            out.append(ZeroScore(damage=1000.0 + pressed, frames=5399, result=2, plan=plan,
                                 opportunities=((533, 889, 1285), (554, 1259), (562,), (575,), (588, 1139)),
                                 lanes=tuple(tuple(float(frame) for frame in row) for row in lane)))
        self.battles += len(lanes)
        return out

    def evaluate_one(self, plan, opportunities):
        return self.evaluate([plan], opportunities)[0]

    def close(self):
        pass


def job_for(tmp_path, **kwargs):
    save = make_save(tmp_path, [[100, 200], [110], [120], [130], [140]])
    settings = ZeroSettings(iterations=6, batch=3, verify=False, **kwargs)
    fake = FakeZero(save)
    job = ZeroJob(save, settings, log=lambda _message: None, evaluator=fake)
    return job, fake


def test_job_anneals_from_the_full_set_plan(tmp_path):
    job, fake = job_for(tmp_path)
    summary = job.run()
    assert summary["rounds_total"] == 7
    assert summary["rounds_done"] == 7
    assert summary["battles"] == fake.battles
    assert summary["best"]["damage"] >= 1000.0
    assert summary["baseline"]["set_damage"] == 1000.0
    assert summary["opportunities"][0] == [533, 889, 1285]
    assert summary["export"] is not None
    lanes = summary["export"]["lanes"]
    plan = summary["best"]["plan"]
    assert len(lanes) == TEAM_SIZE
    assert [len(lane) for lane in lanes] == [sum(1 for flag in row if flag) for row in plan]
    assert lanes[0][:1] == [533.0]
    assert fake.batches >= 1


def test_job_state_round_trip(tmp_path):
    job, _fake = job_for(tmp_path)
    job.run()
    state = job.state_dict()
    other, _fake2 = job_for(tmp_path)
    other.apply_state(state)
    assert other.best is not None
    assert other.best.damage == job.best.damage
    assert other.opportunities == job.opportunities
    with pytest.raises(ValueError):
        other.apply_state({"mode": "roster"})


def test_resumed_job_starts_at_the_next_unfinished_round(tmp_path):
    job, _ = job_for(tmp_path)
    job._cancel = lambda: job.rounds_done >= 3
    assert job.run()["cancelled"]
    resumed, evaluator = job_for(tmp_path)
    resumed.apply_state(job.state_dict())
    summary = resumed.run()
    assert summary["rounds_done"] == summary["rounds_total"]
    assert [item["round"] for item in summary["history"]] == list(range(1, 8))
    assert summary["battles"] == job.battles_done + evaluator.battles
    assert summary["export"] is not None


def test_job_cancel_does_not_export(tmp_path):
    job, _fake = job_for(tmp_path)
    job.cancel_run()
    summary = job.run()
    assert summary["cancelled"] is True
    assert summary["export"] is None


def test_zero_top_returns_the_best_distinct_entries():
    summary = {"history": [
        {"round": 1, "damage": 10, "frames": 1},
        {"round": 2, "damage": 30, "frames": 1},
        {"round": 3, "damage": 30, "frames": 1},
        {"round": 4, "damage": 20, "frames": 2},
    ]}
    assert [row["damage"] for row in zero_top(summary, 2)] == [30, 20]


def test_mode_is_registered_with_the_tp_settings():
    spec = modes.get("zero")
    assert spec.label == "0押轴"
    assert [param.name for param in spec.params] == [
        "battle_time", "seed", "samples", "iterations", "batch",
        "initial_temperature", "final_temperature", "ub_limit", "timeout",
        "to_max_attr", "verify",
    ]
    settings = spec.from_values({"iterations": "50", "batch": "2"}, None)
    assert settings.iterations == 50
    assert settings.batch == 2
    assert spec.values(settings)["iterations"] == 50
    plan = "\n".join(spec.plan(None, settings))
    assert "50 轮" in plan
    assert "TP 满" in plan


def test_stats_bits_ask_for_tp_events():
    assert STATS_BITS & 1
    assert STATS_BITS & 2
    assert STATS_BITS & 256


def test_moves_can_switch_a_suffix_and_two_nearby_teammates():
    import random
    rng = random.Random(4)
    times = tuple(tuple(range(100 + i, 601 + i, 100)) for i in range(5))
    start = full_plan(times, limit=6)
    neighbors = [mutate(start, rng, opportunities=times, limit=6) for _ in range(300)]
    assert any(row == (True, True, False, False, False, False) for plan in neighbors for row in plan)
    assert any(sum(row != orig for row, orig in zip(plan, start)) == 2 for plan in neighbors)


def test_ally_ub_set_and_natural_auto_require_full_tp():
    from PcrTimelineTester.zero_anchor import next_operations
    units = (11, 22, 33, 44, 55)
    def tp(role, frame, full):
        return dict(TargetLaneIndex=role, Frame=frame, IsReadyAfter=full, CrossedReady=full)
    stats = {"TpChangeEvents": [tp(1, 90, True), tp(2, 104, True), tp(3, 90, True), tp(3, 103, False)],
             "SkillExecEvents": [dict(UnitId=u, StartFrame=95, EndFrame=110) for u in units]}
    choices = next_operations(((100, 0),), stats, units)
    assert choices[100.001, 1] == (True, "100 帧 1 号位 UB → 2 号位 SET（100 帧）")
    assert choices[110, 1] == (False, "100 帧 1 号位 UB → 2 号位 AUTO（110 帧）")
    assert (100.001, 2) not in choices and (110, 3) not in choices
    assert (105, 1) not in choices


def test_anchored_score_round_trips_the_same_frame_order():
    from PcrTimelineTester.zero_anchor import cast_order, player_lanes
    casts = ((1234., 4), (1234.001, 1), (1250., 2))
    lanes = player_lanes(casts) + ((900.,),)
    assert cast_order(lanes) == casts
    score = ZeroScore(100, 5399, 2, (), lanes=lanes, steps=("同帧 SET", "收招 AUTO"))
    assert ZeroScore.from_dict(score.to_dict()) == score
