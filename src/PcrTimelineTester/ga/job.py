"""语义 GA 模式的任务本体。

一次 GA 任务 = 把现有轴转换成语义起点 → 跑遗传算法 → 用最优基因 replay 回
帧轴并导出。检查点直接写进 ``state.json``（``GenerationState`` 自带序列化），
所以 Ctrl+C 之后可以用同一个运行目录续跑。
"""

from __future__ import annotations

from ..recovery import verifying

from dataclasses import dataclass, field, replace
import threading
import time

from ..client import TuneError
from ..savefile import SaveFile, write_tuned_save  # noqa: F401  (re-export 便于 session 使用)
from .engine import Candidate, Evaluation, GenerationState, optimize
from .evaluator import GaEvaluator
from .fingerprint import sample_frames
from .semantics import (
    EMPTY_GENOME,
    ROLE_COUNT,
    Genome,
    SemanSequenceType,
    SemanSkillType,
    SemanUBType,
    SemanticConversionProfile,
    genome_from_rows,
    search_pools,
)
from .settings import GaSettings


class GaCancelled(RuntimeError):
    """用户按了停止。"""


@dataclass(frozen=True, slots=True)
class GaPrepared:
    """起点信息：转换后的语义轴 + 固定前缀 + 基因池。"""

    seed_lanes: Genome
    roles: tuple[int, ...] | None
    prefix: Genome
    initial: Genome
    pools: Genome
    frames: list[list[float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "seed_lanes": [list(lane) for lane in self.seed_lanes],
            "roles": list(self.roles) if self.roles is not None else None,
            "prefix": [list(lane) for lane in self.prefix],
            "initial": [list(lane) for lane in self.initial],
            "pools": [list(lane) for lane in self.pools],
            "frames": [list(row) for row in self.frames],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GaPrepared":
        roles = data.get("roles")
        return cls(
            seed_lanes=genome_from_rows(data["seed_lanes"]),
            roles=None if roles is None else tuple(int(role) for role in roles),
            prefix=genome_from_rows(data["prefix"]),
            initial=genome_from_rows(data["initial"]),
            pools=genome_from_rows(data["pools"]),
            frames=[[float(value) for value in row] for row in data.get("frames") or []],
        )


class GaJob:
    """一次语义 GA 优化。"""

    def __init__(
        self,
        save: SaveFile,
        settings: GaSettings,
        *,
        log=print,
        evaluator: GaEvaluator | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.save = save
        self.settings = settings
        self.log = log
        self.cancel = cancel or threading.Event()
        self._evaluator = evaluator
        self.prepared: GaPrepared | None = None
        self.state: GenerationState | None = None
        self.baseline: Evaluation | None = None
        self.history: list[dict] = []
        self.rounds: list[dict] = []
        self.export: dict | None = None
        self._resumed = False
        self.profile_detected = False
        self.expected_damage = True
        # 轴自带的语义数据能不能原样还原成自己的帧轴（False 时起点改用帧轴推）
        self.semantic_usable = False
        self.frame_reference: int | None = None

    # ------------------------------------------------------------------ 外部接口
    @property
    def evaluator(self) -> GaEvaluator:
        if self._evaluator is None:
            settings = self.settings
            self._evaluator = GaEvaluator(
                context=self.save.request_context(
                    battle_time=settings.battle_time, battle_seed=settings.seed,
                ),
                endpoint=settings.endpoint,
                battle_time=settings.battle_time,
                battle_seed=settings.seed,
                profile=settings.conversion_profile,
                capture_frames=sample_frames(settings.battle_time)
                if settings.fingerprint_distance is not None else (),
                seeds=settings.seeds(),
                batch_size=settings.batch_size,
                timeout=settings.timeout,
                unit_ids=self.save.unit_ids,
                log=self.log,
            )
            self._evaluator.expected_damage = self.expected_damage
        return self._evaluator

    @property
    def cancelled_requested(self) -> bool:
        return self.cancel.is_set()

    @property
    def simulations(self) -> int:
        """已经模拟了多少场（界面/日志用）。"""
        return self.state.simulations if self.state is not None else 0

    @property
    def battles(self) -> int:
        """打过的战斗场次（多次评估共用同一个摸轴器连接时计数）。"""
        return self._evaluator.battles if self._evaluator is not None else 0

    def cancel_run(self) -> None:
        self.cancel.set()

    def close(self) -> None:
        if self._evaluator is not None:
            self._evaluator.close()

    # ------------------------------------------------------------------ 准备
    def _prepare(self) -> GaPrepared:
        settings = self._resolved_settings()
        # 轴里自带的语义数据如果连自己那套帧轴都还原不出来（多半是导出时保存的
        # SemanUBExecTimeData 和 UBExecTimeData 不是同一次状态），就别拿它当起点：
        # 改用帧轴 + 自动语义老师推出起点，起点伤害才能和帧轴一致。
        # （没有帧轴可用时当然还是得用轴里的语义数据。）
        usable = self.semantic_usable or not self.save.has_frames
        lanes = tuple(tuple(lane) for lane in self.save.semantic) if usable else EMPTY_GENOME
        if self.save.semantic and not usable:
            self.log("提示：这条轴自带的语义数据和它的帧轴对不上，起点改成用帧轴重新推出（自动语义老师）")
        axis = self.evaluator.initial_axis(
            frames=self.save.frames, lanes=lanes, roles=self.save.sequence,
        )
        seed_lanes = tuple(tuple(lane) for lane in axis.lanes)
        fixed = settings.fixed_ub_counts
        if any(count > len(lane) for count, lane in zip(fixed, seed_lanes, strict=True)):
            raise ValueError("固定UB数超过了这条轴每行的 UB 个数")
        prefix = tuple(tuple(lane[:count]) for lane, count in zip(seed_lanes, fixed, strict=True))
        initial = tuple(tuple(lane[count:]) for lane, count in zip(seed_lanes, fixed, strict=True))
        pools = search_pools(
            seed_lanes,
            settings.extra_genes,
            legacy_ub=bool(settings.legacy_ub),
            own_skill=settings.own_skill,
            other_skill=settings.other_skill,
            delay=settings.delay,
        )
        if initial and any(len(lane) > settings.max_lane_length for lane in initial):
            raise ValueError("这条轴的 UB 数量超过了 max_lane_length，请调大后再跑")
        prepared = GaPrepared(seed_lanes, axis.sequence, prefix, initial, pools, axis.frames)
        # 序列模式要把角色序列一路带到每条请求里，评估器自己不知道轴的起点。
        try:
            self.evaluator.roles = prepared.roles
        except AttributeError:  # 测试里的假评估器可能不允许赋值
            pass
        return prepared

    def _resolved_settings(self) -> GaSettings:
        """开工前先把语义口径定下来：序列模式没有角色序列就退回 LANES，
        再拿这条轴自己的语义基因反推「哪个口径能把它原样还原成帧轴」。"""
        settings = self.settings
        if settings.sequence_type is SemanSequenceType.SEQUENCE and not self.save.sequence:
            self.log(
                "提示：这条轴里没有角色序列（SemanUBSequenceData），"
                "「同时调角色序列」用不了，已改回只调五条 UB 序列"
            )
            settings = replace(settings, sequence_type=SemanSequenceType.LANES)
        detected = self._detect_profile(settings)
        if detected is not None and detected != settings.conversion_profile:
            self.log(
                f"语义口径自检：改成 UB类型 {detected.ub_type.name} / 技能类型 {detected.skill_type.name}"
                f" / 序列类型 {detected.sequence_type.name}（这样能把这条轴原样还原）"
            )
            settings = replace(
                settings,
                ub_type=detected.ub_type,
                skill_type=detected.skill_type,
                sequence_type=detected.sequence_type,
            )
            self.profile_detected = True
        self.settings = settings
        # 评估器的口径是构造时定下来的，改过 settings 之后要同步过去，
        # 否则后面的 replay / 转换还在用旧口径（序列模式尤其明显）。
        try:
            self.evaluator.profile = settings.conversion_profile
        except AttributeError:  # 测试里的假评估器可能不允许赋值
            pass
        return settings

    def _detect_profile(self, settings: GaSettings) -> SemanticConversionProfile | None:
        """用轴里自带的语义基因还原帧轴，找出能逐格对上的口径。

        轴的 ``SemanUBExecTimeData`` + ``SemanUBSequenceData`` 本来就是摸轴器按某个口径
        写出来的，只有用同一口径还原才会得到原来的 ``UBExecTimeData``。口径选错
        （比如序列类型选了「同时调角色序列」但轴里没有序列）时还原出的帧会完全不同，
        伤害也就跟着塌了——所以这里先对上再跑。
        """
        if not self.save.has_semantic or not self.save.has_frames or not self.save.semantic:
            return None
        lanes = tuple(tuple(lane) for lane in self.save.semantic)
        roles = self.save.sequence
        candidates = [
            SemanticConversionProfile(ub, skill, sequence)
            for ub in SemanUBType
            for skill in SemanSkillType
            for sequence in ((SemanSequenceType.LANES, SemanSequenceType.SEQUENCE)
                             if roles else (SemanSequenceType.LANES,))
        ]
        current = settings.conversion_profile
        if current in candidates:
            candidates.remove(current)
            candidates.insert(0, current)
        for index, profile in enumerate(candidates):
            try:
                frames = self.evaluator.lanes_to_frames(lanes, roles, profile=profile)
            except (ValueError, RuntimeError, TypeError, TuneError):
                continue
            if self._frames_match(frames, self.save.frames):
                self.semantic_usable = True
                if index == 0:
                    return None  # 当前口径就能对上，什么都不用改
                return profile
        self.log(
            "警告：这条轴的语义基因用任何口径都还原不回原来的帧轴，"
            "起点的伤害可能和帧轴对不上（下面会再核对一次）"
        )
        return None

    @staticmethod
    def _frames_match(actual, expected, *, tolerance: float = 0.01) -> bool:
        """逐格比帧轴。

        摸轴器只回五条角色行，而轴里可能还多一条 Boss 行，所以只比前 ``ROLE_COUNT`` 行。
        """
        rows = min(len(actual), len(expected), ROLE_COUNT)
        if rows <= 0:
            return False
        for left, right in zip(actual[:rows], expected[:rows], strict=True):
            if len(left) != len(right):
                return False
            for a, b in zip(left, right, strict=True):
                if abs(float(a) - float(b)) > tolerance:
                    return False
        return True

    def _cross_check_baseline(self) -> None:
        """拿帧轴本身跑一场当参考：语义起点的伤害应该和它一样。

        对不上通常意味着语义口径或 UB 条件跟这条轴不匹配（例如序列类型选错），
        这时候继续优化出来的数字不可信，所以要显式说出来。
        """
        if self.baseline is None or not self.save.has_frames or not self.save.frames:
            return
        check = getattr(self.evaluator, "frame_damage", None)
        if check is None:
            return
        try:
            reference = check(self.save.frames)
        except (ValueError, RuntimeError, TypeError, TuneError) as error:
            self.log(f"警告：帧轴参考伤害没取到（{error}），跳过口径核对")
            return
        self.frame_reference = int(reference)
        baseline = self.baseline.mean_damage
        if abs(reference - baseline) <= max(1.0, abs(reference) * 0.001):
            return
        self.log(
            f"[warn] 语义起点与帧轴伤害不一致：帧轴 {reference:,} / 语义起点 {baseline:,}"
            f"（战斗帧 {self.baseline.frames}）。多半是语义口径或 UB 条件对不上，"
            f"这次优化的数字不可信——把「序列类型」改回只调五条 UB、"
            f"或把「技能类型」换成轴里用的那种再跑。"
        )

    def _check_cancel(self) -> None:
        if self.cancelled_requested:
            raise GaCancelled("已停止")

    # ------------------------------------------------------------------ 主流程
    def run(self) -> dict:
        started = time.perf_counter()
        if self.prepared is None:
            self.log("准备语义起点：把现有轴转换成语义 UB …")
            self.prepared = self._prepare()
            self.log(
                "语义起点："
                + " / ".join(f"角色{index + 1} {len(lane)} 个 UB" for index, lane in enumerate(self.prepared.seed_lanes))
            )
        settings = self.settings
        prepared = self.prepared
        self._check_cancel()

        if self.baseline is None:
            self.baseline, frames = self.evaluator.replay(prepared.seed_lanes, prepared.roles)
            self.log(
                f"=== 基线：期望伤害 {self.baseline.mean_damage:,} × 战斗帧 {self.baseline.frames} "
                f"/ 结果 {self.baseline.result} ==="
            )
            self._cross_check_baseline()
            prepared = GaPrepared(
                prepared.seed_lanes, prepared.roles, prepared.prefix, prepared.initial,
                prepared.pools, frames,
            )
            self.prepared = prepared

        def on_checkpoint(state: GenerationState) -> None:
            self.state = state
            best = state.best.evaluation
            entry = {
                "generation": state.generation,
                "best_generation": state.best_generation,
                "damage": best.mean_damage,
                "score": best.score(settings.battle_time),
                "simulations": state.simulations,
            }
            if not self.history or self.history[-1]["generation"] != state.generation:
                self.history.append(entry)
            self.log(
                f"第 {state.generation}/{settings.generations} 代：最优 {best.mean_damage:,}"
                f"（第 {state.best_generation} 代产生），已模拟 {state.simulations} 场，"
                f"重复 {state.exact_duplicates + state.behavior_duplicates} 次"
            )
            self._check_cancel()

        rounds = max(1, int(settings.restarts))
        resume_state = self.state
        best_state = None
        for round_index in range(rounds):
            round_settings = (
                settings if round_index == 0
                else settings.with_changes(ga_seed=settings.ga_seed + round_index)
            )
            if rounds > 1:
                self.log(
                    f"--- 第 {round_index + 1}/{rounds} 轮：随机种子 {round_settings.ga_seed}，"
                    f"{'从检查点继续' if resume_state is not None else '重新起跑'} ---"
                )
            state = optimize(
                initial=prepared.initial,
                prefix=prepared.prefix,
                pools=prepared.pools,
                battle_time=settings.battle_time,
                evaluator=self.evaluator.evaluate,
                config=round_settings.ga_config(),
                fixed_lengths=prepared.roles is not None,
                resume=resume_state,
                on_checkpoint=on_checkpoint,
            )
            resume_state = None
            score = state.best.evaluation.score(settings.battle_time)
            if best_state is None or score > best_state.best.evaluation.score(settings.battle_time):
                best_state = state
            self.rounds.append({
                "round": round_index + 1,
                "seed": round_settings.ga_seed,
                "generations": state.generation,
                "best_generation": state.best_generation,
                "damage": state.best.evaluation.mean_damage,
                "score": score,
                "simulations": state.simulations,
                "stopped_early": state.stopped_early,
            })
            self._check_cancel()
        state = best_state
        self.state = state
        self._check_cancel()

        best = state.best.evaluation
        best_lanes = tuple(
            fixed + lane for fixed, lane in zip(prepared.prefix, state.best.genome, strict=True)
        )
        with verifying([], best.mean_damage,
                       semantic=best_lanes, sequence=prepared.roles):
            replay, frames = self.evaluator.replay(best_lanes, prepared.roles)
        self.log(f"导出前 replay：期望伤害 {replay.mean_damage:,} × 战斗帧 {replay.frames}")
        sequence = prepared.roles
        self.export = {
            "lanes": [list(lane) for lane in best_lanes],
            "sequence": list(sequence) if sequence is not None else None,
            "frames": [list(row) for row in frames],
            "damage": replay.mean_damage,
            "frames_count": replay.frames,
            "result": replay.result,
            "ub_counts": list(replay.ub_counts),
        }
        elapsed = time.perf_counter() - started
        summary = self.summary(elapsed)
        self.log(
            f"=== 优化完成：期望伤害 {replay.mean_damage:,}（基线 {self.baseline.mean_damage:,}，"
            f"{'+' if replay.mean_damage >= self.baseline.mean_damage else ''}"
            f"{replay.mean_damage - self.baseline.mean_damage:,}）/ "
            f"{state.generation} 代 / {summary['simulations']} 场 / 用时 {elapsed:.1f}s ==="
        )
        return summary

    # ------------------------------------------------------------------ 结果与续跑
    def summary(self, elapsed: float) -> dict:
        settings = self.settings
        state = self.state
        best = state.best.evaluation if state is not None else None
        return {
            "mode": "ga",
            "excel": str(self.save.path),
            "endpoint": settings.endpoint,
            "battle_time": settings.battle_time,
            "seed": settings.seed,
            "samples": settings.samples,
            "ga": settings.ga_config().to_dict(),
            "semantic": settings.conversion_profile.to_dict(),
            "generations": state.generation if state is not None else 0,
            "best_generation": state.best_generation if state is not None else 0,
            # 多轮重启时每轮各自计数，报告里要的是整趟跑掉的场次。
            "simulations": sum(int(item.get("simulations") or 0) for item in self.rounds)
            or (state.simulations if state is not None else 0),
            "exact_duplicates": state.exact_duplicates if state is not None else 0,
            "behavior_duplicates": state.behavior_duplicates if state is not None else 0,
            "baseline": self.baseline.mean_damage if self.baseline is not None else 0,
            "frame_damage": self.frame_reference,
            "profile_detected": self.profile_detected,
            "baseline_frames": self.baseline.frames if self.baseline is not None else 0,
            "final": best.mean_damage if best is not None else 0,
            "best_score": best.score(settings.battle_time) if best is not None else 0,
            "history": list(self.history),
            "rounds": list(self.rounds),
            "restarts": settings.restarts,
            "export": self.export,
            "cancelled": self.cancelled_requested,
            "battles": self.evaluator.battles if self._evaluator is not None else 0,
            "elapsed_s": round(elapsed, 1),
        }

    def state_dict(self) -> dict:
        return {
            "mode": "ga",
            "expected_damage": self.expected_damage,
            "profile": self.settings.conversion_profile.to_dict(),
            "prepared": None if self.prepared is None else self.prepared.to_dict(),
            "state": None if self.state is None else self.state.to_dict(),
            "history": list(self.history),
            "baseline": None if self.baseline is None else self.baseline.to_dict(),
            "export": self.export,
        }

    def apply_state(self, data: dict | None) -> None:
        if not data:
            return
        # 旧任务按实战伤害排名，续跑时新旧候选必须使用同一口径。
        self.expected_damage = bool(data.get("expected_damage", False))
        prepared = data.get("prepared")
        if prepared:
            self.prepared = GaPrepared.from_dict(prepared)
            if data.get("profile"):
                profile = SemanticConversionProfile.from_dict(data["profile"])
                self.settings = replace(self.settings, ub_type=profile.ub_type, skill_type=profile.skill_type,
                                        sequence_type=profile.sequence_type)
                self.evaluator.profile = profile
            else:
                self._resolved_settings()
            self.evaluator.roles = self.prepared.roles
        baseline = data.get("baseline")
        if baseline:
            self.baseline = Evaluation.from_dict(baseline)
        state = data.get("state")
        if state:
            self.state = GenerationState.from_dict(state)
        self.history = list(data.get("history") or [])
        self.rounds = list(data.get("rounds") or [])
        if self.prepared and self.prepared.roles is not None and self.state:
            counts = tuple(map(len, self.prepared.initial))
            saved = [self.state.best, *(c for island in self.state.islands for c in island)]
            if any(tuple(map(len, candidate.genome)) != counts for candidate in saved):
                self.log("旧检查点中有候选刀数与角色顺序不符，本次沿用起点与基线重新搜索。")
                self.state = None
                self.history.clear()
                self.rounds.clear()
        self.export = data.get("export")
        self._resumed = True
        if self.state is not None:
            self.log(
                f"续跑：从第 {self.state.generation} 代继续（最优 {self.state.best.evaluation.mean_damage:,}，"
                f"第 {self.state.best_generation} 代产生）"
            )
