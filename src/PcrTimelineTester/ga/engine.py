"""多岛屿遗传算法。

评估器与检查点回调都是同步函数，因为本工具用同步 httpx 直接打 ``/simulate``
（服务器本身就是并行算的）。

规则：锦标赛选择（k=3）、按分数加权的交叉、四种突变（替换 / 交换 / 插入 /
删除）、岛屿迁移、灾变、行为去重（指纹距离）、每代检查点。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from random import Random

from .fingerprint import ActionProgressFingerprint, finite_number, fingerprints_close
from .semantics import (
    DEFAULT_MAX_LANE_LENGTH, Genome, ROLE_COUNT, apply_point_edit, full_genome,
    genome_from_rows, integer, point_edits,
)


@dataclass(frozen=True, slots=True)
class GAConfig:
    population: int = 50
    generations: int = 800
    islands: int = 3
    crossover: float = 0.75
    mutation: float = 0.22
    elite_count: int = 2
    mutation_floor: float = 0.0
    mutation_decay: float = 1.0
    stagnation_limit: int = 0
    migration_interval: int = 10
    migrants_ratio: float = 0.08
    disaster_patience: int = 20
    max_lane_length: int = DEFAULT_MAX_LANE_LENGTH
    fingerprint_distance: float | None = 1.0
    seed: int = 42

    def __post_init__(self) -> None:
        integer(self.population, "population", 1)
        integer(self.generations, "generations")
        integer(self.islands, "islands", 1)
        integer(self.elite_count, "elite_count")
        integer(self.stagnation_limit, "stagnation_limit")
        integer(self.migration_interval, "migration_interval")
        integer(self.disaster_patience, "disaster_patience")
        integer(self.max_lane_length, "max_lane_length", 1)
        integer(self.seed, "seed")
        if self.islands > self.population:
            raise ValueError("islands 不能超过 population")
        for name in ("crossover", "mutation", "migrants_ratio"):
            if finite_number(getattr(self, name), name) > 1:
                raise ValueError(f"{name} 不能超过 1")
        if self.elite_count >= self.population:
            raise ValueError("elite_count 必须小于 population")
        for name in ("mutation_floor", "mutation_decay"):
            if finite_number(getattr(self, name), name) < 0:
                raise ValueError(f"{name} 不能是负数")
        if self.mutation_floor > 1 or self.mutation_decay > 1:
            raise ValueError("mutation_floor / mutation_decay 不能超过 1")
        if self.fingerprint_distance is not None:
            finite_number(self.fingerprint_distance, "fingerprint_distance")

    def to_dict(self) -> dict:
        return {
            "population": self.population,
            "generations": self.generations,
            "islands": self.islands,
            "crossover": self.crossover,
            "mutation": self.mutation,
            "elite_count": self.elite_count,
            "mutation_floor": self.mutation_floor,
            "mutation_decay": self.mutation_decay,
            "stagnation_limit": self.stagnation_limit,
            "migration_interval": self.migration_interval,
            "migrants_ratio": self.migrants_ratio,
            "disaster_patience": self.disaster_patience,
            "max_lane_length": self.max_lane_length,
            "fingerprint_distance": self.fingerprint_distance,
            "seed": self.seed,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "GAConfig":
        data = dict(data or {})
        known = set(cls.__slots__)
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass(frozen=True, slots=True)
class BattleSample:
    seed: int
    damage: int
    frames: int
    result: int

    def __post_init__(self) -> None:
        if type(self.seed) is not int:
            raise ValueError("样本种子必须是整数")
        integer(self.damage, "样本伤害")
        integer(self.frames, "样本 BattleFrames")
        if type(self.result) is not int or self.result not in (0, 1, 2):
            raise ValueError("样本战斗结果必须是胜利、失败或超时")

    def score(self, battle_time: int) -> int:
        remaining = max(0, battle_time * 60 - self.frames) if self.result == 0 else 0
        return self.damage + remaining * 1_000_000_000


@dataclass(frozen=True, slots=True)
class Evaluation:
    damage: int
    frames: int
    result: int
    ub_counts: tuple[int, ...]
    fingerprint: ActionProgressFingerprint | None = None
    samples: tuple[BattleSample, ...] = ()

    def __post_init__(self) -> None:
        integer(self.damage, "伤害")
        integer(self.frames, "BattleFrames")
        integer(self.result, "Result")
        if self.result not in (0, 1, 2):
            raise ValueError("仿真战斗结果必须是胜利、失败或超时")
        if len(self.ub_counts) != ROLE_COUNT:
            raise ValueError("SemanUBNum 必须包含五个角色的计数")
        for count in self.ub_counts:
            integer(count, "SemanUBNum")
        if self.samples:
            if any(not isinstance(sample, BattleSample) for sample in self.samples):
                raise ValueError("评分样本必须是 BattleSample")
            first = self.samples[0]
            if (self.damage, self.frames, self.result) != (first.damage, first.frames, first.result):
                raise ValueError("评分的参考战斗与第一个样本不一致")
            if len({sample.seed for sample in self.samples}) != len(self.samples):
                raise ValueError("评分样本必须使用不同的战斗种子")

    @property
    def mean_damage(self) -> int | float:
        if not self.samples:
            return self.damage
        return sum(sample.damage for sample in self.samples) / len(self.samples)

    def score(self, battle_time: int) -> int | float:
        if self.samples:
            return sum(sample.score(battle_time) for sample in self.samples) / len(self.samples)
        remaining = max(0, battle_time * 60 - self.frames) if self.result == 0 else 0
        return self.damage + remaining * 1_000_000_000

    def to_dict(self) -> dict:
        return {
            "damage": self.damage,
            "frames": self.frames,
            "result": self.result,
            "ub_counts": list(self.ub_counts),
            "fingerprint": None if self.fingerprint is None else self.fingerprint.to_dict(),
            "samples": [
                {"seed": sample.seed, "damage": sample.damage,
                 "frames": sample.frames, "result": sample.result}
                for sample in self.samples
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Evaluation":
        payload = data.get("fingerprint")
        return cls(
            damage=data["damage"], frames=data["frames"], result=data["result"],
            ub_counts=tuple(data["ub_counts"]),
            fingerprint=None if payload is None else ActionProgressFingerprint.from_dict(payload),
            samples=tuple(BattleSample(**sample) for sample in data.get("samples", ())),
        )


@dataclass(frozen=True, slots=True)
class Candidate:
    genome: Genome
    evaluation: Evaluation

    def to_dict(self) -> dict:
        return {"genome": [list(lane) for lane in self.genome], "evaluation": self.evaluation.to_dict()}

    @classmethod
    def from_dict(cls, data: dict) -> "Candidate":
        return cls(genome_from_rows(data["genome"]), Evaluation.from_dict(data["evaluation"]))


@dataclass(frozen=True, slots=True)
class GenerationState:
    generation: int
    islands: tuple[tuple[Candidate, ...], ...]
    best: Candidate
    best_generation: int
    rng_state: tuple
    simulations: int
    exact_duplicates: int
    behavior_duplicates: int
    observed: int
    stopped_early: bool = False

    def to_dict(self) -> dict:
        return {
            "generation": self.generation,
            "islands": [[candidate.to_dict() for candidate in island] for island in self.islands],
            "best": self.best.to_dict(),
            "best_generation": self.best_generation,
            "rng_state": [self.rng_state[0], list(self.rng_state[1]), self.rng_state[2]],
            "simulations": self.simulations,
            "exact_duplicates": self.exact_duplicates,
            "behavior_duplicates": self.behavior_duplicates,
            "observed": self.observed,
            "stopped_early": self.stopped_early,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GenerationState":
        version, numbers, spare = data["rng_state"]
        return cls(
            generation=data["generation"],
            islands=tuple(tuple(Candidate.from_dict(item) for item in island) for island in data["islands"]),
            best=Candidate.from_dict(data["best"]),
            best_generation=data["best_generation"],
            rng_state=(version, tuple(numbers), spare),
            simulations=data["simulations"],
            exact_duplicates=data["exact_duplicates"],
            behavior_duplicates=data["behavior_duplicates"],
            observed=data["observed"],
            stopped_early=bool(data.get("stopped_early", False)),
        )


BatchEvaluator = Callable[[Sequence[Genome]], Sequence[Evaluation]]
CheckpointCallback = Callable[[GenerationState], None]


def parent_representatives(
    island: Sequence[Candidate], threshold: float | None, battle_time: int,
) -> list[Candidate]:
    """按分数排序，并剔除行为上重复（指纹距离过近）的个体。"""
    ranked = sorted(island, key=lambda item: item.evaluation.score(battle_time), reverse=True)
    if threshold is None:
        return ranked
    retained = []
    by_layout = defaultdict(list)
    for candidate in ranked:
        fingerprint = candidate.evaluation.fingerprint
        if fingerprint is not None:
            comparable = by_layout[fingerprint.layout]
            if any(fingerprints_close(fingerprint, prior, threshold) for prior in comparable):
                continue
            comparable.append(fingerprint)
        retained.append(candidate)
    return retained


def optimize(
    *,
    initial: Genome,
    prefix: Genome,
    pools: Genome,
    battle_time: int,
    evaluator: BatchEvaluator,
    config: GAConfig,
    fixed_lengths: bool = False,
    resume: GenerationState | None = None,
    on_checkpoint: CheckpointCallback | None = None,
) -> GenerationState:
    """评估第 0 代，然后进化到 ``config.generations`` 代（含）。"""
    initial, prefix, pools = map(genome_from_rows, (initial, prefix, pools))
    integer(battle_time, "战斗秒数", 1)
    if any(not pool for pool in pools):
        raise ValueError("每条UB序列需要至少一个候选基因")
    if any(len(lane) > config.max_lane_length for lane in initial):
        raise ValueError("种子UB序列的可变部分超过 max_lane_length")
    rng = Random(config.seed)
    base, remainder = divmod(config.population, config.islands)
    sizes = tuple(base + (i < remainder) for i in range(config.islands))
    state = resume
    if state is not None:
        if (
            not 0 <= state.best_generation <= state.generation <= config.generations
            or tuple(map(len, state.islands)) != sizes
        ):
            raise ValueError("检查点的代数或岛屿规模与配置不符")
        rng.setstate(state.rng_state)

    def new_genome() -> Genome:
        length = min(2, config.max_lane_length)
        lengths = tuple(map(len, initial)) if fixed_lengths else (length,) * ROLE_COUNT
        return tuple(tuple(rng.choice(pool) for _ in range(n)) for n, pool in zip(lengths, pools))

    def rank(candidate: Candidate) -> int | float:
        return candidate.evaluation.score(battle_time)

    def tournament(candidates: Sequence[Candidate]) -> Candidate:
        return max(rng.choices(candidates, k=3), key=rank)

    def cross(first: Candidate, second: Candidate) -> Genome:
        score_a, score_b = max(1, rank(first)), max(1, rank(second))
        chance = score_a / (score_a + score_b)
        children = []
        for a, b in zip(first.genome, second.genome, strict=True):
            donor, other = (a, b) if rng.random() < chance else (b, a)
            if rng.random() < 0.5:
                children.append(donor)
            else:
                split = rng.randrange(min(len(a), len(b)) + 1)
                children.append(other[:split] + donor[split:])
        return tuple(children)

    def mutate(genome: Genome, chance: float) -> Genome:
        result = []
        for values, pool in zip(genome, pools, strict=True):
            lane = list(values)
            if rng.random() < chance:
                operation = rng.randrange(2 if fixed_lengths else 4)
                if not lane and fixed_lengths:
                    result.append(())
                    continue
                if not lane:
                    lane.append(rng.choice(pool))
                elif operation == 0:
                    lane[rng.randrange(len(lane))] = rng.choice(pool)
                elif operation == 1 and len(lane) >= 2:
                    first, second = rng.sample(range(len(lane)), 2)
                    lane[first], lane[second] = lane[second], lane[first]
                elif operation == 2 and len(lane) < config.max_lane_length:
                    lane.insert(rng.randrange(len(lane) + 1), rng.choice(pool))
                elif operation == 3:
                    lane.pop(rng.randrange(len(lane)))
            result.append(tuple(lane))
        return tuple(result)

    first_generation = state.generation + 1 if state is not None else 0
    local_center = None
    local = []
    for generation in range(first_generation, config.generations + 1):
        behavior_duplicates = 0
        if state is None:
            # 各岛都从原轴附近和随机短轴两侧起步，保留成熟轴后半场的配合。
            batches = [
                [mutate(initial, 0.4) if i % 2 == 0 else new_genome() for i in range(size)]
                for size in sizes
            ]
            batches[0][0] = initial
            if config.population > 1:
                zero_island = 1 if config.islands > 1 else 0
                zero_index = 0 if zero_island else 1
                batches[zero_island][zero_index] = (
                    tuple((0,) * len(lane) for lane in initial) if fixed_lengths else ((0,),) * ROLE_COUNT
                )
        else:
            parents = [
                parent_representatives(island, config.fingerprint_distance, battle_time)
                for island in state.islands
            ]
            behavior_duplicates = config.population - sum(map(len, parents))
            batches = []
            migrate = (
                config.islands > 1 and config.migration_interval > 0
                and config.migrants_ratio > 0 and generation % config.migration_interval == 0
            )
            chance = min(1.0, max(config.mutation_floor, config.mutation * (config.mutation_decay ** generation)))
            stale = generation - state.best_generation - 1
            disaster = config.disaster_patience > 0 and stale > 0 and stale % config.disaster_patience == 0
            if state.best.genome != local_center:
                local_center = state.best.genome
                local = list(point_edits(local_center, pools))
            for island, (size, representatives) in enumerate(zip(sizes, parents, strict=True)):
                children = [
                    item.genome for item in representatives[: min(config.elite_count, size)]
                ]
                if migrate:
                    donors = parents[island - 1]
                    quota = min(size - len(children), max(1, round(size * config.migrants_ratio)))
                    children.extend(
                        mutate(donor.genome, min(1.0, chance * 2))
                        for donor in donors[:quota]
                    )
                if disaster:
                    children.extend(new_genome() for _ in range(min(size - len(children), max(1, size // 3))))
                if chance and local:
                    # 每代留一部分名额系统检查冠军的一刀微调，其余名额继续交叉和变异。
                    slots = min(size - len(children), max(1, size // 3))
                    offset = (generation - state.best_generation - 1) * sum(max(1, n // 3) for n in sizes)
                    offset += sum(max(1, n // 3) for n in sizes[:island])
                    children.extend(apply_point_edit(local_center, local[(offset + i) % len(local)]) for i in range(slots))
                while len(children) < size:
                    first = tournament(representatives)
                    genome = first.genome
                    if rng.random() < config.crossover:
                        genome = cross(first, tournament(representatives))
                    children.append(mutate(genome, chance))
                batches.append(children)

        unique = dict.fromkeys(genome for island in batches for genome in island)
        # 只沿用上一代和历史冠军，空间随种群大小封顶，检查点也包含相同的信息。
        facts = {}
        if state is not None:
            facts = {item.genome: item.evaluation for island in state.islands for item in island}
            facts[state.best.genome] = state.best.evaluation
        missing = [genome for genome in unique if genome not in facts]
        evaluation = list(evaluator([full_genome(prefix, genome) for genome in missing])) if missing else []
        if len(evaluation) != len(missing):
            raise ValueError("评估器返回数量与请求不匹配")
        facts.update(zip(missing, evaluation, strict=True))
        evaluated = tuple(
            tuple(Candidate(genome, facts[genome]) for genome in island) for island in batches
        )
        winner = max((item for island in evaluated for item in island), key=rank)
        improved = state is None or rank(winner) > rank(state.best)
        state = GenerationState(
            generation=generation, islands=evaluated,
            best=winner if improved else state.best,
            best_generation=generation if improved else state.best_generation,
            rng_state=rng.getstate(),
            simulations=(state.simulations if state else 0) + sum(
                len(item.samples) or 1 for item in evaluation
            ),
            exact_duplicates=(state.exact_duplicates if state else 0) + config.population - len(missing),
            behavior_duplicates=(state.behavior_duplicates if state else 0) + behavior_duplicates,
            observed=sum(facts[genome].fingerprint is not None for genome in unique),
        )
        if config.stagnation_limit > 0 and state.generation - state.best_generation >= config.stagnation_limit:
            state = replace(state, stopped_early=True)
            if on_checkpoint is not None:
                on_checkpoint(state)
            break
        if on_checkpoint is not None:
            on_checkpoint(state)
    assert state is not None
    return state
