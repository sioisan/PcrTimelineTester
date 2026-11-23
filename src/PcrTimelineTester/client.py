"""PCRCalculator ``/simulate`` adapter for frame-axis battles.

The wire contract is array based: one POST carries an *array* of battle requests,
the response array is in request order, and a single failed item aborts the whole
group instead of being counted as zero damage.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from .savefile import FrameAxis

DEFAULT_CALC_STATS = 3         # Core | UBTime — enough for Stats.TotalDamage
FULL_CALC_STATS = 511          # every statistics flag; slow but complete
DEFAULT_ENDPOINT = "http://127.0.0.1:8080/simulate"


class TuneError(RuntimeError):
    """Transport or battle failure; never silently skipped."""


class _StatsMissing(Exception):
    """Stats were absent, usually because CalcStats was too small."""


@dataclass(frozen=True, slots=True)
class BattleOutcome:
    """One evaluated frame axis (already averaged over the sample seeds).

    ``metric`` is what the search ranks: the 期望伤害 (``OnceResult.exceptDamage``)
    when the server reports one, otherwise the sampled ``Stats.TotalDamage``.
    ``damage`` keeps the sampled damage and ``expected`` the expected value when the
    server actually produced a distinct one (``None`` otherwise).
    """

    metric: float
    damage: int
    battle_frames: int
    result: int
    seed: int
    expected: float | None = None
    # Stats.UBTime：每个玩家实际放出 UB 的帧（用来判断「这一帧根本放不出来」）
    ub_frames: tuple[tuple[float, ...], ...] = ()

    warnings: tuple[str, ...] = ()
    first_expected: float | None = None

    @property
    def is_expected(self) -> bool:
        return self.expected is not None


RESULT_NAMES = {-1: "INVALID", 0: "WIN", 1: "LOSE", 2: "TIME_OVER", 3: "RETIRE"}


def _ub_frames(stats: dict) -> tuple[tuple[float, ...], ...]:
    """``Stats.UBTime`` → 每个玩家实际放 UB 的帧（缺失就当没有）。"""
    rows = stats.get("UBTime")
    if not isinstance(rows, list):
        return ()
    parsed: list[tuple[float, ...]] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        values = [
            float(value)
            for value in row
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        parsed.append(tuple(values))
    return tuple(parsed)


def _expected_damage(result: dict) -> float | None:
    """``OnceResult.exceptDamage`` when the server reports a usable 期望伤害."""
    once = result.get("OnceResult")
    if not isinstance(once, dict):
        return None
    value = once.get("exceptDamage")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


RESULT_NAMES = {-1: "INVALID", 0: "WIN", 1: "LOSE", 2: "TIME_OVER", 3: "RETIRE"}


def result_name(code: int) -> str:
    return RESULT_NAMES.get(code, f"#{code}")


def simulation_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("接口地址必须是 HTTP(S) URL")
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("接口地址必须是 HTTP(S) URL，且地址中不能包含用户名或密码")
    if parts.fragment or parts.query:
        raise ValueError("接口地址不能包含查询参数或片段")
    _ = parts.port
    return urlunsplit(parts._replace(path=parts.path.rstrip("/") or "/simulate"))


def _head(text: str | None, limit: int = 300) -> str:
    if not text:
        return "<空>"
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


class SimulatorClient:
    """Batched ``POST /simulate`` client.

    ``context`` is :meth:`PcrTimelineTester.savefile.SaveFile.request_context`; the
    frame axis and the per-request random seed are filled in per candidate.
    """

    def __init__(
        self,
        *,
        context: dict,
        endpoint: str = DEFAULT_ENDPOINT,
        seeds: tuple[int, ...] = (666,),
        timeout: float = 180.0,
        batch_size: int = 16,
        calc_stats: int = DEFAULT_CALC_STATS,
        log: Callable[[str], None] = print,
    ) -> None:
        if not seeds:
            raise ValueError("至少需要一个战斗随机种子")
        self.endpoint = simulation_url(endpoint)
        self.context = context
        self.seeds = tuple(int(seed) for seed in seeds)
        self.batch_size = max(1, int(batch_size))
        self.calc_stats = int(calc_stats)
        self.log = log
        self.http = httpx.Client(timeout=httpx.Timeout(float(timeout), connect=min(15.0, float(timeout))))
        self.battles = 0

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "SimulatorClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ wire

    def request_for(self, frames: FrameAxis, seed: int) -> dict:
        settings = self.context.get("Settings", {})
        return {
            **self.context,
            "CalcStats": self.calc_stats,
            "IsSkipping": True,
            "Settings": {**settings, "UseFixedRandomSeed": True, "RandomSeed": int(seed)},
            "UBExecTimeList": [list(row) for row in frames],
            "SemanUBExecTimeList": [],
            "SemanUBSequenceData": [],
            "UseSequenceUB": False,
            "IsSemanMode": False,
            "IsAutoMode": False,
            "IsSetMode": False,
            # 期望伤害走 OnceResult（API.md §2.6：OnceResult 需要 NeedOnceResult=true）。
            "NeedOnceResult": True,
        }

    def _post(self, requests: list[dict]) -> list[dict]:
        responses: list[dict] = []
        for start in range(0, len(requests), self.batch_size):
            chunk = requests[start:start + self.batch_size]
            try:
                response = self.http.post(self.endpoint, json=chunk)
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPStatusError as exc:
                raise TuneError(
                    f"接口返回 HTTP {exc.response.status_code}：{_head(exc.response.text)}"
                ) from exc
            except httpx.RequestError as exc:
                raise TuneError(f"接口请求失败（{type(exc).__name__}：{exc}）") from exc
            except ValueError as exc:
                raise TuneError("接口回包无法解析为 JSON") from exc
            if not isinstance(payload, list):
                raise TuneError("接口回包必须是数组（请求是数组，回包也应是数组）")
            if len(payload) != len(chunk):
                raise TuneError(f"接口返回数量与请求不匹配（请求 {len(chunk)} 项，回包 {len(payload)} 项）")
            responses.extend(payload)
            self.battles += len(chunk)
        return responses

    def _outcome(self, result: object, seed: int, index: int) -> BattleOutcome:
        if not isinstance(result, dict):
            raise TuneError(f"接口回包第 {index} 项必须是对象")
        if result.get("Success") is not True:
            raise TuneError(f"战斗失败（第 {index} 项）：{result.get('Message') or '（无消息）'}")
        current_seed = result.get("CurrentSeed")
        if isinstance(current_seed, bool) or not isinstance(current_seed, int) or current_seed != seed:
            raise TuneError(f"回包 CurrentSeed 与请求种子不符（请求 {seed}，回包 {current_seed!r}）")
        stats = result.get("Stats")
        if not isinstance(stats, dict):
            raise _StatsMissing()
        damage = stats.get("TotalDamage")
        if damage is None:
            raise _StatsMissing()
        damage_value = float(damage)
        if not math.isfinite(damage_value):
            raise TuneError(f"回包伤害不是有限数值（第 {index} 项）")
        expected = _expected_damage(result)
        battle_frames = result.get("BattleFrames")
        result_code = result.get("Result")
        return BattleOutcome(
            metric=expected if expected is not None else damage_value,
            damage=int(round(damage_value)),
            battle_frames=int(battle_frames) if isinstance(battle_frames, (int, float)) else 0,
            result=int(result_code) if isinstance(result_code, (int, float)) else -1,
            seed=seed,
            expected=expected,
            ub_frames=_ub_frames(stats),
            warnings=tuple((result.get("OnceResult") or {}).get("warnings") or ()),
            first_expected=expected,
        )

    # -------------------------------------------------------------- evaluate

    def evaluate(self, candidates: list[FrameAxis]) -> list[BattleOutcome]:
        """Battle every candidate on every sample seed, return mean damage."""
        if not candidates:
            return []
        try:
            return self._evaluate(candidates)
        except _StatsMissing:
            if self.calc_stats == FULL_CALC_STATS:
                raise TuneError("接口回包里没有 Stats.TotalDamage（CalcStats 已经是 511）") from None
            self.log(f"警告：CalcStats={self.calc_stats} 时回包没有 Stats，自动改用 {FULL_CALC_STATS} 重试一次")
            self.calc_stats = FULL_CALC_STATS
            try:
                return self._evaluate(candidates)
            except _StatsMissing:
                raise TuneError("接口回包里没有 Stats.TotalDamage（CalcStats 已经是 511）") from None

    def _evaluate(self, candidates: list[FrameAxis]) -> list[BattleOutcome]:
        requests: list[dict] = []
        seeds: list[int] = []
        owners: list[int] = []
        for owner, frames in enumerate(candidates):
            for seed in self.seeds:
                requests.append(self.request_for(frames, seed))
                seeds.append(seed)
                owners.append(owner)
        responses = self._post(requests)
        grouped: dict[int, list[BattleOutcome]] = {}
        for owner, seed, request_index, result in zip(owners, seeds, range(1, len(requests) + 1), responses):
            grouped.setdefault(owner, []).append(self._outcome(result, seed, request_index))
        outcomes: list[BattleOutcome] = []
        for owner in range(len(candidates)):
            items = grouped.get(owner)
            if not items:
                raise TuneError(f"接口没有返回第 {owner + 1} 项的结果")
            damages = [item.damage for item in items]
            metrics = [item.metric for item in items]
            expected = [item.expected for item in items]
            outcomes.append(BattleOutcome(
                metric=sum(metrics) / len(metrics),
                damage=damages[0],
                battle_frames=items[0].battle_frames,
                result=items[0].result,
                seed=items[0].seed,
                expected=(sum(expected) / len(expected)) if all(e is not None for e in expected) else None,
                ub_frames=items[0].ub_frames,
                warnings=tuple(dict.fromkeys(w for item in items for w in item.warnings)),
                first_expected=items[0].first_expected,
            ))
        return outcomes

    def effect_frames(self, frames: FrameAxis) -> list[set[int]]:
        from .effects import effect_frames

        seed = self.seeds[0]
        request = self.request_for(frames, seed)
        request["CalcStats"] = FULL_CALC_STATS
        result = self._post([request])[0]
        try:
            self._outcome(result, seed, 1)
        except _StatsMissing:
            return [set() for _ in self.context["PlayerList"]]
        units = [int(player["unitId"]) for player in self.context["PlayerList"]]
        return effect_frames(result["Stats"], units)

    def evaluate_one(self, frames: FrameAxis) -> BattleOutcome:
        return self.evaluate([frames])[0]
