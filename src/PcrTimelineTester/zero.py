"""0 押轴：整场不打手动 UB，只在「TP 满了」的那些帧上决定按不按。

摸轴器没有「逐刀 AUTO / SET」的字段，能表达的只有：

* ``SetToggles``：每个角色一个 bool，true = 一有机会就按（游戏里的 SET）；
* ``IsAutoMode``：全局开关，true = 没被手动帧按掉的 UB 由自动逻辑自己找时机放。

所以这里把「逐刀 O / X」写成「在哪些 TP 满的帧上主动按」：

* 先打一遍纯 AUTO / 纯 SET，从回包的 ``Stats.TpChangeEvents`` 里拿到每个角色
  **TP 满的帧**（``CrossedReady``）——那就是「这一刀能不能 SET 出来」的判据；
* 某个角色第 k 刀选 ``O`` ⇒ 把它第 k 个 TP 满帧写进 ``UBExecTimeList``（到点就按）；
  选 ``X`` ⇒ 不写，交给 AUTO 兜底（``IsAutoMode=true``、``SetToggles`` 全 false）；
* 因为跳过的刀会让后面 TP 满的时机整体变化，每轮都用**最近一次回包的 TP 帧**重建帧表，
  最优解固定下来之后再用当时实际发送的帧表复核一遍。

自动 / SET 的刀数本来就随打法变化（实测同一轴：纯 AUTO 8 刀、纯 SET 9 刀、混排 7 刀也可能更多），
所以每个角色排多少刀不写死，跟着 TP 满帧的条数走，上限 ``ub_limit``。
"""

from __future__ import annotations

import math
import random
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, fields, replace
from typing import Any, Callable, Sequence

import httpx

from .roster import POST_WORKERS, lineup_order
from .savefile import SaveFile

TEAM_SIZE = 5
MAX_DEPTH = 32
# Core（Success / BattleFrames / Result）+ UBTime（实际放 UB 的帧）+ TpChange（TP 变化事件）
STATS_BITS = 1 | 2 | 256


class ZeroCancelled(RuntimeError):
    """用户中途停掉了 0 押轴。"""


Plan = tuple[tuple[bool, ...], ...]


def _station(save: SaveFile) -> tuple[int, ...]:
    units = tuple(int(unit) for unit in save.unit_ids)
    if len(units) != TEAM_SIZE:
        raise ValueError(f"这条轴有 {len(units)} 个角色，0 押轴要求正好 {TEAM_SIZE} 个")
    return lineup_order(units)


def _players_by_unit(save: SaveFile) -> dict[int, dict]:
    players: dict[int, dict] = {}
    for player in save.players:
        try:
            players[int(player["unitId"])] = player
        except (KeyError, TypeError, ValueError):
            continue
    return players


def plan_text(plan: Sequence[Sequence[bool]]) -> str:
    """把逐刀设定写成 `1 XOOO（之后一律 O）` 这种形式。"""
    rows = []
    for index, unit_plan in enumerate(plan, start=1):
        if not unit_plan:
            rows.append(f"{index} -")
            continue
        head = "".join("O" if flag else "X" for flag in unit_plan)
        tail = "O" if unit_plan[-1] else "X"
        rows.append(f"{index} {head}（之后一律 {tail}）")
    return "、".join(rows)


def plan_rows(plan: Sequence[Sequence[bool]]) -> list[str]:
    rows = []
    for index, unit_plan in enumerate(plan, start=1):
        if not unit_plan:
            rows.append(f"{index}: -（之后一律 X）")
            continue
        head = "".join("O" if flag else "X" for flag in unit_plan)
        tail = "O" if unit_plan[-1] else "X"
        rows.append(f"{index}: {head}（之后一律 {tail}）")
    return rows


def normalise(plan: Sequence[Sequence[bool]], *, depth: Sequence[int] | None = None) -> Plan:
    rows: list[tuple[bool, ...]] = []
    for index, unit_plan in enumerate(plan):
        limit = None if depth is None else max(0, int(depth[index]))
        flags = [bool(flag) for flag in unit_plan]
        if limit is not None:
            flags = flags[:limit]
        rows.append(tuple(flags))
    while len(rows) < TEAM_SIZE:
        rows.append(())
    return tuple(rows[:TEAM_SIZE])


