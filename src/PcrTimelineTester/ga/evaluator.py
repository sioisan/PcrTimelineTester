"""语义模式的 /simulate 适配器。

跟帧模式客户端（``client.py``）的区别：
- 请求带 ``IsSemanMode=true`` + ``SemanUBExecTimeList``（五条基因）＋可选的
  ``SemanUBSequenceData`` / ``UseSequenceUB``；
- ``CalcStats`` 用 3（FAST_BASE）：只需要伤害、战斗帧、SemanUBNum；
- 需要行为去重时，再额外把 ``ActionProgressFrames`` 传进去，读回
  ``Stats.ActionProgress`` 算指纹；
- 另有两条「辅助调用」：``CalcStats=32`` 把帧轴转换成语义轴，
  ``CalcStats=2`` 把语义轴还原成帧轴（都读 ``Stats`` 里的对应字段）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import httpx

from ..client import TuneError, _expected_damage, simulation_url
from .engine import BattleSample, Evaluation
from .fingerprint import ActionProgressFingerprint
from .semantics import (
    EMPTY_GENOME,
    ROLE_COUNT,
    Genome,
    SemanSequenceType,
    SemanticConversionProfile,
    genome_from_rows,
    integer,
    sequence_roles,
)

FAST_BASE = 3
UB_TIME = 2
SEMANTIC_UB_CONVERSION = 32
AUTO_SEMAN_TEACHER = 128


def _object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise TuneError(f"{label} 缺失或不是对象")
    return value


@dataclass(frozen=True, slots=True)
class ConvertedAxis:
    lanes: Genome
    sequence: tuple[int, ...] | None = None
    frames: list[list[float]] = field(default_factory=list)


class GaEvaluator:
    """把基因序列评估成 ``Evaluation``。"""

    def __init__(
        self,
        *,
        context: dict,
        endpoint: str,
        battle_time: int,
        battle_seed: int,
        profile: SemanticConversionProfile,
        capture_frames: tuple[int, ...] = (),
        seeds: Sequence[int] = (666,),
        batch_size: int = 16,
        timeout: float = 180.0,
        unit_ids: Sequence[int] = (),
        roles: tuple[int, ...] | None = None,
        log=print,
    ) -> None:
        self.endpoint = simulation_url(endpoint)
        self.profile = profile
        self.context = context
        self.battle_time = integer(battle_time, "战斗秒数", 1)
        self.battle_seed = battle_seed
        self.capture_frames = tuple(capture_frames)
        self.seeds = tuple(integer(seed, "种子") for seed in seeds) or (battle_seed,)
        self.batch_size = integer(batch_size, "单次场次", 1)
        self.timeout = float(timeout)
        self.unit_ids = set(unit_ids)
        self.roles = tuple(roles) if roles is not None else None
        self.frame_set = set(self.capture_frames)
        self.log = log
        self.client = httpx.Client(timeout=self.timeout)
        self.battles = 0
        self.expected_damage = True

    # ------------------------------------------------------------------ 生命周期
    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "GaEvaluator":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------------ 请求构造
    def request_for(
        self,
        *,
        calc_stats: int,
        lanes: Genome = EMPTY_GENOME,
        frames: Sequence[Sequence[float]] = (),
        semantic: bool,
        roles: tuple[int, ...] | None = None,
        auto: bool = False,
        set_mode: bool = False,
        capture: bool = False,
        seed: int | None = None,
        profile: SemanticConversionProfile | None = None,
    ) -> dict:
        request = {
            "IsGuildBattle": True,
            "IsTalentQuest": False,
            **self.context,
            **(profile or self.profile).wire_fields(),
            "CalcStats": calc_stats,
            "Settings": {
                **self.context.get("Settings", {}),
                "UseFixedRandomSeed": True,
                "RandomSeed": self.battle_seed if seed is None else seed,
            },
            "UBExecTimeList": [list(row) for row in frames],
            "SemanUBExecTimeList": [list(lane) for lane in lanes],
            "SemanUBSequenceData": list(roles) if roles is not None else [],
            "UseSequenceUB": roles is not None,
            "IsAutoMode": auto,
            "IsSetMode": set_mode,
            "IsSemanMode": semantic,
            "NeedOnceResult": self.expected_damage and calc_stats == FAST_BASE,
        }
        if capture and self.capture_frames:
            request["ActionProgressFrames"] = list(self.capture_frames)
        return request

    def _post(self, requests: list[dict]) -> list[dict]:
        try:
            response = self.client.post(self.endpoint, json=requests)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise TuneError(f"接口返回 HTTP {exc.response.status_code}：{exc.response.text[:200]}") from exc
        except httpx.RequestError as exc:
            raise TuneError(f"接口请求失败（{type(exc).__name__}：{exc}）") from exc
        except ValueError as exc:
            raise TuneError("接口回包无法解析为 JSON") from exc
        if not isinstance(payload, list):
            raise TuneError("接口回包必须是数组（请求是数组，回包也应是数组）")
        if len(payload) != len(requests):
            raise TuneError(f"接口返回数量与请求不匹配（请求 {len(requests)} 项，回包 {len(payload)} 项）")
        for index, (request, result) in enumerate(zip(requests, payload, strict=True)):
            if not isinstance(result, dict):
                raise TuneError(f"接口回包第 {index + 1} 项必须是对象")
            if result.get("Success") is not True:
                raise TuneError(f"战斗失败（第 {index + 1} 项）：{result.get('Message') or '没有说明'}")
            expected = request["Settings"]["RandomSeed"]
            if type(result.get("CurrentSeed")) is not int or result["CurrentSeed"] != expected:
                raise TuneError("接口回包的 CurrentSeed 与固定战斗种子不符")
        self.battles += len(payload)
        return payload

    def _send(self, requests: list[dict]) -> list[dict]:
        chunks = (requests[start:start + self.batch_size] for start in range(0, len(requests), self.batch_size))
        results: list[dict] = []
        for chunk in chunks:
            results.extend(self._post(chunk))
        return results

    # ------------------------------------------------------------------ 解析
    def _evaluation(self, response: dict, *, capture: bool = False) -> Evaluation:
        stats = _object(response.get("Stats"), "Stats")
        raw = stats.get("ActionProgress") if capture else None
        fingerprint = None if raw is None else ActionProgressFingerprint.from_payload(raw)
        if fingerprint and (
            not self.unit_ids.issuperset(fingerprint.unit_ids)
            or not self.frame_set.issuperset(fingerprint.frames)
        ):
            raise TuneError("ActionProgress 的角色或采样帧与请求不符")
        counts = response.get("SemanUBNum")
        if not isinstance(counts, list):
            raise TuneError("接口回包缺少 SemanUBNum 数组")
        return Evaluation(
            damage=self._damage(response, stats),
            frames=int(response.get("BattleFrames") or 0),
            result=int(response.get("Result") or 0),
            ub_counts=tuple(int(count) for count in counts),
            fingerprint=fingerprint,
        )

    def _damage(self, response: dict, stats: dict) -> int:
        damage = _expected_damage(response) if self.expected_damage else None
        if damage is None:
            damage = stats.get("TotalDamage")
        if isinstance(damage, bool) or not isinstance(damage, (int, float)):
            raise TuneError("接口回包里没有可用的伤害")
        return int(round(damage))

    def _aggregate(self, responses: list[dict], *, capture: bool = False) -> Evaluation:
        first = self._evaluation(responses[0], capture=capture)
        if len(responses) == 1:
            return first
        evaluations = [first] + [self._evaluation(response) for response in responses[1:]]
        samples = tuple(
            BattleSample(seed, item.damage, item.frames, item.result)
            for seed, item in zip(self.seeds, evaluations, strict=True)
        )
        return Evaluation(
            first.damage, first.frames, first.result, first.ub_counts, first.fingerprint, samples,
        )

    # ------------------------------------------------------------------ 对外接口
    def evaluate(self, genomes: Sequence[Genome]) -> list[Evaluation]:
        requests = [
            self.request_for(
                calc_stats=FAST_BASE, lanes=genome, semantic=True, roles=self.roles,
                capture=index == 0 and position == 0, seed=seed,
            )
            for position, genome in enumerate(genomes)
            for index, seed in enumerate(self.seeds)
        ]
        responses = self._send(requests)
        count = len(self.seeds)
        return [
            self._aggregate(responses[start:start + count], capture=bool(self.capture_frames))
            for start in range(0, len(responses), count)
        ]

    def convert(self, frames: Sequence[Sequence[float]]) -> ConvertedAxis:
        """帧轴 → 语义轴。

        先试 ``CalcStats=32`` 的 ``Stats.UbConversion``（摸轴器的语义转换接口）；
        老摸轴器没有这个字段时，退回「自动语义老师」：用 ``CalcStats=128`` 让摸轴器
        自己排出这五条语义基因（``Stats.AutoSemanUBExecTimeList``）。
        """
        result = self._send([self.request_for(
            calc_stats=SEMANTIC_UB_CONVERSION, frames=frames, semantic=False,
        )])[0]
        stats = _object(result.get("Stats"), "Stats")
        raw = stats.get("UbConversion")
        if isinstance(raw, dict):
            lanes = genome_from_rows(raw.get("SemanUBExecTimeList"), trim_sentinels=True)
            roles = raw.get("SemanUBSequenceData")
            if self.profile.sequence_type is SemanSequenceType.SEQUENCE:
                return ConvertedAxis(lanes, sequence_roles(roles, lanes))
            return ConvertedAxis(lanes, None)
        self.log("提示：这台摸轴器没有 Stats.UbConversion，改用自动语义老师推出的起点")
        return self._teacher_axis(frames)

    def _teacher_axis(self, frames: Sequence[Sequence[float]]) -> ConvertedAxis:
        """老摸轴器：读 ``Stats.AutoSemanUBExecTimeList`` 当语义起点。"""
        result = self._send([self.request_for(
            calc_stats=AUTO_SEMAN_TEACHER, frames=frames, semantic=False,
        )])[0]
        stats = _object(result.get("Stats"), "Stats")
        rows = stats.get("AutoSemanUBExecTimeList")
        if not isinstance(rows, list) or len(rows) != ROLE_COUNT:
            raise ValueError(
                "这台摸轴器既不支持帧轴→语义轴转换（没有 Stats.UbConversion），"
                "也没有自动语义老师的输出（Stats.AutoSemanUBExecTimeList）。"
                "请先在 PCRCalculator 里把这条轴切成语义模式再导出，或改用逐帧搜索。"
            )
        lanes = genome_from_rows(rows, trim_sentinels=True)
        roles = None
        if self.profile.sequence_type is SemanSequenceType.SEQUENCE:
            # 老师不排跨角色顺序：按「先角色 0 的基因、再角色 1 …」直接串起来。
            roles = tuple(role for role, lane in enumerate(lanes) for _ in lane)
        return ConvertedAxis(lanes, roles)

    def lanes_to_frames(self, lanes: Genome, roles: tuple[int, ...] | None = None, *,
                        profile: SemanticConversionProfile | None = None) -> list[list[float]]:
        """语义轴 → 帧轴（``CalcStats=2`` 读 ``Stats.UBTime``）。

        ``profile`` 用来试不同的语义口径（自检时会挨个试），不传就用当前口径。
        """
        roles = self.roles if roles is None else roles
        result = self._send([self.request_for(
            calc_stats=UB_TIME, lanes=lanes, roles=roles, semantic=True, profile=profile,
        )])[0]
        stats = _object(result.get("Stats"), "Stats")
        rows = stats.get("UBTime")
        if not isinstance(rows, list) or not rows:
            raise TuneError("接口回包里没有 Stats.UBTime（拿不到这条语义轴对应的帧）")
        return [[float(value) for value in row] for row in rows]

    def frame_damage(self, frames: Sequence[Sequence[float]]) -> int:
        """帧轴本身能打多少（自检用：语义起点的伤害应该和它一致）。

        帧模式没有 ``SemanUBNum``，所以这里只读伤害，不走 ``_evaluation``。
        """
        responses = self._send([
            self.request_for(calc_stats=FAST_BASE, frames=frames, semantic=False, seed=seed)
            for seed in self.seeds
        ])
        damages: list[int] = []
        for response in responses:
            stats = _object(response.get("Stats"), "Stats")
            damages.append(self._damage(response, stats))
        return int(round(sum(damages) / len(damages)))

    def initial_axis(self, *, frames: Sequence[Sequence[float]] = (), lanes: Genome = EMPTY_GENOME,
                     roles: tuple[int, ...] | None = None) -> ConvertedAxis:
        """现有轴的语义起点：语义轴先还原再转换（与原实现一致），帧轴直接转换。"""
        if any(lanes):
            converted_frames = self.lanes_to_frames(lanes, roles)
            converted = self.convert(converted_frames)
            return ConvertedAxis(converted.lanes, converted.sequence, converted_frames)
        if not frames:
            raise ValueError("这条轴既没有帧数据也没有语义数据")
        converted = self.convert(frames)
        return ConvertedAxis(converted.lanes, converted.sequence, [list(row) for row in frames])

    def replay(self, lanes: Genome, roles: tuple[int, ...] | None = None) -> tuple[Evaluation, list[list[float]]]:
        roles = self.roles if roles is None else roles
        responses = self._send([
            self.request_for(calc_stats=FAST_BASE, lanes=lanes, roles=roles, semantic=True, seed=seed)
            for seed in self.seeds
        ])
        # FAST_BASE 已包含 UB_TIME。老算器缺少此字段时单独取一次。
        evaluation = self._aggregate(responses)
        rows = responses[0]["Stats"].get("UBTime")
        if self.seeds[0] == self.battle_seed and isinstance(rows, list) and rows:
            return evaluation, [list(map(float, row)) for row in rows]
        return evaluation, self.lanes_to_frames(lanes, roles)
