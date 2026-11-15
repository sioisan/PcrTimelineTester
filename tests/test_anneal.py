"""退火模式的单元测试：邻域算子、温度、接受准则、任务与产物。"""

from __future__ import annotations

import dataclasses
import json
import random
from pathlib import Path

import pytest

from conftest import make_save

from PcrTimelineTester import modes
from PcrTimelineTester.anneal import (
    AnnealCancelled,
    AnnealJob,
    AnnealSettings,
    AnnealState,
    anneal,
    mutate,
    temperature_at,
)
from PcrTimelineTester.ga.engine import Candidate, Evaluation
from PcrTimelineTester.ga.evaluator import ConvertedAxis
from PcrTimelineTester.ga.semantics import EMPTY_GENOME, ROLE_COUNT, Genome
from PcrTimelineTester.session import AnnealSession


class FakeEvaluator:
    """把基因和换成伤害；伤害随「基因和」单调上升，方便断言。"""

    def __init__(self, *, frames: int = 1800) -> None:
        self.battles = 0
        self.calls: list = []
        self.frames = frames
        self.closed = False
        self.requested: list[Genome] = []

    def _damage(self, lanes) -> int:
        return 100 + sum(sum(lane) for lane in lanes)

    def initial_axis(self, *, frames=(), lanes=EMPTY_GENOME, roles=None):
        self.calls.append(("initial_axis", tuple(tuple(lane) for lane in lanes)))
        seed = tuple(tuple(lane) for lane in lanes) if any(lanes) else tuple((index + 1,) for index in range(ROLE_COUNT))
        return ConvertedAxis(seed, roles)

    def evaluate(self, genomes):
        self.calls.append(("evaluate", len(genomes)))
        self.battles += len(genomes)
        self.requested.extend(tuple(tuple(lane) for lane in genome) for genome in genomes)
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
        return [[float(index * 10)] for index, _ in enumerate(lanes)]

    def frame_damage(self, frames):
        raise RuntimeError("没有参考伤害")

    def close(self):
        self.closed = True


def _settings(**overrides) -> AnnealSettings:
    base = dict(iterations=20, initial_temperature=0.05, final_temperature=0.001, restarts=1,
                report_every=5, fingerprint_distance=None, verify=False)
    base.update(overrides)
    return AnnealSettings(**base)


INITIAL: Genome = tuple((1,) for _ in range(ROLE_COUNT))


def _pools() -> Genome:
    return tuple((0, 1, 2, 3) for _ in range(ROLE_COUNT))


def _save(tmp_path):
    return make_save(tmp_path, [[470, 500], [480], [490], [500], [510]])


# ------------------------------------------------------------------ 纯函数
def test_temperature_falls_geometrically() -> None:
    settings = _settings(initial_temperature=1.0, final_temperature=0.01)
    values = [temperature_at(settings, 1.0, index / 10) for index in range(11)]
    assert values[0] == pytest.approx(1.0)
    assert values[-1] == pytest.approx(0.01, rel=1e-6)
    assert all(later < earlier for earlier, later in zip(values, values[1:]))


def test_every_operator_returns_a_valid_genome() -> None:
    settings = _settings()
    rng = random.Random(7)
    for operator in ("swap", "double", "chain", "manual"):
        for _ in range(20):
            genome, used = mutate(EMPTY_GENOME, _pools(), settings, rng, operator=operator)
            assert used == operator
            assert len(genome) == ROLE_COUNT
            assert all(allele in (0, 1, 2, 3) for lane in genome for allele in lane)


def test_anneal_accepts_worse_steps_only_by_temperature() -> None:
    settings = _settings(initial_temperature=0.5, final_temperature=0.5)

    def evaluator_for(damage_by_sum):
        class _E:
            battles = 0

            def evaluate(self, genomes):
                self.battles += len(genomes)
                return [Evaluation(damage_by_sum(sum(sum(l) for l in g)), 1800, 0,
                                   tuple(len(l) for l in g)) for g in genomes]
        return _E()

    # 平坦地形、温度接近 0：几乎不可能接受「不变」以外的步，且没有更好的步
    hot = anneal(
        INITIAL,
        settings=settings,
        evaluator=evaluator_for(lambda total: 1000),
        prefix=EMPTY_GENOME,
        pools=_pools(),
        baseline=Evaluation(1000, 1800, 0, (1,) * ROLE_COUNT),
        battle_time=90,
        rng_seed=3,
        log=lambda _message: None,
        cancelled=lambda: False,
        resume=None,
    )
    state, history = hot
    assert state.iteration == settings.iterations
    assert 0 < len(history) <= settings.iterations // settings.report_every
    assert state.best is not None and state.best.evaluation.damage >= 1000