def tp_ready_frames(payload: dict) -> tuple[tuple[int, ...], ...]:
    """从回包里取「TP 满」的帧，按站位（TargetLaneIndex）分组。"""
    stats = payload.get("Stats") or {}
    lanes: list[list[int]] = [[] for _ in range(TEAM_SIZE)]
    for event in stats.get("TpChangeEvents") or ():
        try:
            lane = int(event.get("TargetLaneIndex"))
        except (TypeError, ValueError):
            continue
        if not 0 <= lane < TEAM_SIZE:
            continue
        before = event.get("EnergyBefore")
        after = event.get("EnergyAfter")
        cap = event.get("EnergyCap")
        crossed = bool(event.get("CrossedReady"))
        if not crossed and None not in (before, after, cap):
            try:
                crossed = float(before) < float(cap) and float(after) >= float(cap)
            except (TypeError, ValueError):
                crossed = False
        if not crossed:
            continue
        try:
            frame = int(event.get("Frame"))
        except (TypeError, ValueError):
            continue
        if frame > 0 and (not lanes[lane] or lanes[lane][-1] != frame):
            lanes[lane].append(frame)
    return tuple(tuple(sorted(set(row))) for row in lanes)


def ub_frames(payload: dict) -> tuple[tuple[float, ...], ...]:
    stats = payload.get("Stats") or {}
    rows: list[tuple[float, ...]] = []
    for row in stats.get("UBTime") or ():
        try:
            rows.append(tuple(float(value) for value in row))
        except (TypeError, ValueError):
            rows.append(())
    while len(rows) < TEAM_SIZE:
        rows.append(())
    return tuple(rows[:TEAM_SIZE])


def build_lanes(plan: Sequence[Sequence[bool]], opportunities: Sequence[Sequence[int]]) -> list[list[float]]:
    """把逐刀设定翻成 `UBExecTimeList`：O 的刀取对应 TP 满帧，X 的刀不写（交给 AUTO）。"""
    lanes: list[list[float]] = []
    for index in range(TEAM_SIZE):
        unit_plan = plan[index] if index < len(plan) else ()
        frames = opportunities[index] if index < len(opportunities) else ()
        lane: list[float] = []
        for depth, flag in enumerate(unit_plan):
            if not flag or depth >= len(frames):
                continue
            lane.append(float(frames[depth]))
        lanes.append(sorted(lane))
    return lanes


def full_plan(opportunities: Sequence[Sequence[int]], *, limit: int) -> Plan:
    """全员「每个 TP 满帧都按」（= 纯 SET）的起点。"""
    rows = []
    for frames in opportunities:
        depth = min(int(limit), len(frames))
        rows.append(tuple(True for _ in range(depth)))
    return normalise(rows)


def mutate(
    plan: Plan,
    rng: random.Random,
    *,
    opportunities: Sequence[Sequence[int]],
    limit: int,
) -> Plan:
    """在当前解上改一小处：翻一刀 / 连翻两刀 / 多排一刀 / 少排一刀。"""
    rows = [list(row) for row in plan]
    while len(rows) < TEAM_SIZE:
        rows.append([])
    unit = rng.randrange(TEAM_SIZE)
    row = rows[unit]
    caps = [
        min(int(limit), max(len(opportunities[i]) if i < len(opportunities) else 0, len(rows[i])))
        for i in range(TEAM_SIZE)
    ]
    roll = rng.random()
    if roll < 0.18 and row:  # 把某角色从这一刀起整段改成同一种操作
        start = rng.randrange(len(row))
        row[start:] = [not row[start]] * (len(row) - start)
    elif roll < 0.36 and row and opportunities[unit]:
        # 同时改变两个 TP 到账时间最接近的队友操作。
        index = rng.randrange(min(len(row), len(opportunities[unit])))
        frame = opportunities[unit][index]
        choices = [(abs(at - frame), role, j) for role, other in enumerate(rows) if role != unit
                   for j, at in enumerate(opportunities[role][:len(other)])]
        if choices:
            _, role, j = min(choices)
            rows[role][j] = not rows[role][j]
        row[index] = not row[index]
    elif roll < 0.72:  # 翻一刀（或相邻两刀）
        if row:
            start = rng.randrange(len(row))
            span = 1 if rng.random() < 0.75 else 2
            for offset in range(span):
                index = start + offset
                if index < len(row):
                    row[index] = not row[index]
    elif roll < 0.86:  # 多排一刀
        if len(row) < caps[unit]:
            row.append(rng.random() < 0.5)
    else:  # 少排一刀
        if len(row) > 1:
            row.pop()
    rows[unit] = row
    return tuple(tuple(bool(flag) for flag in row) for row in rows[:TEAM_SIZE])


