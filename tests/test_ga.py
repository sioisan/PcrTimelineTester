"""语义 GA 模式的测试（基因/指纹/引擎/接口契约/任务/会话）。"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import httpx
import pytest

from conftest import ENEMY_IDS, UNIT_IDS, make_save, timeline_raw, write_save

from PcrTimelineTester.client import TuneError
from PcrTimelineTester.ga.engine import (
    BattleSample, Candidate, Evaluation, GAConfig, GenerationState, optimize,
)
from PcrTimelineTester.ga.evaluator import ConvertedAxis, GaEvaluator
from PcrTimelineTester.ga.fingerprint import (
    ActionProgressFingerprint, fingerprint_distance, fingerprints_close, sample_frames,
)
from PcrTimelineTester.ga.job import GaCancelled, GaPrepared, GaJob
from PcrTimelineTester.ga.semantics import (
    EMPTY_GENOME, ROLE_COUNT, SemanSequenceType, SemanSkillType, SemanUBType,
    SemanticConversionProfile, full_genome, genome_from_rows, search_pools, sequence_roles,
)
from PcrTimelineTester.ga.settings import GaSettings
from PcrTimelineTester.savefile import read_save, write_tuned_save
from PcrTimelineTester.session import GaSession

# --------------------------------------------------------------------- semantics


def test_genome_from_rows_trims_sentinels() -> None:
    genome = genome_from_rows([[1, 2, -1, -1], [3, -1], [], [], [7]], trim_sentinels=True)
    assert genome == ((1, 2), (3,), (), (), (7,))
    with pytest.raises(ValueError):
        genome_from_rows([[1], [2]])


def test_sequence_roles_must_consume_every_gene() -> None:
    lanes = ((1, 2), (3,), (4,), (), ())
    assert sequence_roles([0, 1, 0, 2], lanes) == (0, 1, 0, 2)
    with pytest.raises(ValueError):
        sequence_roles([0, 1, 0], lanes)
    with pytest.raises(ValueError):
        sequence_roles([0, 1, 0, 5], lanes)


def test_search_pools_include_seed_delay_and_legacy() -> None:
    seed = ((10,), (20,), (30,), (40,), (50,))
    pools = search_pools(seed, EMPTY_GENOME, legacy_ub=False, own_skill=True, other_skill=False, delay=3)
    for role, lane in enumerate(pools):
        assert seed[role][0] in lane
        assert {1, 2, 3}.issubset(set(lane))
        # 自己的技能 UB 族：10000 + role*1000 起，每段 10 个
        assert 10000 + role * 1000 in lane
        # 不含别人的技能族
        assert 10000 + ((role + 1) % ROLE_COUNT) * 1000 not in lane or role == ROLE_COUNT - 1
    legacy = search_pools(seed, EMPTY_GENOME, legacy_ub=True, own_skill=False, other_skill=False, delay=3)
    assert 10900 in legacy[0] or 11000 in legacy[0]


def test_full_genome_reassembles_prefix() -> None:
    prefix = ((9,), (8,), (7,), (6,), (5,))
    genome = ((1, 2), (3,), (), (), (4,))
    assert full_genome(prefix, genome)[0] == (9, 1, 2)


# -------------------------------------------------------------------- fingerprint


def test_sample_frames_are_increasing_and_bounded() -> None:
    frames = sample_frames(30)
    assert frames and list(frames) == sorted(frames)
    assert len(frames) <= 32
    assert frames[0] >= 1
    with pytest.raises(ValueError):
        sample_frames(200)


def _payload(progress: list[list[int]], frames: list[int], ids: list[int], loop: int = 10) -> dict:
    return {
        "Frames": frames,
        "UnitIds": ids,
        "StartFrames": [0 for _ in ids],
        "LoopFrames": [loop for _ in ids],
        "Progress": progress,
    }


def test_fingerprint_normalization_and_distance() -> None:
    frames = [10, 20]
    ids = [UNIT_IDS[0], UNIT_IDS[1]]
    first = ActionProgressFingerprint.from_payload(_payload([[1, 2], [3, 4]], frames, ids))
    shifted = ActionProgressFingerprint.from_payload(_payload([[11, 12], [13, 14]], frames, ids))
    other = ActionProgressFingerprint.from_payload(_payload([[1, 2], [8, 9]], frames, ids))
    assert fingerprints_close(first, shifted, 0.0)
    assert fingerprint_distance(first, other) > 0
    assert fingerprint_distance(first, shifted) == 0


def test_fingerprint_rejects_bad_payload() -> None:
    ids = [UNIT_IDS[0]]
    with pytest.raises(ValueError):
        ActionProgressFingerprint.from_payload(_payload([[1], [2]], [10, 5], ids))
    with pytest.raises(ValueError):
        ActionProgressFingerprint.from_payload(_payload([[1], [2]], [10, 20], [UNIT_IDS[0], UNIT_IDS[0]]))


# ------------------------------------------------------------------------- engine


def _sum_evaluator(seen: list) -> callable:
    def evaluate(genomes):
        assert len(set(genomes)) == len(genomes)  # 同一次提交里不重复
        seen.extend(genomes)
        results = []
        for genome in genomes:
            counts = tuple(len(lane) for lane in genome)
            damage = sum(sum(lane) for lane in genome)
            results.append(Evaluation(damage=damage, frames=100, result=0, ub_counts=counts))
        return results
    return evaluate


def test_optimize_improves_and_skips_duplicates() -> None:
    seen: list = []
    config = GAConfig(population=12, generations=6, islands=2, seed=7, fingerprint_distance=None)
    state = optimize(
        initial=((0,), (0,), (0,), (0,), (0,)),
        prefix=EMPTY_GENOME,
        pools=((0, 1, 2, 3),) * ROLE_COUNT,
        battle_time=90,
        evaluator=_sum_evaluator(seen),
        config=config,
    )
    assert state.generation == 6
    assert state.best.evaluation.damage > 0
    assert state.simulations == len(seen)


def test_unchanged_generation_reuses_checkpoint_scores() -> None:
    seen = []
    config = GAConfig(population=8, islands=2, generations=4, crossover=0, mutation=0,
                      migration_interval=0, disaster_patience=0)
    kwargs = dict(initial=EMPTY_GENOME, prefix=EMPTY_GENOME,
                  pools=((0, 1),) * ROLE_COUNT, battle_time=90, config=config)
    checkpoints = []
    state = optimize(evaluator=_sum_evaluator(seen), on_checkpoint=checkpoints.append, **kwargs)
    assert state.simulations == checkpoints[0].simulations == len(seen)
    seen.clear()
    assert optimize(evaluator=_sum_evaluator(seen), resume=checkpoints[1], **kwargs) == state
    assert not seen


def test_optimize_checkpoint_and_resume_are_deterministic() -> None:
    checkpoints: list[GenerationState] = []
    config = GAConfig(population=8, generations=4, islands=2, seed=11, fingerprint_distance=None)
    kwargs = dict(
        initial=((0,), (0,), (0,), (0,), (0,)),
        prefix=EMPTY_GENOME,
        pools=((0, 1, 2),) * ROLE_COUNT,
        battle_time=90,
        config=config,
    )
    first = optimize(evaluator=_sum_evaluator([]), on_checkpoint=checkpoints.append, **kwargs)
    assert [state.generation for state in checkpoints] == [0, 1, 2, 3, 4]
    resumed = optimize(evaluator=_sum_evaluator([]), resume=checkpoints[2], **kwargs)
    assert resumed.best.evaluation.damage == first.best.evaluation.damage
    with pytest.raises(ValueError):
        optimize(evaluator=_sum_evaluator([]), resume=replace(checkpoints[2], generation=99), **kwargs)


def test_evaluation_score_prefers_faster_win() -> None:
    fast = Evaluation(damage=100, frames=1800, result=0, ub_counts=(1,) * ROLE_COUNT)
    slow = Evaluation(damage=100, frames=3000, result=0, ub_counts=(1,) * ROLE_COUNT)
    assert fast.score(90) > slow.score(90)
    assert Evaluation(damage=100, frames=100, result=1, ub_counts=(1,) * ROLE_COUNT).score(90) == 100


def test_generation_state_round_trips() -> None:
    state = GenerationState(
        generation=3,
        islands=(((Candidate(genome=((1,),) * ROLE_COUNT,
                              evaluation=Evaluation(100, 10, 0, (1,) * ROLE_COUNT)),)),),
        best=Candidate(genome=((1,),) * ROLE_COUNT,
                       evaluation=Evaluation(100, 10, 0, (1,) * ROLE_COUNT)),
        best_generation=2,
        rng_state=(3, (1, 2, 3), None),
        simulations=5,
        exact_duplicates=1,
        behavior_duplicates=2,
        observed=6,
    )
    again = GenerationState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert again.generation == 3
    assert again.best.genome == state.best.genome
    assert again.rng_state == state.rng_state


# ---------------------------------------------------------------------- evaluator

REAL_CLIENT = httpx.Client


def _install(monkeypatch, handler) -> None:
    monkeypatch.setattr(
        httpx, "Client",
        lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout")),
    )


def _profile() -> SemanticConversionProfile:
    return SemanticConversionProfile(sequence_type=SemanSequenceType.LANES)


def _evaluator(monkeypatch, handler, **kwargs) -> GaEvaluator:
    _install(monkeypatch, handler)
    params = dict(
        context={"PlayerList": [], "Settings": {"RandomSeed": 1}},
        endpoint="http://127.0.0.1:8080/simulate",
        battle_time=90,
        battle_seed=666,
        profile=_profile(),
        capture_frames=(10, 20),
        batch_size=4,
        unit_ids=UNIT_IDS,
        log=lambda _message: None,
    )
    params.update(kwargs)
    return GaEvaluator(**params)


def _battle_reply(request: dict) -> dict:
    genome = request["SemanUBExecTimeList"]
    damage = 100 + sum(sum(lane) for lane in genome)
    payload = {
        "Success": True,
        "CurrentSeed": request["Settings"]["RandomSeed"],
        "Stats": {"TotalDamage": damage},
        "BattleFrames": 1800,
        "Result": 0,
        "SemanUBNum": [len(lane) for lane in genome],
    }
    if request.get("ActionProgressFrames"):
        frames = list(request["ActionProgressFrames"])
        payload["Stats"]["ActionProgress"] = _payload(
            [[index + 1 for _ in UNIT_IDS] for index, _ in enumerate(frames)],
            frames,
            list(UNIT_IDS),
            loop=1800,
        )
    return payload


def test_evaluator_request_contract(monkeypatch) -> None:
    bodies: list[list[dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        bodies.append(body)
        return httpx.Response(200, json=[_battle_reply(item) for item in body])

    evaluator = _evaluator(monkeypatch, handler)
    results = evaluator.evaluate([((0,), (0,), (0,), (0,), (0,)), ((1, 1), (0,), (), (), ())])
    body = bodies[0]
    assert len(body) == 2
    first = body[0]
    assert first["IsSemanMode"] is True
    assert first["IsGuildBattle"] is True
    assert first["NeedOnceResult"] is True
    assert first["SemanUBExecTimeList"] == [[0], [0], [0], [0], [0]]
    assert first["UseSequenceUB"] is False and first["SemanUBSequenceData"] == []
    assert first["ActionProgressFrames"] == [10, 20]
    assert "ActionProgressFrames" not in body[1]
    assert first["CalcStats"] == 3
    assert results[0].fingerprint is not None
    assert results[1].fingerprint is None
    assert results[1].damage == 102
    assert evaluator.battles == 2


def test_evaluator_sequence_and_multiseed(monkeypatch) -> None:
    bodies: list[list[dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        bodies.append(body)
        return httpx.Response(200, json=[_battle_reply(item) for item in body])

    evaluator = _evaluator(
        monkeypatch, handler, profile=SemanticConversionProfile(sequence_type=SemanSequenceType.SEQUENCE),
        seeds=(666, 777), batch_size=8, roles=(0, 1),
    )
    results = evaluator.evaluate([((1,), (2,), (), (), ())])
    assert len(bodies[0]) == 2  # 两个种子
    assert bodies[0][0]["UseSequenceUB"] is True
    assert bodies[0][0]["SemanUBSequenceData"] == [0, 1]
    assert bodies[0][0]["Settings"]["RandomSeed"] == 666
    assert bodies[0][1]["Settings"]["RandomSeed"] == 777
    assert results[0].damage == 100 + 1 + 2
    assert results[0].samples


def test_evaluator_convert_and_lanes_to_frames(monkeypatch) -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        seen.extend(body)
        if body[0]["CalcStats"] == 32:
            reply = {
                "Success": True, "CurrentSeed": body[0]["Settings"]["RandomSeed"],
                "Stats": {"UbConversion": {"SemanUBExecTimeList": [[1], [2], [], [], []],
                                           "SemanUBSequenceData": [0, 1]}},
            }
        elif body[0]["CalcStats"] == 2:
            reply = {
                "Success": True, "CurrentSeed": body[0]["Settings"]["RandomSeed"],
                "Stats": {"UBTime": [[100, 200], [300], [0], [0], [0]]},
            }
        else:
            reply = _battle_reply(body[0])
        return httpx.Response(200, json=[reply])

    evaluator = _evaluator(monkeypatch, handler)
    converted = evaluator.convert([[10, 20], [30], [0], [0], [0]])
    assert isinstance(converted, ConvertedAxis)
    assert converted.lanes == ((1,), (2,), (), (), ())
    assert seen[0]["CalcStats"] == 32
    assert seen[0]["IsSemanMode"] is False
    assert seen[0]["UBExecTimeList"] == [[10, 20], [30], [0], [0], [0]]
    frames = evaluator.lanes_to_frames(((1,), (2,), (), (), ()), (0, 1))
    assert frames == [[100.0, 200.0], [300.0], [0.0], [0.0], [0.0]]
    assert seen[1]["CalcStats"] == 2
    assert seen[1]["IsSemanMode"] is True


def test_evaluator_reports_server_failures(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        return httpx.Response(200, json=[{**item, "Success": False, "Message": "炸了"} for item in body])

    evaluator = _evaluator(monkeypatch, handler)
    with pytest.raises(TuneError):
        evaluator.evaluate([EMPTY_GENOME])


# --------------------------------------------------------------------------- job


class FakeEvaluator:
    """把基因和换成伤害，并记录调用顺序。"""

    def __init__(self, *, frames: int = 1800, fail_on: int = 90) -> None:
        self.battles = 0
        self.calls: list = []
        self.frames = frames
        self.fail_on = fail_on
        self.closed = False
        self.profile_frames: dict = {}
        self.frame_reference: int | None = None

    def _damage(self, lanes) -> int:
        return 100 + sum(sum(lane) for lane in lanes)

    def initial_axis(self, *, frames=(), lanes=EMPTY_GENOME, roles=None):
        self.calls.append(("initial_axis", tuple(tuple(lane) for lane in lanes)))
        seed = tuple(tuple(lane) for lane in lanes) if any(lanes) else tuple((index + 1,) for index in range(ROLE_COUNT))
        return ConvertedAxis(seed, roles)

    def evaluate(self, genomes):
        self.calls.append(("evaluate", len(genomes)))
        self.battles += len(genomes)
        return [
            Evaluation(self._damage(genome), self.frames, 0, tuple(len(lane) for lane in genome))
            for genome in genomes
        ]

    def replay(self, lanes, roles=None):
        self.calls.append(("replay", tuple(tuple(lane) for lane in lanes)))
        self.battles += 1
        return (
            Evaluation(self._damage(lanes), self.frames, 0, tuple(len(lane) for lane in lanes)),
            [[float(index * 10)] for index in range(ROLE_COUNT)],
        )

    def convert(self, frames):
        return ConvertedAxis(tuple((index + 1,) for index in range(ROLE_COUNT)), None)

    def lanes_to_frames(self, lanes, roles=None, *, profile=None):
        frames = self.profile_frames.get(profile) if profile is not None else None
        if frames is not None:
            return [list(row) for row in frames]
        return [[float(index * 10)] for index, _ in enumerate(lanes)]

    def frame_damage(self, frames):
        self.calls.append(("frame_damage",))
        self.battles += 1
        if self.frame_reference is None:
            raise RuntimeError("没有参考伤害")
        return self.frame_reference

    def close(self):
        self.closed = True


def _ga_settings(**overrides) -> GaSettings:
    base = dict(population=8, generations=3, islands=2, fingerprint_distance=None, ga_seed=5, verify=False)
    base.update(overrides)
    return GaSettings(**base)


def test_ga_job_runs_and_keeps_the_best(tmp_path) -> None:
    save = make_save(tmp_path, [[470, 500], [480], [490], [500], [510]])
    evaluator = FakeEvaluator()
    job = GaJob(save, _ga_settings(), log=lambda _message: None, evaluator=evaluator)
    summary = job.run()
    assert summary["mode"] == "ga"
    assert summary["baseline"] == 100 + 1 + 2 + 3 + 4 + 5
    assert summary["final"] >= summary["baseline"]
    assert summary["simulations"] > 0
    export = summary["export"]
    assert len(export["lanes"]) == ROLE_COUNT
    assert export["frames"] == [[0.0], [10.0], [20.0], [30.0], [40.0]]
    assert job.state is not None and job.state.generation == 3


def test_ga_job_cancel_writes_no_export(tmp_path) -> None:
    save = make_save(tmp_path, [[470, 500], [480], [490], [500], [510]])
    job = GaJob(save, _ga_settings(), log=lambda _message: None, evaluator=FakeEvaluator())
    original = job.evaluator.evaluate

    def cancelling(genomes):
        job.cancel_run()
        return original(genomes)

    job.evaluator.evaluate = cancelling  # type: ignore[method-assign]
    with pytest.raises(GaCancelled):
        job.run()
    assert job.summary(0.1)["export"] is None


def test_ga_job_state_round_trip(tmp_path) -> None:
    save = make_save(tmp_path, [[470, 500], [480], [490], [500], [510]])
    job = GaJob(save, _ga_settings(), log=lambda _message: None, evaluator=FakeEvaluator())
    job.run()
    data = json.loads(json.dumps(job.state_dict()))
    resumed = GaJob(save, _ga_settings(), log=lambda _message: None, evaluator=FakeEvaluator())
    resumed.apply_state(data)
    assert resumed.state is not None
    assert resumed.state.generation == job.state.generation
    assert resumed.prepared is not None



def _target_evaluator(target: tuple, reward: int) -> callable:
    def evaluate(genomes):
        return [
            Evaluation(
                damage=reward if genome == target else 0,
                frames=100, result=0, ub_counts=tuple(len(lane) for lane in genome),
            )
            for genome in genomes
        ]
    return evaluate


def test_elite_count_keeps_the_best_individual() -> None:
    # 起点就是最优解，基因池里只有「变差」的 0：没有精英保留的话几代就把它冲掉了。
    target = ((3,),) * ROLE_COUNT
    config = GAConfig(
        population=6, generations=3, islands=1, seed=5,
        elite_count=2, mutation=1.0, fingerprint_distance=None,
    )
    state = optimize(
        initial=target,
        prefix=EMPTY_GENOME,
        pools=((0,),) * ROLE_COUNT,
        battle_time=90,
        evaluator=_target_evaluator(target, 100),
        config=config,
    )
    assert state.best.evaluation.damage == 100
    assert target in [candidate.genome for candidate in state.islands[0]]
    without_elite = optimize(
        initial=target,
        prefix=EMPTY_GENOME,
        pools=((0,),) * ROLE_COUNT,
        battle_time=90,
        evaluator=_target_evaluator(target, 100),
        config=replace(config, elite_count=0),
    )
    assert target not in [candidate.genome for candidate in without_elite.islands[0]]


def test_stagnation_limit_stops_early() -> None:
    def flat(genomes):
        return [Evaluation(5, 100, 0, tuple(len(lane) for lane in genome)) for genome in genomes]

    config = GAConfig(
        population=6, generations=50, islands=1, seed=3,
        stagnation_limit=3, fingerprint_distance=None,
    )
    state = optimize(
        initial=((0,),) * ROLE_COUNT,
        prefix=EMPTY_GENOME,
        pools=((0, 1),) * ROLE_COUNT,
        battle_time=90,
        evaluator=flat,
        config=config,
    )
    assert state.stopped_early is True
    assert state.generation == 3


def test_mutation_knobs_are_validated_and_round_trip() -> None:
    with pytest.raises(ValueError):
        GAConfig(mutation_decay=1.5)
    with pytest.raises(ValueError):
        GAConfig(mutation_floor=-0.1)
    with pytest.raises(ValueError):
        GAConfig(population=4, elite_count=4)
    config = GAConfig(population=8, mutation_decay=0.9, mutation_floor=0.05, elite_count=3)
    restored = GAConfig.from_dict(config.to_dict())
    assert restored.mutation_decay == 0.9
    assert restored.mutation_floor == 0.05
    assert restored.elite_count == 3


def test_ga_job_restarts_pick_the_best_round(tmp_path) -> None:
    save = make_save(tmp_path, [[470, 500], [480], [490], [500], [510]])
    logs: list = []
    settings = _ga_settings(islands=1, population=6, generations=2, restarts=2)
    job = GaJob(save, settings, log=logs.append, evaluator=FakeEvaluator())
    summary = job.run()
    assert summary["restarts"] == 2
    assert [item["round"] for item in job.rounds] == [1, 2]
    assert [item["seed"] for item in job.rounds] == [settings.ga_seed, settings.ga_seed + 1]
    assert summary["final"] >= summary["baseline"]
    assert any("第 2/2 轮" in line for line in logs)


def test_ga_settings_keep_the_new_knobs() -> None:
    settings = GaSettings(
        elite_count=4, mutation_decay=0.8, mutation_floor=0.1, stagnation_limit=6, restarts=3,
    )
    assert settings.ga_config().elite_count == 4
    assert settings.ga_config().stagnation_limit == 6
    again = GaSettings.from_dict(settings.to_dict())
    assert again == settings

def test_ga_settings_validate_and_round_trip() -> None:
    settings = GaSettings()
    with pytest.raises(ValueError):
        GaSettings(battle_time=91)
    with pytest.raises(ValueError):
        GaSettings(samples=200)
    with pytest.raises(ValueError):
        GaSettings.from_dict({"nonsense": 1})
    again = GaSettings.from_dict(settings.to_dict())
    assert again == settings
    assert settings.seeds()[0] == settings.seed


# ----------------------------------------------------------------------- session


def test_ga_session_writes_semantic_workbook(tmp_path) -> None:
    save = make_save(tmp_path, [[470, 500], [480], [490], [500], [510]])
    run_dir = tmp_path / "run"
    logs: list[str] = []
    session = GaSession(save, _ga_settings(), run_dir, log=logs.append, evaluator=FakeEvaluator())
    assert session.run() == 0
    assert (run_dir / "config.json").is_file()
    assert (run_dir / "state.json").is_file()
    assert (run_dir / "report.json").is_file()
    assert (run_dir / "报告.md").is_file()
    workbook = run_dir / "优化结果.xlsx"
    assert workbook.is_file()
    tuned = read_save(workbook, require_frames=False)
    assert tuned.has_semantic
    assert tuned.has_frames
    group = tuned.group
    assert group["useSemanMode"] is True
    assert group["useSequenceUB"] is False
    report = (run_dir / "报告.md").read_text(encoding="utf-8")
    assert "语义 UB" in report
    assert "基线期望伤害" in report


def _semantic_save(tmp_path, *, lanes, frames=(), sequence=None):
    """造一条带语义数据的轴（默认没有角色序列）。"""
    raw = timeline_raw(frames or [[470], [480], [490], [500], [510]])
    group = raw["playerGroupData"]
    group["SemanUBExecTimeData"] = [list(lane) for lane in lanes]
    group["useSemanMode"] = True
    group["SemanUBSequenceData"] = list(sequence) if sequence else []
    group["useSequenceUB"] = bool(sequence)
    path = tmp_path / "语义轴.xlsx"
    write_save(path, raw)
    return read_save(path, require_frames=False)


def test_job_falls_back_to_lanes_when_the_axis_has_no_sequence(tmp_path) -> None:
    save = _semantic_save(tmp_path, lanes=[[1], [0], [0], [0], [0]])
    assert save.sequence is None
    lines: list[str] = []
    settings = _ga_settings(sequence_type=SemanSequenceType.SEQUENCE)
    job = GaJob(save, settings, log=lines.append, evaluator=FakeEvaluator())
    job.run()
    assert job.settings.sequence_type is SemanSequenceType.LANES
    assert any("没有角色序列" in line for line in lines)


def test_job_detects_the_profile_that_reproduces_the_frames(tmp_path) -> None:
    frames = [[470], [480], [490], [500], [510]]
    save = _semantic_save(tmp_path, lanes=[[1], [0], [7], [0], [3]], frames=frames)
    evaluator = FakeEvaluator()
    hits = SemanticConversionProfile(SemanUBType.SERIES_1, SemanSkillType.SERIES_10, SemanSequenceType.LANES)
    for ub in SemanUBType:
        for skill in SemanSkillType:
            for sequence in SemanSequenceType:
                evaluator.profile_frames[
                    SemanticConversionProfile(ub, skill, sequence)
                ] = frames if (ub, skill, sequence) == (SemanUBType.SERIES_1, SemanSkillType.SERIES_10, SemanSequenceType.LANES) else [[1], [2], [3], [4], [5]]
    lines: list[str] = []
    job = GaJob(save, _ga_settings(), log=lines.append, evaluator=evaluator)
    job.run()
    assert job.settings.skill_type is SemanSkillType.SERIES_10
    assert job.profile_detected is True
    assert any("语义口径自检" in line for line in lines)


def test_job_starts_from_the_frame_axis_when_the_saved_semantic_data_is_stale(tmp_path):
    """轴里的语义数据和自己的帧轴对不上时，起点改用帧轴重推，别拿错的语义数据起跑。"""
    frames = [[400, 900], [500], [600], [700], [800]]
    save = _semantic_save(tmp_path, lanes=((5,), (6,), (7,), (8,), (9,)), frames=frames)
    evaluator = FakeEvaluator()
    evaluator.frame_reference = 100
    log: list[str] = []
    job = GaJob(save, _ga_settings(generations=0), log=log.append, evaluator=evaluator)

    job.run()

    assert job.semantic_usable is False
    # 起点用的是帧轴（lanes 传空），不是轴里那五条对不上的语义基因
    assert evaluator.calls[0] == ("initial_axis", EMPTY_GENOME)
    assert any("起点改成用帧轴重新推出" in line for line in log)


def test_job_warns_when_the_semantic_start_does_not_match_the_frames(tmp_path) -> None:
    save = _semantic_save(
        tmp_path, lanes=[[1], [0], [7], [0], [3]], frames=[[470], [480], [490], [500], [510]],
    )
    evaluator = FakeEvaluator()
    evaluator.frame_reference = 999_999
    lines: list[str] = []
    job = GaJob(save, _ga_settings(), log=lines.append, evaluator=evaluator)
    summary = job.run()
    assert summary["frame_damage"] == 999_999
    assert any("不一致" in line for line in lines)
