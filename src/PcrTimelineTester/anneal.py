"""退火模式：模拟退火（SA）在语义 UB 轴上找更优的一刀。

* 温度随迭代几何下降：``T_i = T0 * (T_end / T0) ** (i / iterations)``；
  温度再按「观测到的 |delta| 滑动平均」缩放，评分量级大时不至于只能下坡；
* 邻域算子按概率挑一种：单点替换、双点互换、链式替换、手动指定，
  链式替换的长度上限就是「链长范围」（一步最多联动几处）；
* 接受准则：``delta >= 0`` 直接接受，否则以 ``exp(delta / T)`` 的概率接受
  （delta 用评分差，评分见 ``Evaluation.score``）；
* 允许把已经算过的基因记在表里直接复用它的分数，可选「允许重复状态」。

本模块只依赖 ``ga`` 包已有的基因表示、评估器和指纹。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
import random
import threading
import time

from .ga.engine import Candidate, Evaluation
from .ga.job import GaJob
from .ga.semantics import (
    DEFAULT_MAX_LANE_LENGTH,
    EMPTY_GENOME,
    ROLE_COUNT,
    Genome,
    genome_from_rows,
    point_neighbors,
)
from .ga.settings import GaSettings


class AnnealCancelled(RuntimeError):
    """用户按了停止。"""


# --------------------------------------------------------------------- 设置
@dataclass(frozen=True, slots=True)
class AnnealSettings:
    """一次退火任务的参数（``base`` 里放评估相关的公共项）。"""

    battle_time: int = 90
    seed: int = 666
    samples: int = 1
    batch_size: int = 16
    timeout: float = 180.0
    endpoint: str = "http://127.0.0.1:8080/simulate"
    iterations: int = 400
    initial_temperature: float = 0.02
    final_temperature: float = 0.0002
    cooling: str = "geometric"
    chain_range: int = 4
    moves: int = 1
    swap_chance: float = 0.55
    double_swap_chance: float = 0.15
    chain_chance: float = 0.20
    manual_chance: float = 0.10
    allow_duplicates: bool = False
    restarts: int = 1
    report_every: int = 25
    fingerprint_distance: float | None = 1.0
    verify: bool = True
    # 语义口径与基因池（与 GA 模式同义）
    ub_type: object = None
    skill_type: object = None
    sequence_type: object = None
    fixed_ub_counts: tuple[int, int, int, int, int] = (0, 0, 0, 0, 0)
    extra_genes: Genome = EMPTY_GENOME
    legacy_ub: bool | None = None
    own_skill: bool = True
    other_skill: bool = False
    delay: int = 3

    def __post_init__(self) -> None:
        if not 1 <= int(self.battle_time) <= 90:
            raise ValueError("战斗秒数必须在 1~90 之间")
        if not 1 <= int(self.samples) <= 100:
            raise ValueError("采样种子数必须在 1~100 之间")
        if int(self.iterations) < 1:
            raise ValueError("迭代次数至少为 1")
        if not 0.0 < float(self.initial_temperature) <= 1.0:
            raise ValueError("初始温度（相对基线）必须在 0~1 之间，例如 0.02")
        if not 0.0 <= float(self.final_temperature) <= float(self.initial_temperature):
            raise ValueError("最低温度必须不小于 0，且不超过初始温度")
        if int(self.moves) < 1 or int(self.moves) > 3:
            raise ValueError("每步改动次数只能是 1~3")
        if int(self.chain_range) < 1:
            raise ValueError("链长范围至少为 1")
        if int(self.restarts) < 0 or int(self.restarts) > 10000:
            raise ValueError("重启轮数必须在 1~20 之间")
        if int(self.report_every) < 1:
            raise ValueError("日志间隔至少为 1 次迭代")
        total = (
            float(self.swap_chance)
            + float(self.double_swap_chance)
            + float(self.chain_chance)
            + float(self.manual_chance)
        )
        if total <= 0:
            raise ValueError("四种邻域算子的概率不能全为 0")
        for name, value in (
            ("单点替换概率", self.swap_chance),
            ("双点互换概率", self.double_swap_chance),
            ("链式替换概率", self.chain_chance),
            ("手动指定概率", self.manual_chance),
        ):
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name}必须在 0~1 之间")
        self.ga_equivalent()

    # ------------------------------------------------------------------ 转换
    def ga_equivalent(self) -> GaSettings:
        """把公共项折成 GaSettings：评估器、口径、基因池都复用它。"""
        values = {
            "battle_time": int(self.battle_time),
            "seed": int(self.seed),
            "samples": int(self.samples),
            "batch_size": int(self.batch_size),
            "timeout": float(self.timeout),
            "endpoint": self.endpoint,
            "fingerprint_distance": self.fingerprint_distance,
            "fixed_ub_counts": tuple(int(v) for v in self.fixed_ub_counts),
            "extra_genes": self.extra_genes,
            "legacy_ub": self.legacy_ub,
            "own_skill": bool(self.own_skill),
            "other_skill": bool(self.other_skill),
            "delay": int(self.delay),
            "verify": bool(self.verify),
        }
        for key in ("ub_type", "skill_type", "sequence_type"):
            value = getattr(self, key)
            if value is not None:
                values[key] = value
        return GaSettings(**values)

    def with_changes(self, **changes) -> "AnnealSettings":
        return replace(self, **changes)

    def temperatures(self) -> tuple[float, float]:
        """返回 (T0, T_end) —— 单位是伤害，按基线量级缩放。"""
        return float(self.initial_temperature), float(self.final_temperature)

    def to_dict(self) -> dict:
        profile = None
        try:
            profile = self.ga_equivalent().conversion_profile.to_dict()
        except Exception:  # pragma: no cover - 极端情况下仍要能存盘
            profile = None
        data = {
            "battle_time": int(self.battle_time),
            "seed": int(self.seed),
            "samples": int(self.samples),
            "batch_size": int(self.batch_size),
            "timeout": float(self.timeout),
            "endpoint": self.endpoint,
            "iterations": int(self.iterations),
            "initial_temperature": float(self.initial_temperature),
            "final_temperature": float(self.final_temperature),
            "cooling": self.cooling,
            "chain_range": int(self.chain_range),
            "moves": int(self.moves),
            "swap_chance": float(self.swap_chance),
            "double_swap_chance": float(self.double_swap_chance),
            "chain_chance": float(self.chain_chance),
            "manual_chance": float(self.manual_chance),
            "allow_duplicates": bool(self.allow_duplicates),
            "restarts": int(self.restarts),
            "report_every": int(self.report_every),
            "fingerprint_distance": self.fingerprint_distance,
            "verify": bool(self.verify),
            "fixed_ub_counts": [int(v) for v in self.fixed_ub_counts],
            "extra_genes": [list(lane) for lane in self.extra_genes],
            "legacy_ub": self.legacy_ub,
            "own_skill": bool(self.own_skill),
            "other_skill": bool(self.other_skill),
            "delay": int(self.delay),
        }
        if profile:
            data.update(profile)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "AnnealSettings":
        known = {item.name for item in __import__("dataclasses").fields(cls)}
        extra = {"ub_type", "skill_type", "sequence_type"}
        unknown = set(data) - known - extra
        if unknown:
            raise ValueError("配置里有不认识的字段：" + "、".join(sorted(unknown)))
        values = {key: data[key] for key in data if key in known}
        if "fixed_ub_counts" in values:
            values["fixed_ub_counts"] = tuple(int(v) for v in values["fixed_ub_counts"])
        if "extra_genes" in values:
            values["extra_genes"] = genome_from_rows(values["extra_genes"])
        for key in ("ub_type", "skill_type", "sequence_type"):
            if data.get(key) is not None:
                values[key] = data[key]
        return cls(**values)


# --------------------------------------------------------------------- 状态
@dataclass(frozen=True, slots=True)
class AnnealState:
    """可续跑的退火状态。"""

    iteration: int = 0
    temperature: float = 0.0
    best: Candidate | None = None
    best_iteration: int = 0
    simulations: int = 0
    accepted: int = 0
    rejected: int = 0
    exact_duplicates: int = 0
    behavior_duplicates: int = 0
    stopped_early: bool = False

    def to_dict(self) -> dict:
        return {
            "iteration": int(self.iteration),
            "temperature": float(self.temperature),
            "best": None if self.best is None else self.best.to_dict(),
            "best_iteration": int(self.best_iteration),
            "simulations": int(self.simulations),
            "accepted": int(self.accepted),
            "rejected": int(self.rejected),
            "exact_duplicates": int(self.exact_duplicates),
            "behavior_duplicates": int(self.behavior_duplicates),
            "stopped_early": bool(self.stopped_early),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AnnealState":
        best = data.get("best")
        return cls(
            iteration=int(data.get("iteration", 0)),
            temperature=float(data.get("temperature", 0.0)),
            best=None if best is None else Candidate.from_dict(best),
            best_iteration=int(data.get("best_iteration", 0)),
            simulations=int(data.get("simulations", 0)),
            accepted=int(data.get("accepted", 0)),
            rejected=int(data.get("rejected", 0)),
            exact_duplicates=int(data.get("exact_duplicates", 0)),
            behavior_duplicates=int(data.get("behavior_duplicates", 0)),
            stopped_early=bool(data.get("stopped_early", False)),
        )


# --------------------------------------------------------------------- 邻域
def _choose_weighted(rng: random.Random, settings: AnnealSettings) -> str:
    weights = [
        (max(0.0, float(settings.swap_chance)), "swap"),
        (max(0.0, float(settings.double_swap_chance)), "double"),
        (max(0.0, float(settings.chain_chance)), "chain"),
        (max(0.0, float(settings.manual_chance)), "manual"),
    ]
    total = sum(weight for weight, _ in weights)
    roll = rng.random() * total
    upto = 0.0
    for weight, name in weights:
        upto += weight
        if roll <= upto:
            return name
    return "swap"


def _positions(genome: Genome) -> list[tuple[int, int]]:
    return [(role, index) for role, lane in enumerate(genome) for index in range(len(lane))]


def mutate(
    genome: Genome,
    pools: Genome,
    settings: AnnealSettings,
    rng: random.Random,
    *,
    operator: str | None = None,
    fixed_lengths: bool = False,
) -> tuple[Genome, str]:
    """生成一个邻居；返回 (新基因, 实际用到的算子名)。

    ``settings.moves`` 大于 1 时，一步里连着做几次改动（"swap+chain"），
    否则单点改动永远只能落在「只改一个基因」的邻域里，很难跳过局部最优。
    """
    lane_genome = [list(lane) for lane in genome]
    ops = [op for op in ("swap", "double", "chain", "manual") if _weight(settings, op) > 0]
    if not ops:
        ops = ["swap"]
    steps = max(1, min(3, int(getattr(settings, "moves", 1))))
    if steps > 1:
        single = replace(settings, moves=1)
        current = genome
        names: list[str] = []
        for _ in range(steps):
            current, step_name = mutate(current, pools, single, rng, operator=operator, fixed_lengths=fixed_lengths)
            names.append(step_name)
        return current, "+".join(dict.fromkeys(names))
    name = operator if operator in ops else _choose_weighted(rng, settings)
    filled = [role for role, lane in enumerate(lane_genome) if lane]
    if name == "chain" and not fixed_lengths and (not filled or rng.random() < 0.45):
        role = rng.randrange(ROLE_COUNT)
        lane = lane_genome[role]
        if lane and (len(lane) >= DEFAULT_MAX_LANE_LENGTH or rng.random() < 0.35):
            lane.pop(rng.randrange(len(lane)))
        elif pools[role]:
            lane.insert(rng.randrange(len(lane) + 1), rng.choice(pools[role]))
        return tuple(map(tuple, lane_genome)), name
    if not filled:
        return genome, name

    if name == "swap":
        role = rng.choice(filled)
        index = rng.randrange(len(lane_genome[role]))
        pool = pools[role] if role < len(pools) and pools[role] else (lane_genome[role][index],)
        value = lane_genome[role][index]
        # 同一类语义编码里微调一档，同时保留跨技能的大步探索。
        if value in pool and len(pool) > 1 and rng.random() < 0.5:
            at = pool.index(value)
            adjacent = pool[max(0, at - 1):at] + pool[at + 1:at + 2]
            lane_genome[role][index] = rng.choice(adjacent)
        else:
            lane_genome[role][index] = rng.choice(pool)
    elif name == "double":
        first = rng.choice(filled)
        second = rng.choice(filled)
        i = rng.randrange(len(lane_genome[first]))
        j = rng.randrange(len(lane_genome[second]))
        a, b = lane_genome[first][i], lane_genome[second][j]
        if a != b and b in pools[first] and a in pools[second]:
            lane_genome[first][i], lane_genome[second][j] = b, a
        else:
            for role, index in ((first, i), (second, j)):
                value = lane_genome[role][index]
                adjacent = [v for v in (value - 1, value + 1) if v in pools[role]]
                if adjacent:
                    lane_genome[role][index] = rng.choice(adjacent)
    elif name == "chain":
        role = rng.choice(filled)
        lane = lane_genome[role]
        length = min(len(lane), max(1, int(settings.chain_range)))
        length = rng.randint(1, length)
        start = rng.randint(0, max(0, len(lane) - length))
        pool = pools[role] if role < len(pools) and pools[role] else tuple(lane)
        for offset in range(length):
            lane[start + offset] = rng.choice(pool)
    else:  # manual：先等概率挑角色，再挑这一条里的某一刀
        role = rng.randrange(ROLE_COUNT) if len(lane_genome) >= ROLE_COUNT else rng.choice(filled)
        if not lane_genome[role]:
            role = rng.choice(filled)
        lane = lane_genome[role]
        index = rng.randrange(len(lane))
        pool = pools[role] if role < len(pools) and pools[role] else (lane[index],)
        lane[index] = rng.choice(pool)
    return tuple(tuple(lane) for lane in lane_genome), name


def _weight(settings: AnnealSettings, name: str) -> float:
    return {
        "swap": float(settings.swap_chance),
        "double": float(settings.double_swap_chance),
        "chain": float(settings.chain_chance),
        "manual": float(settings.manual_chance),
    }[name]


def temperature_at(settings: AnnealSettings, start: float, progress: float) -> float:
    """几何降温的系数：返回 initial → final 之间按 progress 走的那条曲线。

    这里返回的是「倍数」，真正的温度还要乘上观察到的典型差值（见 ``anneal()``），
    否则在几亿伤害的尺度上温度会大到几乎全是随机游走、或者小到完全不接受上坡步。
    """
    high, low = settings.temperatures()
    if start <= 0:
        start = high
    if progress <= 0:
        return start
    if progress >= 1:
        return max(low, 1e-9)
    ratio = (max(low, 1e-9) / max(start, 1e-9)) ** min(1.0, progress)
    return max(1e-9, start * ratio)


def anneal(
    initial: Genome,
    *,
    settings: AnnealSettings,
    evaluator,
    prefix: Genome,
    pools: Genome,
    baseline: Evaluation,
    battle_time: float,
    rng_seed: int,
    log=print,
    cancelled=lambda: False,
    resume: AnnealState | None = None,
    fixed_lengths: bool = False,
) -> tuple[AnnealState, list[dict]]:
    """跑一次退火；返回 (最终状态, 日志行)。"""
    rng = random.Random(rng_seed)
    history: list[dict] = []
    cache: dict[Genome, Evaluation] = {initial: baseline}
    # 观察到的「变差的幅度」的滑动平均：温度按它缩放，才能既接受得起上坡步、
    # 又不至于在几亿伤害的尺度上变成纯随机游走。
    scale = 0.0
    scale_count = 0
    current = initial
    current_eval = baseline
    score = current_eval.score(battle_time)

    state = resume or AnnealState()
    best = state.best
    if best is None or best.evaluation.score(battle_time) < score:
        best = Candidate(genome=initial, evaluation=current_eval)
        state = replace(state, best=best, best_iteration=0)
    best_score = best.evaluation.score(battle_time)

    start_temperature = settings.initial_temperature
    total = int(settings.iterations)
    begin = int(state.iteration)
    if begin:
        log(f"续跑：从第 {begin}/{total} 次迭代继续（当前最优 {_fmt(best_score)}）")

    # 迭代预算按候选数计算，每批从同一当前解试几个邻居。
    width = max(1, min(4, settings.batch_size // settings.samples))
    iteration = begin
    neighbors = iter(point_neighbors(best.genome, pools))
    while iteration < total:
        if cancelled():
            raise AnnealCancelled()
        count = min(width, total - iteration, settings.report_every - iteration % settings.report_every)
        iteration += count
        progress = iteration / max(1, total)
        ratio = temperature_at(settings, start_temperature, progress)
        temperature = max(1e-9, ratio * (scale if scale_count else 1.0))
        proposals = {}
        duplicates = 0
        if settings.swap_chance > 0 and (count > 1 or iteration % 2 == 0):
            for neighbor in neighbors:
                if neighbor not in cache:
                    proposals[neighbor] = "最优轴单刀微调"
                    break
        for _ in range(count * 4):
            if len(proposals) == count:
                break
            candidate, operator = mutate(current, pools, settings, rng, fixed_lengths=fixed_lengths)
            if candidate in proposals or (not settings.allow_duplicates and candidate in cache):
                duplicates += 1
                continue
            proposals[candidate] = operator
        missing = [genome for genome in proposals if genome not in cache]
        if missing:
            results = evaluator.evaluate([full(prefix, genome) for genome in missing])
            if len(results) != len(missing):
                raise RuntimeError("退火评估返回的候选数量与请求不符")
            cache.update(zip(missing, results, strict=True))
        state = replace(state, iteration=iteration, exact_duplicates=state.exact_duplicates + duplicates,
                        simulations=state.simulations + len(missing) * settings.samples)
        if not proposals:
            continue
        candidate = max(proposals, key=lambda genome: cache[genome].score(battle_time))
        operator, candidate_eval = proposals[candidate], cache[candidate]
        candidate_score = candidate_eval.score(battle_time)
        best_previous = best_score
        delta = candidate_score - score
        if delta < 0:
            step = abs(float(delta))
            scale_count += 1
            scale += (step - scale) / scale_count if scale_count > 1 else step - scale
        accepted = delta >= 0 or rng.random() < math.exp(delta / max(1e-9, temperature))
        if accepted:
            current, current_eval, score = candidate, candidate_eval, candidate_score
            state = replace(state, accepted=state.accepted + 1)
            if candidate_score > best_score:
                best_score = candidate_score
                best = Candidate(genome=candidate, evaluation=candidate_eval)
                neighbors = iter(point_neighbors(best.genome, pools))
                log(
                    f"第 {iteration} 次迭代：{_fmt(candidate_score)}（{operator} 换来的新高，"
                    f"相对基线 {_signed(candidate_eval.mean_damage - baseline.mean_damage)}）"
                )
        else:
            state = replace(state, rejected=state.rejected + 1)
        state = replace(
            state,
            iteration=iteration,
            temperature=temperature,
            best=best,
            best_iteration=iteration if candidate_score > best_previous else state.best_iteration,
        )
        if iteration % max(1, int(settings.report_every)) == 0:
            entry = {
                "iteration": iteration,
                "temperature": temperature,
                "current": current_eval.mean_damage,
                "best": best.evaluation.mean_damage,
                "score": best_score,
                "simulations": state.simulations,
            }
            history.append(entry)
            log(
                f"[{iteration}/{total}] T={_fmt(temperature)} 当前 {current_eval.mean_damage:,} "
                f"最优 {best.evaluation.mean_damage:,} 未接受 {state.rejected}"
            )
    return replace(state, best=best), history


def full(prefix: Genome, genome: Genome) -> Genome:
    """把固定前缀接回基因前面（评估器要完整五条序列）。"""
    return tuple(tuple(prefix[role]) + tuple(genome[role]) for role in range(len(genome)))


def _fmt(value: float) -> str:
    if abs(value) >= 1_000_000:
        return f"{value:,.0f}"
    return f"{value:,.2f}"


# --------------------------------------------------------------------- 任务
@dataclass(frozen=True, slots=True)
class AnnealRound:
    round: int
    seed: int
    iterations: int
    best_iteration: int
    damage: int
    score: float
    simulations: int

    def to_dict(self) -> dict:
        return {
            "round": self.round,
            "seed": self.seed,
            "iterations": self.iterations,
            "best_generation": self.best_iteration,
            "damage": self.damage,
            "score": self.score,
            "simulations": self.simulations,
        }


class AnnealJob:
    """一次退火优化任务：准备 → 退火 → replay 导出。"""

    def __init__(
        self,
        save,
        settings: AnnealSettings,
        *,
        log=print,
        evaluator=None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.save = save
        self.settings = settings
        self.log = log
        self.cancel = cancel or threading.Event()
        self.prepared = None
        self.state: AnnealState | None = None
        self.baseline: Evaluation | None = None
        self.history: list[dict] = []
        self.rounds: list[AnnealRound] = []
        self.export: dict | None = None
        self._resumed = False
        self._ga = GaJob(
            save,
            settings.ga_equivalent(),
            log=log,
            evaluator=evaluator,
            cancel=self.cancel,
        )

    # ------------------------------------------------------------ 转发给 GaJob
    @property
    def evaluator(self):
        return self._ga.evaluator

    @property
    def profile_detected(self) -> bool:
        return bool(getattr(self._ga, "profile_detected", False))

    @property
    def frame_reference(self) -> int | None:
        return getattr(self._ga, "frame_reference", None)

    @property
    def simulations(self) -> int:
        return self.state.simulations if self.state is not None else 0

    @property
    def battles(self) -> int:
        evaluator = getattr(self._ga, "_evaluator", None)
        return getattr(evaluator, "battles", 0) if evaluator is not None else 0

    def cancel_run(self) -> None:
        self.cancel.set()

    @property
    def cancelled_requested(self) -> bool:
        return self.cancel.is_set()

    def close(self) -> None:
        self._ga.close()

    # ------------------------------------------------------------------ 主流程
    def run(self) -> dict:
        started = time.monotonic()
        self._prepare()
        prepared, baseline = self._evaluate_initial(self.prepared)
        self.prepared = prepared
        self.baseline = baseline
        self._ga.prepared = prepared
        self._ga.baseline = baseline
        self._ga._cross_check_baseline()

        best_state: AnnealState | None = None
        planned = max(1, int(self.settings.restarts))
        unlimited = int(self.settings.restarts) == 0
        stale_rounds = 0
        index = 0
        while unlimited or index < planned:
            index += 1
            if self.cancelled_requested:
                raise AnnealCancelled()
            resume = self.state if index == 1 else None
            seed = int(self.settings.seed) + index - 1
            if planned > 1 or unlimited:
                self.log(
                    f"--- 第 {index}{' 轮' if unlimited else f'/{planned} 轮'}：随机种子 {seed}，"
                    f"{'从检查点继续' if resume else '重新起跑'} ---"
                )
            state, history = anneal(
                prepared.initial,
                settings=self.settings,
                evaluator=self.evaluator,
                prefix=prepared.prefix,
                pools=prepared.pools,
                baseline=baseline,
                battle_time=float(self.settings.battle_time),
                rng_seed=seed,
                log=self.log,
                cancelled=lambda: self.cancelled_requested,
                resume=resume,
                fixed_lengths=prepared.roles is not None,
            )
            self.history.extend(history)
            score = state.best.evaluation.score(float(self.settings.battle_time)) if state.best else float("-inf")
            self.rounds.append(
                AnnealRound(
                    round=index,
                    seed=seed,
                    iterations=int(state.iteration),
                    best_iteration=int(state.best_iteration),
                    damage=round(state.best.evaluation.mean_damage) if state.best else 0,
                    score=float(score),
                    simulations=int(state.simulations),
                )
            )
            previous = (
                best_state.best.evaluation.score(float(self.settings.battle_time))
                if best_state is not None and best_state.best is not None
                else float("-inf")
            )
            if best_state is None or score > previous:
                best_state = state
                stale_rounds = 0
            else:
                stale_rounds += 1
            self.state = state
            # restarts = 0 表示不限轮数：连续 3 轮都没刷新最优就停下来
            if unlimited and stale_rounds >= 3:
                self.log(f"连续 {stale_rounds} 轮没有刷新最优，停止重启（共跑 {index} 轮）")
                break
        self.state = best_state or self.state

        best_lanes = full(prepared.prefix, self.state.best.genome) if self.state and self.state.best else prepared.seed_lanes
        replay_eval, frames = self.evaluator.replay(best_lanes, prepared.roles)
        self.log(f"导出前复核：期望伤害 {replay_eval.mean_damage:,} × 战斗帧 {replay_eval.frames}")
        sequence = prepared.roles
        self.export = {
            "lanes": [list(lane) for lane in best_lanes],
            "sequence": list(sequence) if sequence is not None else None,
            "frames": [list(row) for row in frames],
            "damage": round(replay_eval.mean_damage),
            "frames_count": int(replay_eval.frames),
            "result": int(replay_eval.result),
            "ub_counts": list(replay_eval.ub_counts),
        }
        summary = self.summary(time.monotonic() - started)
        self.log(
            f"=== 退火完成：期望伤害 {summary['final']:,}（基线 {summary['baseline']:,}，"
            f"{_signed(summary['final'] - summary['baseline'])}）/ {summary['iterations']} 次迭代 / "
            f"{summary['simulations']} 场 / 用时 {summary['elapsed_s']:.1f}s ==="
        )
        return summary

    def _prepare(self) -> None:
        ga = self._ga
        ga.prepared = None
        ga.settings = self.settings.ga_equivalent()
        prepared = ga._prepare()
        ga.prepared = prepared
        self.prepared = prepared
        self.log("语义起点：" + " / ".join(
            f"角色{index + 1} {len(lane)} 个 UB" for index, lane in enumerate(prepared.seed_lanes)
        ))

    def _evaluate_initial(self, prepared):
        baseline, frames = self.evaluator.replay(prepared.seed_lanes, prepared.roles)
        prepared = replace(prepared, frames=frames)
        self.log(
            f"=== 基线：期望伤害 {baseline.mean_damage:,} × 战斗帧 {baseline.frames} / 结果 {baseline.result} ==="
        )
        return prepared, baseline

    # ------------------------------------------------------------------ 汇总
    def summary(self, elapsed: float) -> dict:
        state = self.state
        best = state.best if state is not None else None
        return {
            "mode": "anneal",
            "excel": str(self.save.path),
            "endpoint": self.settings.endpoint,
            "battle_time": int(self.settings.battle_time),
            "seed": int(self.settings.seed),
            "samples": int(self.settings.samples),
            "anneal": self.settings.to_dict(),
            "profile": self.settings.ga_equivalent().conversion_profile.to_dict(),
            "iterations": int(state.iteration) if state else 0,
            "total_iterations": int(self.settings.iterations),
            "best_iteration": int(state.best_iteration) if state else 0,
            "simulations": int(state.simulations) if state else 0,
            "accepted": int(state.accepted) if state else 0,
            "rejected": int(state.rejected) if state else 0,
            "restarts": int(self.settings.restarts),
            "rounds": [item.to_dict() for item in self.rounds],
            "baseline": round(self.baseline.mean_damage) if self.baseline else 0,
            "baseline_frames": int(self.baseline.frames) if self.baseline else 0,
            "final": round(best.evaluation.mean_damage) if best else 0,
            "best_score": float(best.evaluation.score(float(self.settings.battle_time))) if best else 0.0,
            "history": list(self.history),
            "export": self.export,
            "profile_detected": bool(self.profile_detected),
            "frame_damage": self.frame_reference,
            "cancelled": self.cancelled_requested,
            "battles": self.battles,
            "elapsed_s": round(float(elapsed), 1),
        }

    def state_dict(self) -> dict:
        return {
            "mode": "anneal",
            "expected_damage": self._ga.expected_damage,
            "prepared": None if self.prepared is None else self.prepared.to_dict(),
            "state": None if self.state is None else self.state.to_dict(),
            "history": list(self.history),
            "rounds": [item.to_dict() for item in self.rounds],
            "baseline": None
            if self.baseline is None
            else {
                "damage": int(self.baseline.damage),
                "frames": int(self.baseline.frames),
                "result": int(self.baseline.result),
                "ub_counts": list(self.baseline.ub_counts),
            },
            "export": self.export,
        }

    def apply_state(self, data: dict) -> None:
        from .ga.job import GaPrepared

        self._ga.expected_damage = bool(data.get("expected_damage", False))
        prepared = data.get("prepared")
        if prepared:
            self.prepared = GaPrepared.from_dict(prepared)
            self._ga.prepared = self.prepared
        state = data.get("state")
        if state:
            self.state = AnnealState.from_dict(state)
            self._resumed = True
            best = self.state.best
            if best is not None:
                self.log(
                    f"续跑：从第 {self.state.iteration}/{int(self.settings.iterations)} 次迭代继续"
                    f"（最优 {best.evaluation.damage:,}，第 {self.state.best_iteration} 次产生）"
                )
        self.history = list(data.get("history") or [])
        self.rounds = [
            AnnealRound(
                round=int(item.get("round", 1)),
                seed=int(item.get("seed", self.settings.seed)),
                iterations=int(item.get("iterations", 0)),
                best_iteration=int(item.get("best_generation", 0)),
                damage=int(item.get("damage", 0)),
                score=float(item.get("score", 0.0)),
                simulations=int(item.get("simulations", 0)),
            )
            for item in data.get("rounds") or []
        ]
        baseline = data.get("baseline")
        if baseline:
            self.baseline = Evaluation(
                damage=int(baseline.get("damage", 0)),
                frames=int(baseline.get("frames", 0)),
                result=int(baseline.get("result", 0)),
                ub_counts=tuple(int(value) for value in baseline.get("ub_counts") or ()),
            )
        self.export = data.get("export")


def _signed(value: float) -> str:
    if abs(value) < 0.5:
        return "持平"
    return f"{'+' if value > 0 else '-'}{abs(value):,.0f}"