def test_duplicates_are_reused_instead_of_requested() -> None:
    settings = _settings(iterations=6, allow_duplicates=False)

    class _E:
        def __init__(self) -> None:
            self.battles = 0
            self.requested: list[Genome] = []

        def evaluate(self, genomes):
            self.battles += len(genomes)
            self.requested.extend(genomes)
            return [Evaluation(1000, 1800, 0, tuple(len(l) for l in g)) for g in genomes]

    evaluator = _E()
    state, _history = anneal(
        INITIAL,
        settings=settings,
        evaluator=evaluator,
        prefix=EMPTY_GENOME,
        pools=INITIAL,  # 池子里只有 1 → 邻居换不掉基因
        baseline=Evaluation(1000, 1800, 0, (1,) * ROLE_COUNT),
        battle_time=90,
        rng_seed=5,
        log=lambda _message: None,
        cancelled=lambda: False,
        resume=None,
    )
    assert state.best is not None
    assert len(set(evaluator.requested)) == len(evaluator.requested)  # 同一基因只请求一次
    assert evaluator.battles <= settings.iterations + 1


# ------------------------------------------------------------------ 任务
def test_job_runs_and_exports_the_best(tmp_path) -> None:
    save = _save(tmp_path)
    evaluator = FakeEvaluator()
    job = AnnealJob(save, _settings(), log=lambda _message: None, evaluator=evaluator)
    summary = job.run()
    assert summary["mode"] == "anneal"
    assert summary["iterations"] == 20
    assert summary["final"] >= summary["baseline"]
    assert summary["export"] is not None
    assert len(summary["export"]["lanes"]) == ROLE_COUNT
    assert summary["export"]["frames"] == [[0.0], [10.0], [20.0], [30.0], [40.0]]
    assert evaluator.closed is False  # close() 由会话负责


def test_job_cancel_writes_no_export(tmp_path) -> None:
    save = _save(tmp_path)
    job = AnnealJob(save, _settings(), log=lambda _message: None, evaluator=FakeEvaluator())
    original = job.evaluator.evaluate

    def cancelling(genomes):
        job.cancel_run()
        return original(genomes)

    job.evaluator.evaluate = cancelling  # type: ignore[method-assign]
    with pytest.raises(AnnealCancelled):
        job.run()
    assert job.summary(0.1)["export"] is None


def test_job_state_round_trip(tmp_path) -> None:
    save = _save(tmp_path)
    job = AnnealJob(save, _settings(iterations=8), log=lambda _message: None, evaluator=FakeEvaluator())
    summary = job.run()
    payload = job.state_dict()
    assert json.loads(json.dumps(payload))  # 可序列化

    resumed = AnnealJob(save, _settings(iterations=8), log=lambda _message: None, evaluator=FakeEvaluator())
    resumed.apply_state(payload)
    assert resumed.state is not None
    assert resumed.state.iteration == summary["iterations"]
    assert resumed.prepared is not None


def test_session_writes_artifacts(tmp_path) -> None:
    save = _save(tmp_path)
    run_dir = tmp_path / "run"
    session = AnnealSession(
        save,
        _settings(iterations=6),
        run_dir,
        log=lambda _message: None,
        evaluator=FakeEvaluator(),
    )
    with session:
        code = session.run()
    assert code == 0
    assert (run_dir / "config.json").exists()
    assert (run_dir / "报告.md").exists()
    assert (run_dir / "report.json").exists()
    assert session.workbook is not None and Path(session.workbook).exists()
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    assert config["mode"] == "anneal"


def test_session_cancel_leaves_no_workbook(tmp_path) -> None:
    save = _save(tmp_path)
    run_dir = tmp_path / "run"

    class _Cancel:
        def evaluate(self, genomes):
            return [Evaluation(1000, 1800, 0, tuple(len(l) for l in g)) for g in genomes]

        def initial_axis(self, *, frames=(), lanes=EMPTY_GENOME, roles=None):
            return ConvertedAxis(tuple((index + 1,) for index in range(ROLE_COUNT)), roles)

        def replay(self, lanes, roles=None):
            return Evaluation(1000, 1800, 0, (1,) * ROLE_COUNT), [[0.0]] * ROLE_COUNT

        def lanes_to_frames(self, lanes, roles=None, *, profile=None):
            return [[float(index)] for index in range(len(lanes))]

        def frame_damage(self, frames):
            raise RuntimeError("没有参考伤害")

        def convert(self, frames):
            return ConvertedAxis(EMPTY_GENOME, None)

        battles = 0

        def close(self):
            pass

    session = AnnealSession(save, _settings(iterations=4), run_dir, log=lambda _message: None,
                            evaluator=_Cancel())
    with session:
        session.cancel()
        code = session.run()
    assert code == 130
    assert session.workbook is None
    assert not (run_dir / "退火结果.xlsx").exists()


# ------------------------------------------------------------------ 模式接口
def test_anneal_mode_is_registered_and_round_trips(tmp_path) -> None:
    assert "anneal" in modes.keys()
    spec = modes.get("anneal")
    assert spec.needs_frames is False
    assert spec.workbook_name == "退火结果.xlsx"
    values = spec.defaults()
    assert len(spec.params) == 26
    save = _save(tmp_path)
    settings = spec.from_values(values, save)
    assert isinstance(settings, AnnealSettings)
    assert spec.values(settings) == values
    with pytest.raises(ValueError):
        spec.from_values({**values, "不存在的参数": 1}, save)
