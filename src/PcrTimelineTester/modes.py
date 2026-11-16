"""优化模式的接口层：每个模式都是一个可注册的模式。

本工具现在有两种模式（``frame`` 逐帧搜索、``ga`` 语义 UB 遗传算法），前端
（命令行 / 窗口）都只认这里的 :class:`ModeSpec`，不硬编码模式分支。要接第三种
优化方式，只需要在 :data:`_REGISTRY` 里登记一个新的 :class:`ModeSpec`：

* ``params`` 声明它有哪些可调参数（名字、中文标签、悬浮说明、范围/选项），
  命令行 ``--set 名字=值`` 和窗口里的通用参数区都是照着它生成的；
* ``build(values, save)`` 把扁平的值字典变成该模式自己的设置对象；
* ``session(...)`` 返回一个带 ``run()/close()/cancel()`` 的会话；
* ``plan(save, settings)`` 给出写进日志的那几行摘要。

这样新增模式不需要改 ``cli.py`` / ``gui.py`` 的流程代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .client import DEFAULT_CALC_STATS
from .ga.semantics import SemanSequenceType, SemanSkillType, SemanUBType
from .ga.settings import GaSettings
from .job import JobSettings
from .savefile import SaveFile, read_save
from .search import SearchSettings, ThresholdMode
from .session import GaSession, TuneSession

FRAME = "frame"
GA = "ga"
ANNEAL = "anneal"


# --------------------------------------------------------------------------- 参数
@dataclass(frozen=True, slots=True)
class Param:
    """一个可调参数：前端照着它生成输入框、命令行开关和悬浮说明。"""

    name: str
    label: str
    tip: str
    kind: str = "int"  # int | float | bool | choice
    default: Any = 0
    low: float | None = None
    high: float | None = None
    choices: tuple[tuple[Any, str], ...] = ()

    def coerce(self, value: Any) -> Any:
        """把命令行 / 输入框里的东西变成设置对象要的类型。"""
        if self.kind == "ids":
            if isinstance(value, (list, tuple, set)):
                items = [int(item) for item in value]
            elif isinstance(value, str):
                cleaned = value.replace("，", ",").replace("、", ",").replace(" ", ",")
                parts = [item for item in cleaned.split(",") if item]
                try:
                    items = [int(item) for item in parts]
                except ValueError:
                    raise ValueError(f"{self.label} 只能是逗号分隔的角色 ID（现在是 {value!r}）") from None
            else:
                raise ValueError(f"{self.label} 只能是逗号分隔的角色 ID（现在是 {value!r}）")
            return tuple(dict.fromkeys(items))
        if self.kind == "groups":
            from .roster import GroupSpec

            if isinstance(value, str):
                items = []
                for chunk in value.replace("；", ";").split(";"):
                    chunk = chunk.strip()
                    if not chunk:
                        continue
                    head, _, members = chunk.partition("=")
                    name, _, minimum = head.partition(":")
                    items.append(
                        GroupSpec(
                            name=name.strip(),
                            members=tuple(int(part) for part in members.replace("，", ",").replace("、", ",").split(",") if part.strip()),
                            minimum=int(minimum or 0),
                        )
                    )
                return tuple(items)
            if isinstance(value, (list, tuple)):
                return tuple(item if isinstance(item, GroupSpec) else GroupSpec.from_dict(item) for item in value)
            raise ValueError(f"{self.label} 的写法是「组名:最少人数=角色ID,角色ID;…」（现在是 {value!r}）")
        if self.kind == "bool":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in ("1", "true", "yes", "on", "是", "开"):
                return True
            if text in ("0", "false", "no", "off", "否", "关", ""):
                return False
            raise ValueError(f"{self.label} 只能是 开/关（现在是 {value!r}）")
        if self.kind == "choice":
            # 既要吃命令行里的字符串（值或中文标签），也要吃设置对象里的枚举/整数。
            candidate = getattr(value, "value", value)
            if isinstance(candidate, str):
                text = candidate.strip()
                for wire, label in self.choices:
                    if text in (str(wire), label):
                        return wire
            else:
                for wire, _label in self.choices:
                    if candidate == wire:
                        return wire
            names = "、".join(label for _wire, label in self.choices)
            raise ValueError(f"{self.label} 只能是 {names} 之一（现在是 {value!r}）")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{self.label} 必须是数值（现在是 {value!r}）") from None
        if self.kind == "int":
            if not number.is_integer():
                raise ValueError(f"{self.label} 必须是整数（现在是 {value!r}）")
            number = int(number)
        if self.low is not None and number < self.low:
            raise ValueError(f"{self.label} 不能小于 {self.low:g}（现在是 {number:g}）")
        if self.high is not None and number > self.high:
            raise ValueError(f"{self.label} 不能大于 {self.high:g}（现在是 {number:g}）")
        return number

    def text(self, value: Any) -> str:
        """设置对象里的值 → 显示/保存用的字符串。"""
        if self.kind == "ids":
            return ",".join(str(int(item)) for item in (value or ()))
        if self.kind == "groups":
            parts = []
            for group in value or ():
                members = ",".join(str(int(unit)) for unit in group.members)
                parts.append(f"{group.name}:{int(group.minimum)}={members}")
            return ";".join(parts)
        if self.kind == "bool":
            return "开" if value else "关"
        if self.kind == "choice":
            for wire, label in self.choices:
                if wire == value:
                    return label
            return str(value)
        if self.kind == "float":
            return f"{float(value):g}"
        return str(value)


@dataclass(frozen=True, slots=True)
class FrameSettings:
    """逐帧搜索模式的设置：搜索参数 + 作业参数。"""

    job: JobSettings
    search: SearchSettings


# --------------------------------------------------------------------------- 模式
@dataclass(frozen=True, slots=True)
class ModeSpec:
    key: str
    label: str
    description: str
    needs_frames: bool = True
    workbook_name: str = "调帧结果.xlsx"
    params: tuple[Param, ...] = ()
    build: Callable[[dict[str, Any], SaveFile], Any] = field(default=lambda values, save: values)
    session: Callable[..., Any] = field(default=None)  # type: ignore[assignment]
    plan: Callable[[SaveFile, Any], list[str]] = field(default=lambda save, settings: [])

    # ---- 便捷方法（前端只调这些）
    def read_save(self, path: Path, *, log: Callable[[str], None] | None = None) -> SaveFile:
        return read_save(path, require_frames=self.needs_frames, log=log)

    def defaults(self) -> dict[str, Any]:
        return {param.name: param.default for param in self.params}

    def values(self, settings: Any) -> dict[str, Any]:
        """设置对象 → 扁平值字典（写进 config.json 用）。"""
        data = settings.to_dict() if hasattr(settings, "to_dict") else dict(settings)
        return {param.name: data[param.name] for param in self.params if param.name in data}

    def from_values(self, values: dict[str, Any], save: SaveFile) -> Any:
        """扁平值字典 → 设置对象；缺的取默认值，多出来的名字直接报错。"""
        known = {param.name for param in self.params}
        unknown = sorted(set(values) - known)
        if unknown:
            raise ValueError(f"{self.label} 没有这些参数：{'、'.join(unknown)}")
        merged = self.defaults()
        merged.update({name: value for name, value in values.items() if value is not None})
        coerced = {param.name: param.coerce(merged[param.name]) for param in self.params}
        return self.build(coerced, save)

    def open_session(self, save: SaveFile, settings: Any, run_dir: Path, **kwargs: Any):
        if self.session is None:
            raise ValueError(f"{self.label} 还没有实现会话")
        return self.session(save, settings, run_dir, **kwargs)


# --------------------------------------------------------------------------- frame
_THRESHOLD_CHOICES = tuple((mode.value, mode.label) for mode in ThresholdMode)


def _frame_build(values: dict[str, Any], save: SaveFile) -> FrameSettings:
    job = JobSettings(
        battle_time=values["battle_time"],
        seed=values["seed"],
        passes=values["passes"],
        samples=values["samples"],
        batch_size=values["batch_size"],
        timeout=float(values["timeout"]),
        calc_stats=values["calc_stats"],
        include_boss=values["include_boss"],
        verify=values["verify"],
    )
    search = SearchSettings(
        step=values["step"],
        max_offset=values["max_offset"],
        outer_probes=values["outer_probes"],
        threshold_mode=ThresholdMode(values["threshold_mode"]),
        threshold_value=float(values["threshold_value"]),
        peak_tolerance=float(values["peak_tolerance"]),
        chunk=values["chunk"],
    )
    return FrameSettings(job, search)


def _frame_session(save: SaveFile, settings: FrameSettings, run_dir: Path, **kwargs: Any) -> TuneSession:
    return TuneSession(save, settings.job, settings.search, run_dir, **kwargs)


def _frame_plan(save: SaveFile, settings: FrameSettings) -> list[str]:
    return [
        f"待测项 {len(_frame_targets(save, settings.job))} 项",
        settings.search.describe(),
    ]


def _frame_targets(save: SaveFile, job: JobSettings):
    from .frames import build_targets

    return build_targets(
        save.frames, save.unit_ids,
        names=save.names, boss_name=save.boss_name,
        include_boss=job.include_boss, items=job.items,
    )


FRAME_PARAMS: tuple[Param, ...] = (
    Param("battle_time", "战斗秒数", "模拟战斗时长（1~90 秒）。", "int", 90, 1, 90),
    Param("seed", "战斗种子", "固定随机种子；用轴里记录的种子才能和游戏逐位一致。",
          "int", 666, -(2 ** 31), 2 ** 31 - 1),
    Param("passes", "循环轮数", "整套流程重复跑几轮，每轮都用上一轮的最优帧当起点。", "int", 1, 1, 20),
    Param("samples", "采样种子数", "每个帧位打几场（不同种子取平均），1 最忠实也最快。", "int", 1, 1, 100),
    Param("batch_size", "单次场次", "一次接口请求里塞几场战斗。", "int", 16, 1, 512),
    Param("timeout", "超时秒", "单次接口请求的超时时间。", "float", 180.0, 1, 3600),
    Param("calc_stats", "CalcStats", "统计位掩码，默认 3；读不到伤害会自动改用 511 重试。", "int",
          DEFAULT_CALC_STATS, 0, 511),
    Param("include_boss", "含 Boss 行", "把第六条 Boss 帧行也当作可调项。", "bool", False),
    Param("verify", "跑完复核", "调完后用调帧后的轴再打一场，把最终伤害写进报告。", "bool", True),
    Param("step", "步长", "每次挪动多少帧。", "int", 5, 1, 60),
    Param("max_offset", "偏移±", "以原帧为中心，上下最多探多少帧。", "int", 100, 1, 2000),
    Param("outer_probes", "外探", "超阈值后继续往外探几帧。", "int", 3, 0, 50),
    Param("threshold_mode", "阈值口径", "允许掉幅的口径：绝对伤害 / 百分比 / 不允许掉伤害。",
          "choice", ThresholdMode.ABSOLUTE.value, None, None, _THRESHOLD_CHOICES),
    Param("threshold_value", "阈值", "允许伤害下降多少还算可接受（口径由 threshold_mode 决定）。",
          "float", 500_000.0, 0, 10 ** 9),
    Param("peak_tolerance", "峰值容差", "差值小于它就当同一档伤害，取更早的帧。", "float", 0.5, 0, 10 ** 6),
    Param("chunk", "每批帧数", "一次请求里预取多少帧（只影响速度）。", "int", 8, 1, 64),
)

_UB_CHOICES = tuple((value.value, f"{value.name}") for value in SemanUBType)
_SKILL_CHOICES = tuple((value.value, value.name) for value in SemanSkillType)
_SEQUENCE_CHOICES = tuple((value.value, value.name) for value in SemanSequenceType)

GA_PARAMS: tuple[Param, ...] = (
    Param("battle_time", "战斗秒数", "模拟战斗时长（1~90 秒）。", "int", 90, 1, 90),
    Param("seed", "战斗种子", "固定随机种子。", "int", 666, -(2 ** 31), 2 ** 31 - 1),
    Param("samples", "采样种子数", "每个基因评估几个种子取平均。", "int", 1, 1, 100),
    Param("batch_size", "单次场次", "一次接口请求里塞几场战斗。", "int", 32, 1, 512),
    Param("timeout", "超时秒", "单次接口请求的超时时间。", "float", 180.0, 1, 3600),
    Param("population", "种群", "每一代保留多少个候选 UB 序列。", "int", 48, 2, 512),
    Param("generations", "代数", "总共跑多少代。", "int", 30, 1, 100000),
    Param("islands", "岛屿", "分成几个岛各自进化、定期互相迁入。", "int", 3, 1, 64),
    Param("crossover", "交叉率", "两个父代按比例混合的强度（0~1）。", "float", 0.75, 0, 1),
    Param("mutation", "突变率", "每个基因被随机改动的概率（0~1）。", "float", 0.22, 0, 1),
    Param("elite_count", "精英保留", "每一代原样留到下一代的最优个体数（0 = 不保留）。", "int", 2, 0, 64),
    Param("mutation_decay", "突变衰减", "每代把突变率乘上这个系数（1 = 不衰减）。", "float", 1.0, 0, 1),
    Param("mutation_floor", "突变下限", "衰减到这个值就不再降（0~1）。", "float", 0.0, 0, 1),
    Param("stagnation_limit", "停滞停止", "连着多少代没有进步就提前结束（0 = 一直跑到代数上限）。",
          "int", 0, 0, 100000),
    Param("restarts", "重启轮数", "整套 GA 换不同随机种子重跑几轮，取最好的一轮。", "int", 1, 1, 20),
    Param("migration_interval", "迁移间隔", "每隔几代迁一次个体。", "int", 10, 1, 10000),
    Param("migrants_ratio", "迁移比例", "每次迁移换掉本岛多少比例的个体（0~1）。", "float", 0.08, 0, 1),
    Param("disaster_patience", "灾变间隔", "连着多少代没进步就注入新个体。", "int", 20, 1, 100000),
    Param("max_lane_length", "单行上限", "一条 UB 序列最多多少基因。", "int", 96, 1, 4096),
    Param("fingerprint_distance", "指纹容差", "小于它就算行为重复，直接复用结果；0 = 不判重。",
          "float", 1.0, 0, 10 ** 6),
    Param("ga_seed", "GA 种子", "遗传算法自己的随机种子。", "int", 42, 0, 2 ** 31 - 1),
    Param("delay", "退避帧数", "基因池额外加入 1..N 帧的候选值。", "int", 3, 1, 600),
    Param("ub_type", "UB 类型", "语义 UB 的分段方式。", "choice", SemanUBType.SERIES_1.value,
          None, None, _UB_CHOICES),
    Param("skill_type", "技能类型", "语义技能族，决定基因池里放哪些技能段。", "choice",
          SemanSkillType.SERIES_20.value, None, None, _SKILL_CHOICES),
    Param("sequence_type", "序列类型", "只调五条 UB 序列，还是一起调角色序列。", "choice",
          SemanSequenceType.LANES.value, None, None, _SEQUENCE_CHOICES),
    Param("legacy_ub", "旧版 UB 候选", "基因池里加入旧版 UB 的编码。", "bool", False),
    Param("own_skill", "本角色技能", "基因池包含本角色自己的技能段。", "bool", True),
    Param("other_skill", "其他角色技能", "基因池也放其他角色的技能段。", "bool", False),
    Param("verify", "跑完复核", "导出前用最优序列再打一场确认伤害。", "bool", True),
)


def _ga_build(values: dict[str, Any], save: SaveFile) -> GaSettings:
    return GaSettings(
        battle_time=values["battle_time"],
        seed=values["seed"],
        samples=values["samples"],
        batch_size=values["batch_size"],
        timeout=float(values["timeout"]),
        population=values["population"],
        generations=values["generations"],
        islands=values["islands"],
        crossover=float(values["crossover"]),
        mutation=float(values["mutation"]),
        elite_count=values["elite_count"],
        mutation_decay=float(values["mutation_decay"]),
        mutation_floor=float(values["mutation_floor"]),
        stagnation_limit=values["stagnation_limit"],
        restarts=values["restarts"],
        migration_interval=values["migration_interval"],
        migrants_ratio=float(values["migrants_ratio"]),
        disaster_patience=values["disaster_patience"],
        max_lane_length=values["max_lane_length"],
        fingerprint_distance=float(values["fingerprint_distance"]),
        ga_seed=values["ga_seed"],
        delay=values["delay"],
        ub_type=SemanUBType(values["ub_type"]),
        skill_type=SemanSkillType(values["skill_type"]),
        sequence_type=SemanSequenceType(values["sequence_type"]),
        legacy_ub=bool(values["legacy_ub"]),
        own_skill=bool(values["own_skill"]),
        other_skill=bool(values["other_skill"]),
        verify=bool(values["verify"]),
    )


def _ga_session(save: SaveFile, settings: GaSettings, run_dir: Path, **kwargs: Any) -> GaSession:
    return GaSession(save, settings, run_dir, **kwargs)


def _ga_plan(save: SaveFile, settings: GaSettings) -> list[str]:
    return [
        f"种群 {settings.population} / 代数 {settings.generations} / 岛屿 {settings.islands}",
        f"精英 {settings.elite_count} / 突变 {settings.mutation:g}×{settings.mutation_decay:g}^代"
        f"（下限 {settings.mutation_floor:g}）/ 停滞停止 {settings.stagnation_limit or '关'}"
        f" / 重启 {settings.restarts} 轮",
        f"语义口径 {settings.conversion_profile}",
    ]


# --------------------------------------------------------------------------- 退火模式
ANNEAL_PARAMS: tuple[Param, ...] = (
    Param("battle_time", "战斗秒数", "模拟战斗时长（1~90 秒）。", "int", 90, 1, 90),
    Param("seed", "战斗种子", "固定随机种子。", "int", 666, -(2 ** 31), 2 ** 31 - 1),
    Param("samples", "采样种子数", "每个候选评估几个种子取平均。", "int", 1, 1, 100),
    Param("batch_size", "单次场次", "一次接口请求里塞几场战斗。", "int", 16, 1, 512),
    Param("timeout", "超时秒", "单次接口请求的超时时间。", "float", 180.0, 1, 3600),
    Param("iterations", "迭代次数", "总共试多少个邻居（每个邻居打一场）。", "int", 400, 1, 1000000),
    Param("initial_temperature", "初始温度", "按基线伤害的比例算；越大越敢接受变差的一步（0.02 = 2%）。",
          "float", 0.02, 0.00001, 1.0),
    Param("final_temperature", "最低温度", "降到这个比例就基本不再接受变差的步。", "float", 0.0002, 0.0, 1.0),
    Param("chain_range", "链长上限", "链式替换一次最多动几刀。", "int", 4, 1, 96),
    Param(
        "moves",
        "每步改动",
        "一步里连做几次小改动（1~3）；1 只改一个基因，跳不出局部最优时调到 2 或 3。",
        "int",
        1,
        1,
        3,
    ),
    Param("swap_chance", "单点替换", "随机挑一刀换一个基因的概率。", "float", 0.55, 0, 1),
    Param("double_swap_chance", "双点互换", "随机挑两刀互换位置的概率。", "float", 0.15, 0, 1),
    Param("chain_chance", "链式替换", "连着改一串基因的概率。", "float", 0.2, 0, 1),
    Param("manual_chance", "手动指定", "先等概率挑角色、再改这一条里的一刀。", "float", 0.1, 0, 1),
    Param("allow_duplicates", "允许重复状态", "关着的时候算过的基因直接复用分数，不重复请求。", "bool", False),
    Param(
        "restarts",
        "重启轮数",
        "换随机种子重跑几轮，取最好的一轮；0 = 不限，连续 3 轮都没刷新最优才停。",
        "int",
        1,
        0,
        10000,
    ),
    Param("report_every", "日志间隔", "每多少次迭代打一行进度。", "int", 25, 1, 10000),
    Param("fingerprint_distance", "指纹容差", "小于它就算行为重复（0 = 不判重）。", "float", 1.0, 0, 10 ** 6),
    Param("delay", "退避帧数", "基因池额外加入 1..N 帧的候选值。", "int", 3, 1, 600),
    Param("ub_type", "UB 类型", "语义 UB 的分段方式。", "choice", SemanUBType.SERIES_1.value,
          None, None, _UB_CHOICES),
    Param("skill_type", "技能类型", "语义技能族，决定基因池里放哪些技能段。", "choice",
          SemanSkillType.SERIES_20.value, None, None, _SKILL_CHOICES),
    Param("sequence_type", "序列类型", "只调五条 UB 序列，还是一起调角色序列。", "choice",
          SemanSequenceType.LANES.value, None, None, _SEQUENCE_CHOICES),
    Param("legacy_ub", "旧版 UB 候选", "基因池里加入旧版 UB 的编码。", "bool", False),
    Param("own_skill", "本角色技能", "基因池包含本角色自己的技能段。", "bool", True),
    Param("other_skill", "其他角色技能", "基因池也放其他角色的技能段。", "bool", False),
    Param("verify", "跑完复核", "导出前用最优序列再打一场确认伤害。", "bool", True),
)


def _anneal_build(values: dict[str, Any], save: SaveFile):
    from .anneal import AnnealSettings

    return AnnealSettings(
        battle_time=values["battle_time"],
        seed=values["seed"],
        samples=values["samples"],
        batch_size=values["batch_size"],
        timeout=float(values["timeout"]),
        iterations=values["iterations"],
        initial_temperature=float(values["initial_temperature"]),
        final_temperature=float(values["final_temperature"]),
        chain_range=values["chain_range"],
        moves=values["moves"],
        swap_chance=float(values["swap_chance"]),
        double_swap_chance=float(values["double_swap_chance"]),
        chain_chance=float(values["chain_chance"]),
        manual_chance=float(values["manual_chance"]),
        allow_duplicates=bool(values["allow_duplicates"]),
        restarts=values["restarts"],
        report_every=values["report_every"],
        fingerprint_distance=float(values["fingerprint_distance"]),
        delay=values["delay"],
        ub_type=SemanUBType(values["ub_type"]),
        skill_type=SemanSkillType(values["skill_type"]),
        sequence_type=SemanSequenceType(values["sequence_type"]),
        legacy_ub=bool(values["legacy_ub"]),
        own_skill=bool(values["own_skill"]),
        other_skill=bool(values["other_skill"]),
        verify=bool(values["verify"]),
    )


def _anneal_session(save: SaveFile, settings, run_dir: Path, **kwargs: Any):
    from .session import AnnealSession

    return AnnealSession(save, settings, run_dir, **kwargs)


def _anneal_plan(save: SaveFile, settings) -> list[str]:
    return [
        f"迭代 {settings.iterations} × 重启 {'不限（连续 3 轮无提升即停）' if not settings.restarts else f'{settings.restarts} 轮'}"
        f"（每 {settings.report_every} 次打一行日志）",
        f"温度 {settings.initial_temperature:g} → {settings.final_temperature:g}"
        f"（几何降温）/ 链长上限 {settings.chain_range} / 每步改动 {settings.moves} 次",
        f"邻域 单点 {settings.swap_chance:g} / 双点 {settings.double_swap_chance:g}"
        f" / 链式 {settings.chain_chance:g} / 手动 {settings.manual_chance:g}",
        f"语义口径 {settings.ga_equivalent().conversion_profile}",
    ]


# --------------------------------------------------------------------------- 注册表
_REGISTRY: dict[str, ModeSpec] = {}


def register(spec: ModeSpec, *, replace: bool = False) -> ModeSpec:
    """登记一个优化模式（供第三方/后续模式使用）。"""
    if not spec.key:
        raise ValueError("优化模式的 key 不能为空")
    if spec.key in _REGISTRY and not replace:
        raise ValueError(f"优化模式 {spec.key} 已经注册过了")
    _REGISTRY[spec.key] = spec
    return spec


def get(key: str) -> ModeSpec:
    try:
        return _REGISTRY[key]
    except KeyError:
        names = "、".join(_REGISTRY) or "（空）"
        raise ValueError(f"不认识的优化模式：{key}（可用：{names}）") from None


def unregister(key: str) -> None:
    """注销一个模式（测试与临时实验用）。"""
    _REGISTRY.pop(key, None)


def modes() -> tuple[ModeSpec, ...]:
    return tuple(_REGISTRY.values())


def keys() -> tuple[str, ...]:
    return tuple(_REGISTRY)


def labels() -> tuple[tuple[str, str], ...]:
    return tuple((spec.key, spec.label) for spec in _REGISTRY.values())


register(ModeSpec(
    key=FRAME,
    label="逐帧搜索",
    description="一格一格挪 UB 帧，结果最可控。",
    needs_frames=True,
    workbook_name="调帧结果.xlsx",
    params=FRAME_PARAMS,
    build=_frame_build,
    session=_frame_session,
    plan=_frame_plan,
))

register(ModeSpec(
    key=GA,
    label="语义 GA",
    description="用遗传算法整体搜索语义 UB 序列。",
    needs_frames=False,
    workbook_name="优化结果.xlsx",
    params=GA_PARAMS,
    build=_ga_build,
    session=_ga_session,
    plan=_ga_plan,
))

register(ModeSpec(
    key=ANNEAL,
    label="退火",
    description="模拟退火：按温度和概率接受变差的一步，适合从现有轴小幅改进。",
    needs_frames=False,
    workbook_name="退火结果.xlsx",
    params=ANNEAL_PARAMS,
    build=_anneal_build,
    session=_anneal_session,
    plan=_anneal_plan,
))


# --------------------------------------------------------------------------- 阵容搜索
ROSTER = "roster"

ROSTER_PARAMS: tuple[Param, ...] = (
    Param("battle_time", "战斗秒数", "模拟战斗时长（1~90 秒）。", "int", 90, 1, 90),
    Param("seed", "战斗种子", "固定随机种子。", "int", 666, -(2 ** 31), 2 ** 31 - 1),
    Param("samples", "采样种子数", "每个阵容评估几个种子取平均。", "int", 1, 1, 100),
    Param("batch_size", "单次场次", "一次接口请求里塞几场战斗（默认 32 = 一支队伍的 32 种组合一把发完）。", "int", 32, 1, 512),
    Param("timeout", "超时秒", "单次接口请求的超时时间。", "float", 180.0, 1, 3600),
    Param("pool", "候选角色", "逗号分隔的 unitId；界面里点角色勾选就是这一项。", "ids", ()),
    Param(
        "groups",
        "角色分组",
        "写法「组名:最少人数=角色ID,角色ID;…」，例如 A:2=100101,100201;B:1=130001。"
        "每个分组先抽够最少人数，剩下的名额从未分组的角色里补齐，所有队伍组合都会被排完。",
        "groups",
        (),
    ),
    Param("to_max_attr", "按满属性", "把这几个角色按满级满 Rank 算，否则沿用轴里的练度。", "bool", True),
    Param("every_combo", "每队试 32 种 AUTO/SET", "关掉只打全 AUTO 一场（快 32 倍，但不试 SET）。", "bool", True),
    Param("verify", "跑完复核", "导出前用最优阵容再打一场确认伤害。", "bool", True),
)


def _roster_build(values: dict[str, Any], save: SaveFile):
    from .roster import RosterSettings

    pool = values["pool"]
    if not pool:
        pool = tuple(int(unit) for unit in save.unit_ids)
    groups = values["groups"]
    for group in groups:
        for unit in group.members:
            if int(unit) not in pool:
                pool = tuple(pool) + (int(unit),)
    return RosterSettings(
        battle_time=values["battle_time"],
        seed=values["seed"],
        samples=values["samples"],
        batch_size=values["batch_size"],
        timeout=float(values["timeout"]),
        pool=tuple(pool),
        groups=groups,
        to_max_attr=bool(values["to_max_attr"]),
        every_combo=bool(values["every_combo"]),
        verify=bool(values["verify"]),
    )


def _roster_session(save: SaveFile, settings, run_dir: Path, **kwargs: Any):
    from .session import RosterSession

    return RosterSession(save, settings, run_dir, **kwargs)


def _roster_plan(save: SaveFile, settings) -> list[str]:
    from .roster import TEAM_SIZE, team_count, team_quota, ungrouped

    combos = 32 if settings.every_combo else 1
    combo_text = "32 种 AUTO/SET" if combos == 32 else "全 SET"
    total = team_count(settings)
    lines = [
        f"候选 {len(settings.pool)} 个角色，队伍固定 {TEAM_SIZE} 人",
        f"分组配额 {team_quota(settings)} 人，未分组 {len(ungrouped(settings))} 人"
        "（分组只看候选池里的成员，允许两个分组含同一个角色）",
        f"共 {total} 支队伍 × {combo_text} × {max(1, int(settings.samples))} 个种子 = {total * combos * max(1, int(settings.samples))} 场",
        f"属性 {'满级满 Rank' if settings.to_max_attr else '沿用轴里的练度'}",
    ]
    for group in settings.groups:
        if group.members:
            lines.append(f"分组 {group.label}")
    return lines


register(ModeSpec(
    key=ROSTER,
    label="阵容搜索",
    description="把候选池里所有队伍组合排完（支持分组配额），每队试 32 种 AUTO/SET 取最高伤害。",
    needs_frames=False,
    workbook_name="阵容结果.xlsx",
    params=ROSTER_PARAMS,
    build=_roster_build,
    session=_roster_session,
    plan=_roster_plan,
))