def temperatures(settings: "ZeroSettings", rounds: int) -> list[float]:
    """几何降温：返回每轮的温度比例（真正使用时再乘典型 |Δ|）。"""
    rounds = max(1, int(rounds))
    if rounds == 1:
        return [1.0]
    high = max(1e-6, float(settings.initial_temperature))
    low = max(1e-6, min(high, float(settings.final_temperature)))
    ratio = (low / high) ** (1.0 / (rounds - 1))
    return [high * (ratio ** index) for index in range(rounds)]


@dataclass(frozen=True, slots=True)
class ZeroSettings:
    endpoint: str = "http://127.0.0.1:8080/simulate"
    battle_time: int = 90
    seed: int = 666
    samples: int = 1
    timeout: float = 180.0
    to_max_attr: bool = False
    iterations: int = 1200
    batch: int = POST_WORKERS
    initial_temperature: float = 0.5
    final_temperature: float = 0.01
    ub_limit: int = 12
    verify: bool = True

    def __post_init__(self) -> None:
        if not 1 <= int(self.battle_time) <= 90:
            raise ValueError(f"战斗秒数要在 1~90 之间，实际 {self.battle_time}")
        if int(self.seed) < 1:
            raise ValueError(f"种子要不小于 1，实际 {self.seed}")
        if not 1 <= int(self.samples) <= 100:
            raise ValueError(f"采样次数要在 1~100 之间，实际 {self.samples}")
        if float(self.timeout) <= 0:
            raise ValueError(f"超时时间要大于 0，实际 {self.timeout}")
        if int(self.iterations) < 0:
            raise ValueError(f"退火轮数不能是负数，实际 {self.iterations}")
        if not 1 <= int(self.batch) <= 32:
            raise ValueError(f"每轮候选数要在 1~32 之间，实际 {self.batch}")
        if float(self.initial_temperature) < 0 or float(self.final_temperature) < 0:
            raise ValueError("温度不能是负数")
        if not 1 <= int(self.ub_limit) <= MAX_DEPTH:
            raise ValueError(f"每个角色最多排 1~{MAX_DEPTH} 刀，实际 {self.ub_limit}")

    def seeds(self) -> tuple[int, ...]:
        return tuple(int(self.seed) + index for index in range(max(1, int(self.samples))))

    def with_changes(self, **changes: Any) -> "ZeroSettings":
        return dataclass_replace(self, **changes)

    def to_dict(self) -> dict:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @classmethod
    def from_dict(cls, data: dict) -> "ZeroSettings":
        known = {field.name for field in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError("0押轴设置里有不认识的字段：" + "、".join(unknown))
        return cls(**{key: value for key, value in data.items() if key in known})


def dataclass_replace(instance: Any, **changes: Any) -> Any:
    values = {field.name: getattr(instance, field.name) for field in fields(instance)}
    values.update(changes)
    return type(instance)(**values)


def dataclass_fields(cls: Any) -> tuple[str, ...]:
    return tuple(field.name for field in fields(cls))


@dataclass(frozen=True, slots=True)
class ZeroScore:
    damage: float
    frames: int
    result: int
    plan: Plan
    opportunities: tuple[tuple[int, ...], ...] = ()
    lanes: tuple[tuple[float, ...], ...] = ()

    input_windows: tuple[tuple[int, ...], ...] = ()

    @property
    def is_win(self) -> bool:
        return int(self.result) == 0

    def to_dict(self) -> dict:
        return {
            "damage": float(self.damage),
            "frames": int(self.frames),
            "result": int(self.result),
            "plan": [list(row) for row in self.plan],
            "opportunities": [list(row) for row in self.opportunities],
            "lanes": [list(row) for row in self.lanes],
            "input_windows": [list(row) for row in self.input_windows],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ZeroScore":
        plan = tuple(tuple(bool(flag) for flag in row) for row in data.get("plan") or ())
        opps = tuple(tuple(int(frame) for frame in row) for row in data.get("opportunities") or ())
        lanes = tuple(tuple(float(frame) for frame in row) for row in data.get("lanes") or ())
        return cls(
            damage=float(data.get("damage") or 0.0),
            frames=int(data.get("frames") or 0),
            result=int(data.get("result") or 0),
            plan=plan,
            opportunities=opps,
            input_windows=tuple(tuple(row) for row in data.get("input_windows") or ()),
            lanes=lanes,
        )


class ZeroEvaluator:
    """按「哪些 TP 满帧要按」发请求，回包里取伤害 / UB 帧 / TP 满帧。"""

    def __init__(self, save: SaveFile, settings: ZeroSettings, *, log: Callable[[str], None] = print) -> None:
        self.save = save
        self.settings = settings
        self.log = log
        self.units = _station(save)
        self.players = _players_by_unit(save)
        self.battles = 0
        self.client = httpx.Client(timeout=float(settings.timeout))
        self._scores: OrderedDict[tuple, ZeroScore] = OrderedDict()

    def players_in_station_order(self) -> list[dict]:
        missing = [unit for unit in self.units if unit not in self.players]
        if missing:
            raise ValueError("轴里没有这些角色的数据：" + "、".join(str(unit) for unit in missing))
        return [self.players[unit] for unit in self.units]

    def request_for(self, lanes: Sequence[Sequence[float]], *, seed: int | None = None) -> dict:
        """手动帧 = 要按的 TP 满帧；其余交给 AUTO 兜底。"""
        seed = int(seed if seed is not None else self.settings.seed)
        body = self.save.request_context(battle_time=int(self.settings.battle_time), battle_seed=seed)
        body.update(
            PlayerList=self.players_in_station_order(),
            CalcStats=STATS_BITS,
            IsSkipping=True,
            ToMaxUnitAttr=bool(self.settings.to_max_attr),
            UBExecTimeList=[list(lane) for lane in lanes],
            SemanUBExecTimeList=[],
            SemanUBSequenceData=[],
            UseSequenceUB=False,
            IsSemanMode=False,
            IsAutoMode=True,
            IsSetMode=False,
            SetToggles=[False] * TEAM_SIZE,
            NeedOnceResult=True,
        )
        body.setdefault("Settings", {})["UseFixedRandomSeed"] = True
        body["Settings"]["RandomSeed"] = seed
        return body

    def request_pure(self, *, set_mode: bool, seed: int | None = None) -> dict:
        """纯 SET / 纯 AUTO 的对照请求（不带任何手动帧）。"""
        seed = int(seed if seed is not None else self.settings.seed)
        body = self.save.request_context(battle_time=int(self.settings.battle_time), battle_seed=seed)
        body.update(
            PlayerList=self.players_in_station_order(),
            CalcStats=STATS_BITS,
            IsSkipping=True,
            ToMaxUnitAttr=bool(self.settings.to_max_attr),
            UBExecTimeList=[],
            SemanUBExecTimeList=[],
            SemanUBSequenceData=[],
            UseSequenceUB=False,
            IsSemanMode=False,
            IsAutoMode=True,
            IsSetMode=False,
            SetToggles=[bool(set_mode)] * TEAM_SIZE,
            NeedOnceResult=True,
        )
        body.setdefault("Settings", {})["UseFixedRandomSeed"] = True
        body["Settings"]["RandomSeed"] = seed
        return body

    def _post(self, requests: Sequence[dict]) -> list[dict]:
        if not requests:
            return []
        blocks = [requests[i:i + 16] for i in range(0, len(requests), 16)]
        if len(blocks) == 1:
            rows = self._fetch(blocks[0])
        else:
            with ThreadPoolExecutor(max_workers=min(POST_WORKERS, len(blocks))) as pool:
                rows = [item for block in pool.map(self._fetch, blocks) for item in block]
        self.battles += len(rows)
        return rows

    def _fetch(self, block: Sequence[dict]) -> list[dict]:
        last: Exception | None = None
        for _attempt in range(2):
            try:
                response = self.client.post(self.settings.endpoint, json=list(block))
            except httpx.HTTPError as exc:
                last = exc
                continue
            if response.status_code != 200:
                raise ValueError(f"接口返回 {response.status_code}：{response.text[:200]}")
            try:
                payloads = response.json()
            except ValueError as exc:
                raise ValueError(f"接口回包不是 JSON：{response.text[:200]}") from exc
            if not isinstance(payloads, list):
                raise ValueError("接口回包必须是数组（请求是数组，回包也应是数组）")
            if len(payloads) != len(block):
                raise ValueError(f"接口回包条数与请求不一致：请求 {len(block)} 条，回包 {len(payloads)} 条")
            return payloads
        raise ValueError(f"接口请求失败（{last}）")

    def _score(self, payload: dict, request: dict) -> ZeroScore:
        if payload.get("Success") is not True:
            message = payload.get("Message") or "未知原因"
            raise ValueError(f"模拟失败：{message}")
        seed = int((request.get("Settings") or {}).get("RandomSeed") or 0)
        current = payload.get("CurrentSeed")
        if current is not None and seed and int(current) != seed:
            raise ValueError(f"回包种子对不上：请求 {seed}，回包 {current}")
        damage = (payload.get("OnceResult") or {}).get("exceptDamage")
        if not damage:
            damage = (payload.get("Stats") or {}).get("TotalDamage") or 0
        return ZeroScore(
            damage=float(damage or 0.0),
            frames=int(payload.get("BattleFrames") or 0),
            result=int(payload.get("Result") or 0),
            plan=(),
            opportunities=tp_ready_frames(payload),
        )

    def baseline(self) -> dict:
        """打一遍纯 SET / 纯 AUTO，拿对照伤害与初始 TP 满帧。"""
        seeds = self.settings.seeds()
        requests = [self.request_pure(set_mode=mode, seed=seed) for mode in (True, False) for seed in seeds]
        payloads = self._post(requests)
        pure_set, pure_auto = self._aggregate(requests, payloads)
        return {
            "set_damage": pure_set.damage,
            "set_frames": ub_frames(payloads[0]),
            "auto_damage": pure_auto.damage,
            "auto_frames": ub_frames(payloads[len(seeds)]),
            "opportunities": pure_set.opportunities,
        }

    def evaluate(self, plans: Sequence[Plan], opportunities: Sequence[Sequence[int]]) -> list[ZeroScore]:
        # 缓存键必须是实际发出的帧表，同一串 O/X 在不同 TP 满帧上可能是不同战斗。
        keys = [tuple(map(tuple, build_lanes(plan, opportunities))) for plan in plans]
        missing = list(dict.fromkeys(key for key in keys if key not in self._scores))
        self._scores.update(zip(missing, self.evaluate_lanes(missing), strict=True))
        windows = tuple(map(tuple, opportunities))
        scores = [replace(self._scores[key], plan=plan, input_windows=windows) for key, plan in zip(keys, plans)]
        for key in keys:
            self._scores.move_to_end(key)
        while len(self._scores) > 1024:
            self._scores.popitem(last=False)
        return scores

    def _aggregate(self, requests: Sequence[dict], payloads: Sequence[dict]) -> list[ZeroScore]:
        # 第一种子的 TP 满帧用于派生邻居，所有种子复用相同的手动帧表计算平均伤害。
        count = len(self.settings.seeds())
        scores = [self._score(payload, request) for payload, request in zip(payloads, requests, strict=True)]
        return [
            replace(scores[i], damage=sum(s.damage for s in scores[i:i + count]) / count)
            for i in range(0, len(scores), count)
        ]

    def evaluate_lanes(self, lanes: Sequence[Sequence[Sequence[float]]], *, plans: Sequence[Plan] | None = None) -> list[ZeroScore]:
        """按给定的帧表直接评估（复核时必须原样重发，才能重现同一场）。"""
        requests = [self.request_for(lane, seed=seed) for lane in lanes for seed in self.settings.seeds()]
        scores = []
        for index, score in enumerate(self._aggregate(requests, self._post(requests))):
            plan = plans[index] if plans is not None and index < len(plans) else ()
            scores.append(
                ZeroScore(
                    damage=score.damage,
                    frames=score.frames,
                    result=score.result,
                    plan=plan,
                    opportunities=score.opportunities,
                    lanes=tuple(tuple(float(frame) for frame in lane) for lane in lanes[index]),
                )
            )
        return scores

    def evaluate_one(self, plan: Plan, opportunities: Sequence[Sequence[int]]) -> ZeroScore:
        return self.evaluate([plan], opportunities)[0]

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "ZeroEvaluator":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class ZeroJob:
    """0 押轴：以「TP 满帧」为判据，逐刀退火决定按还是不按。"""

    def __init__(
        self,
        save: SaveFile,
        settings: ZeroSettings,
        *,
        log: Callable[[str], None] = print,
        evaluator: ZeroEvaluator | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> None:
        self.save = save
        self.settings = settings
        self.log = log
        self._cancel = cancel
        self._own = evaluator is None
        self.evaluator = evaluator or ZeroEvaluator(save, settings, log=log)
        self.units = tuple(getattr(self.evaluator, "units", ())) or _station(save)
        self.names: dict[int, str] = dict(getattr(save, "names", None) or {})
        self.rounds_total = max(1, int(settings.iterations) + 1)
        self.rounds_done = 0
        self.step_scale = 0.0
        self.battles_done = 0
        self.baseline: dict = {}
        self.opportunities: tuple[tuple[int, ...], ...] = tuple(() for _ in range(TEAM_SIZE))
        self.best: ZeroScore | None = None
        self.current: ZeroScore | None = None
        self.history: list[dict] = []
        self.model_rng = random.Random(int(settings.seed) * 7919 + 17)
        self.cancelled = False
        self.export: dict | None = None

    # ---------- 生命周期 ----------

    def cancel_run(self) -> None:
        self.cancelled = True

    @property
    def cancelled_requested(self) -> bool:
        return bool(self.cancelled or (self._cancel is not None and self._cancel()))

    def close(self) -> None:
        if self._own:
            self.evaluator.close()

    def __enter__(self) -> "ZeroJob":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---------- 主流程 ----------

    def run(self) -> dict:
        settings = self.settings
        seeds = settings.seeds()
        self.log("=== 0 押轴：退火 {} 轮 × 每轮 {} 个候选 × {} 个种子 ===".format(
            int(settings.iterations), int(settings.batch), len(seeds)
        ))
        self.log("站位：" + self._station_text())
        started = time.perf_counter()
        limit = int(settings.ub_limit)
        prior_battles = self.battles_done - self.evaluator.battles
        if self.cancelled_requested:
            self.cancelled = True
            return self.summary(0.0)
        if self.current is None or self.best is None:
            self.baseline = self.evaluator.baseline()
            self.opportunities = tuple(tuple(row) for row in self.baseline["opportunities"])
            self.log("基线：纯 SET {:,} ／ 纯 AUTO {:,}".format(
                int(self.baseline["set_damage"]), int(self.baseline["auto_damage"])
            ))
            self.log("TP 满帧：" + self._opportunity_text())
            start = full_plan(self.opportunities, limit=limit)
            auto = tuple(tuple(False for _ in row) for row in start)
            openings = [start, auto]
            for role, row in enumerate(start):
                if row:
                    openings.append(start[:role] + (auto[role],) + start[role + 1:])
                    openings.append(auto[:role] + (row,) + auto[role + 1:])
            starts = self.evaluator.evaluate(list(dict.fromkeys(openings)), self.opportunities)
            first = max(starts, key=lambda score: score.damage)
            self.opportunities = first.opportunities or self.opportunities
            self.best = first
            self.current = max(starts[:2], key=lambda score: score.damage)
            self.rounds_done = 1
            self._log_round(1, first, improved=True)
            self._push_history(1, first, first)
        self.battles_done = prior_battles + self.evaluator.battles

        schedule = temperatures(settings, max(1, int(settings.iterations)))
        for round_index in range(self.rounds_done, int(settings.iterations) + 1):
            if self.cancelled_requested:
                self.cancelled = True
                self.log("已停止：退火到第 {} 轮".format(self.rounds_done))
                break
            anchor = self.best if round_index % 2 == 0 else self.current
            windows = anchor.opportunities or self.opportunities
            if anchor.input_windows and round_index % 4 in (1, 2):
                windows = anchor.input_windows
            proposals = {}
            for _ in range(settings.batch * 4):
                plan = mutate(anchor.plan, self.model_rng, opportunities=windows, limit=limit)
                lanes = tuple(map(tuple, build_lanes(plan, windows)))
                if lanes != anchor.lanes:
                    proposals.setdefault(lanes, plan)
                if len(proposals) == settings.batch:
                    break
            scores = self.evaluator.evaluate(list(proposals.values()), windows) if proposals else [anchor]
            self.battles_done = prior_battles + self.evaluator.battles
            self.rounds_done = round_index + 1
            pick = max(scores, key=lambda score: (score.damage, score.frames))
            if self.current is not None:
                delta = pick.damage - self.current.damage
                self.step_scale = 0.85 * self.step_scale + 0.15 * abs(delta)
                temperature = schedule[round_index - 1] * max(self.step_scale, 1.0)
                accept = delta >= 0 or (temperature > 0 and self.model_rng.random() < math.exp(max(-50.0, delta / temperature)))
            else:
                accept = True
            if accept:
                self.current = pick
                self.opportunities = pick.opportunities or self.opportunities
            improved = self.best is None or (pick.damage, pick.frames) > (self.best.damage, self.best.frames)
            if improved:
                self.best = pick
            self._log_round(self.rounds_done, pick, improved=improved)
            self._push_history(self.rounds_done, pick, self.best or pick)
            if round_index % max(1, int(settings.iterations) // 20 or 1) == 0:
                self.log("--- 第 {}/{} 轮：当前 {:,} ／ 最优 {:,} ---".format(
                    self.rounds_done, self.rounds_total,
                    int(self.current.damage) if self.current else 0,
                    int(self.best.damage) if self.best else 0,
                ))

        self.cancelled = self.cancelled_requested
        if self.best is not None and settings.verify and not self.cancelled:
            if self.best.lanes:
                again = self.evaluator.evaluate_lanes([self.best.lanes])[0]
            else:
                again = self.evaluator.evaluate_one(self.best.plan, self.best.opportunities or self.opportunities)
            self.battles_done = prior_battles + self.evaluator.battles
            if abs(again.damage - self.best.damage) < 0.5 and again.frames == self.best.frames:
                self.log("复核通过：{:,}（同一次模拟）".format(int(again.damage)))
            else:
                self.log("复核不一致：{:,} → {:,}，以复核为准".format(int(self.best.damage), int(again.damage)))
                self.best = ZeroScore(
                    damage=again.damage, frames=again.frames, result=again.result,
                    plan=self.best.plan, opportunities=again.opportunities or self.best.opportunities,
                    lanes=self.best.lanes or again.lanes,
                )

        if self.best is None:
            raise ValueError("0 押轴没有跑出任何结果")
        best = self.best
        self.log("=== 最优：{:,}（{} 帧 / {}） ===".format(
            int(best.damage), int(best.frames), self._result_text(best.result)
        ))
        for line in plan_rows(best.plan):
            index = int(line.split(":")[0]) - 1
            name = self.names.get(self.units[index], str(self.units[index])) if index < len(self.units) else ""
            self.log("    {} {}".format(name, line))
        self.cancelled = self.cancelled_requested
        if not self.cancelled:
            self.export = {
                "plan": [list(row) for row in best.plan],
                "damage": int(best.damage),
                "frames": int(best.frames),
                "result": int(best.result),
                "lanes": [list(lane) for lane in (best.lanes or build_lanes(best.plan, best.opportunities or self.opportunities))],
            }
        self.elapsed = time.perf_counter() - started
        return self.summary()

    # ---------- 记录 ----------

    def _log_round(self, round_index: int, score: ZeroScore, *, improved: bool) -> None:
        mark = "  新的最优" if improved else ""
        self.log("[{}/{}] {} → {:,}（{} 帧 / {}）{}".format(
            min(self.rounds_done, self.rounds_total), self.rounds_total,
            plan_text(score.plan), int(score.damage), int(score.frames),
            self._result_text(score.result), mark,
        ))

    def _push_history(self, round_index: int, score: ZeroScore, best: ZeroScore) -> None:
        self.history.append({
            "round": int(round_index),
            "damage": int(score.damage),
            "frames": int(score.frames),
            "result": int(score.result),
            "plan": [list(row) for row in score.plan],
            "text": plan_text(score.plan),
            "best": int(best.damage),
        })
        if len(self.history) > 4000:
            del self.history[:1000]

    def _station_text(self) -> str:
        rows = []
        for index, unit in enumerate(self.units, start=1):
            rows.append("{} {}".format(index, self.names.get(unit, str(unit))))
        return "、".join(rows)

    def _opportunity_text(self) -> str:
        rows = []
        for index, unit in enumerate(self.units, start=1):
            frames = self.opportunities[index - 1] if index - 1 < len(self.opportunities) else ()
            rows.append("{} {} 满 {} 次".format(index, self.names.get(unit, str(unit)), len(frames)))
        return "、".join(rows)

    def _result_text(self, result: int) -> str:
        return {0: "胜利", 1: "失败", 2: "超时", 3: "撤退"}.get(int(result), "未知")

    # ---------- 状态 ----------

    def state_dict(self) -> dict:
        state = self.model_rng.getstate()
        return {
            "mode": "zero",
            "rounds_done": int(self.rounds_done),
            "rounds_total": int(self.rounds_total),
            "battles": int(self.battles_done),
            "best": self.best.to_dict() if self.best else None,
            "current": self.current.to_dict() if self.current else None,
            "baseline": {
                "set_damage": float(self.baseline.get("set_damage") or 0.0),
                "auto_damage": float(self.baseline.get("auto_damage") or 0.0),
            },
            "opportunities": [list(row) for row in self.opportunities],
            "history": list(self.history[-200:]),
            "rng": [int(state[0]), list(int(value) for value in state[1])],
            "samples": self.settings.samples,
            "step_scale": self.step_scale,
        }

    def apply_state(self, state: dict) -> None:
        if not isinstance(state, dict) or state.get("mode") != "zero":
            raise ValueError("状态文件和 0 押轴对不上")
        if int(state.get("samples", 1)) != self.settings.samples:
            self.log("续跑：旧成绩的种子数不同，重新按当前种子数评估。")
            return
        self.step_scale = float(state.get("step_scale", 0.0))
        self.rounds_done = int(state.get("rounds_done") or 0)
        self.battles_done = int(state.get("battles") or 0)
        if state.get("best"):
            self.best = ZeroScore.from_dict(state["best"])
        if state.get("current"):
            self.current = ZeroScore.from_dict(state["current"])
        baseline = state.get("baseline") or {}
        self.baseline = {
            "set_damage": float(baseline.get("set_damage") or 0.0),
            "auto_damage": float(baseline.get("auto_damage") or 0.0),
        }
        opportunities = state.get("opportunities") or ()
        if opportunities:
            self.opportunities = tuple(tuple(int(frame) for frame in row) for row in opportunities)
        self.history = list(state.get("history") or [])
        rng = state.get("rng")
        if isinstance(rng, list) and len(rng) == 2:
            try:
                self.model_rng.setstate((int(rng[0]), tuple(int(value) for value in rng[1]), None))
            except (TypeError, ValueError):
                pass

    def summary(self, elapsed: float | None = None) -> dict:
        best = self.best
        planned = (max(1, int(self.settings.iterations)) + 1) * max(1, int(self.settings.batch)) * len(self.settings.seeds())
        return {
            "mode": "zero",
            "excel": str(getattr(self.save, "path", "")),
            "endpoint": self.settings.endpoint,
            "battle_time": int(self.settings.battle_time),
            "seed": int(self.settings.seed),
            "samples": int(self.settings.samples),
            "to_max_attr": bool(self.settings.to_max_attr),
            "iterations": int(self.settings.iterations),
            "batch": int(self.settings.batch),
            "ub_limit": int(self.settings.ub_limit),
            "units": [int(unit) for unit in self.units],
            "rounds_total": int(self.rounds_total),
            "rounds_done": int(self.rounds_done),
            "planned": int(planned),
            "battles": int(self.battles_done),
            "final": bool(self.best is not None and (self.current is None or self.best.damage >= self.current.damage)),
            "cancelled": bool(self.cancelled),
            "baseline": {
                "set_damage": float(self.baseline.get("set_damage") or 0.0),
                "auto_damage": float(self.baseline.get("auto_damage") or 0.0),
            },
            "opportunities": [list(row) for row in self.opportunities],
            "best": {
                "plan": [list(row) for row in (best.plan if best else ())],
                "text": plan_text(best.plan) if best else "",
                "rows": plan_rows(best.plan) if best else [],
                "damage": int(best.damage) if best else 0,
                "frames": int(best.frames) if best else 0,
                "result": int(best.result) if best else 0,
            },
            "history": list(self.history),
            "export": self.export,
            "elapsed": float(elapsed if elapsed is not None else getattr(self, "elapsed", 0.0) or 0.0),
        }


def zero_top(summary: dict, limit: int = 8) -> list[dict]:
    """刷新记录里伤害最高的几次（去重按伤害+帧）。"""
    rows = list(summary.get("history") or [])
    seen: set[tuple[int, int]] = set()
    picked: list[dict] = []
    for row in sorted(rows, key=lambda item: (-int(item.get("damage") or 0), int(item.get("frames") or 0))):
        key = (int(row.get("damage") or 0), int(row.get("frames") or 0))
        if key in seen:
            continue
        seen.add(key)
        picked.append(row)
        if len(picked) >= max(1, int(limit)):
            break
    return picked
