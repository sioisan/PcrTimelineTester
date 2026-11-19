"""语义 GA 模式的配置。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .engine import GAConfig
from .semantics import (
    DEFAULT_MAX_LANE_LENGTH,
    EMPTY_GENOME,
    ROLE_COUNT,
    Genome,
    SemanSequenceType,
    SemanSkillType,
    SemanUBType,
    SemanticConversionProfile,
    genome_from_rows,
    integer,
)

DEFAULT_ENDPOINT = "http://127.0.0.1:8080/simulate"


def _zero_counts(value: object) -> tuple[int, ...]:
    if value is None:
        return (0,) * ROLE_COUNT
    if not isinstance(value, (tuple, list)) or len(value) != ROLE_COUNT:
        raise ValueError("固定UB数必须是五个整数")
    return tuple(integer(item, "固定UB数") for item in value)


@dataclass(frozen=True, slots=True)
class GaSettings:
    """一次语义 GA 优化需要的全部参数（可 JSON 往返，用于续跑）。"""

    endpoint: str = DEFAULT_ENDPOINT
    battle_time: int = 90
    seed: int = 666
    samples: int = 1
    batch_size: int = 32
    timeout: float = 180.0
    # GA 本体
    population: int = 48
    generations: int = 30
    islands: int = 3
    crossover: float = 0.75
    mutation: float = 0.22
    elite_count: int = 2
    mutation_floor: float = 0.0
    mutation_decay: float = 1.0
    stagnation_limit: int = 0
    restarts: int = 1
    migration_interval: int = 10
    migrants_ratio: float = 0.08
    disaster_patience: int = 20
    max_lane_length: int = DEFAULT_MAX_LANE_LENGTH
    fingerprint_distance: float | None = 1.0
    ga_seed: int = 42
    # 语义转换
    ub_type: int = SemanUBType.SERIES_1
    skill_type: int = SemanSkillType.SERIES_20
    sequence_type: int = SemanSequenceType.LANES
    # 搜索池
    fixed_ub_counts: tuple[int, ...] = (0,) * ROLE_COUNT
    extra_genes: Genome = EMPTY_GENOME
    legacy_ub: bool | None = None
    own_skill: bool = True
    other_skill: bool = False
    delay: int = 3
    verify: bool = True

    def __post_init__(self) -> None:
        integer(self.battle_time, "战斗秒数", 1)
        if self.battle_time > 90:
            raise ValueError("战斗秒数最多 90 秒")
        integer(self.seed, "种子")
        integer(self.samples, "采样种子数", 1)
        if self.samples > 100:
            raise ValueError("采样种子数最多 100")
        integer(self.batch_size, "单次场次", 1)
        if not self.timeout > 0:
            raise ValueError("超时秒数必须大于 0")
        object.__setattr__(self, "fixed_ub_counts", _zero_counts(self.fixed_ub_counts))
        object.__setattr__(self, "extra_genes", genome_from_rows(list(self.extra_genes)))
        integer(self.delay, "退避帧数", 1)
        integer(self.restarts, "重启轮数", 1)
        if self.restarts > 20:
            raise ValueError("重启轮数最多 20")
        # GAConfig 自己会做范围校验，这里先构造一次把错误提前抛出。
        self.ga_config()

    # ---------------------------------------------------------------- 派生视图
    @property
    def conversion_profile(self) -> SemanticConversionProfile:
        return SemanticConversionProfile(
            ub_type=self.ub_type, skill_type=self.skill_type, sequence_type=self.sequence_type,
        )

    def ga_config(self) -> GAConfig:
        return GAConfig(
            population=self.population,
            generations=self.generations,
            islands=self.islands,
            crossover=self.crossover,
            mutation=self.mutation,
            elite_count=self.elite_count,
            mutation_floor=self.mutation_floor,
            mutation_decay=self.mutation_decay,
            stagnation_limit=self.stagnation_limit,
            migration_interval=self.migration_interval,
            migrants_ratio=self.migrants_ratio,
            disaster_patience=self.disaster_patience,
            max_lane_length=self.max_lane_length,
            fingerprint_distance=self.fingerprint_distance,
            seed=self.ga_seed,
        )

    def seeds(self) -> tuple[int, ...]:
        return (self.seed,) + tuple(
            (abs(self.seed) + index) % (2**31 - 1) for index in range(1, self.samples)
        )

    def with_changes(self, **changes) -> "GaSettings":
        return replace(self, **changes)

    # ---------------------------------------------------------------- 序列化
    def to_dict(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "battle_time": self.battle_time,
            "seed": self.seed,
            "samples": self.samples,
            "batch_size": self.batch_size,
            "timeout": self.timeout,
            "population": self.population,
            "generations": self.generations,
            "islands": self.islands,
            "crossover": self.crossover,
            "mutation": self.mutation,
            "elite_count": self.elite_count,
            "mutation_floor": self.mutation_floor,
            "mutation_decay": self.mutation_decay,
            "stagnation_limit": self.stagnation_limit,
            "restarts": self.restarts,
            "migration_interval": self.migration_interval,
            "migrants_ratio": self.migrants_ratio,
            "disaster_patience": self.disaster_patience,
            "max_lane_length": self.max_lane_length,
            "fingerprint_distance": self.fingerprint_distance,
            "ga_seed": self.ga_seed,
            **self.conversion_profile.to_dict(),
            "fixed_ub_counts": list(self.fixed_ub_counts),
            "extra_genes": [list(lane) for lane in self.extra_genes],
            "legacy_ub": self.legacy_ub,
            "own_skill": self.own_skill,
            "other_skill": self.other_skill,
            "delay": self.delay,
            "verify": self.verify,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "GaSettings":
        data = dict(data or {})
        known = {field_name for field_name in cls.__slots__} | {"ub_type", "skill_type", "sequence_type"}
        unknown = set(data) - known
        values = {key: value for key, value in data.items() if key in known}
        if "extra_genes" in values:
            values["extra_genes"] = genome_from_rows(list(values["extra_genes"]))
        settings = cls(**values)
        if unknown:
            raise ValueError(f"配置里有不认识的字段：{'、'.join(sorted(unknown))}")
        return settings
