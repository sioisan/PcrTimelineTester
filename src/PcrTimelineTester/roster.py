"""阵容搜索：把候选池里的队伍组合**全部排一遍**（含分组配额），每队试 2^n 种 AUTO/SET。

设计要点：

* 队伍固定 5 人（摸轴器的公会战接口只吃 5 人），不再有迭代/温度这类参数；
* 角色可以分组（A 组至少 2 人、B 组 1 人……），剩下的名额从未分组的人里补齐；
  组员只在组内换，未分组的人只在未分组里换，这样「A 组两个人 + B 组一个人 + 未分组两个人」
  的所有排列都会被枚举到；
* 每支队伍都把「每个人 AUTO / SET」的 2^n 种组合各打一场（`:class:`TeamEvaluator`），
  取伤害最高的那种作为这支队的分，并记下每人的操作；
* 中途取消不导出结果，靠 ``state.json`` 里的 cursor 续跑。
"""

from __future__ import annotations

import itertools
import math
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterator, Sequence

from concurrent.futures import ThreadPoolExecutor

import httpx

from .client import TuneError, simulation_url
from .datas import unit_width
from .savefile import SaveFile

ROLE_KEYS = ("unitId", "level", "rarity", "rank", "equipLevel", "skillLevel")
DEFAULT_ENDPOINT = "http://127.0.0.1:8080/simulate"
TEAM_SIZE = 5
POST_WORKERS = 4  # 同时发几个请求（服务端本身是多线程的，串行发吃不满 CPU）
RESULT_LIMIT = 100


class RosterCancelled(RuntimeError):
    """用户点了停止。"""


# ---------------------------------------------------------------- 设置
@dataclass(frozen=True, slots=True)
class GroupSpec:
    """一个分组：成员 + 最少要上几个人（0 = 不强制，成员算进未分组）。"""

    name: str
    members: tuple[int, ...] = ()
    minimum: int = 0

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        if not name:
            raise ValueError("分组名不能为空")
        object.__setattr__(self, "name", name)
        members = tuple(dict.fromkeys(int(unit) for unit in self.members))
        object.__setattr__(self, "members", members)
        if int(self.minimum) < 0:
            raise ValueError(f"分组 {name} 的最少人数不能是负数")
        object.__setattr__(self, "minimum", int(self.minimum))
        if self.minimum > len(members):
            raise ValueError(f"分组 {name} 只有 {len(members)} 个角色，最少人数不能大于它")

    @property
    def used(self) -> bool:
        """只有 minimum > 0 的分组才参与配额。"""
        return self.minimum > 0

    @property
    def label(self) -> str:
        if not self.used:
            return f"{self.name}（{len(self.members)} 人，不限）"
        return f"{self.name}（{len(self.members)} 人里至少 {self.minimum} 人）"

    def to_dict(self) -> dict:
        return {"name": self.name, "members": list(self.members), "minimum": self.minimum}

    @classmethod
    def from_dict(cls, data: dict) -> "GroupSpec":
        return cls(
            name=str(data.get("name") or ""),
            members=tuple(int(unit) for unit in (data.get("members") or ())),
            minimum=int(data.get("minimum") or 0),
        )


@dataclass(frozen=True, slots=True)
class RosterSettings:
    endpoint: str = DEFAULT_ENDPOINT
    battle_time: int = 90
    seed: int = 666
    samples: int = 1
    batch_size: int = 32
    timeout: float = 180.0
    pool: tuple[int, ...] = ()
    groups: tuple[GroupSpec, ...] = ()
    to_max_attr: bool = True
    verify: bool = True
    every_combo: bool = True

    def __post_init__(self) -> None:
        if not 1 <= int(self.battle_time) <= 90:
            raise ValueError("战斗秒数必须在 1~90 之间")
        if not 1 <= int(self.samples) <= 100:
            raise ValueError("采样种子数必须在 1~100 之间")
        if not 1 <= int(self.batch_size) <= 512:
            raise ValueError("单次场次必须在 1~512 之间")
        if float(self.timeout) <= 0:
            raise ValueError("超时秒必须大于 0")
        pool = tuple(dict.fromkeys(int(unit) for unit in self.pool))
        object.__setattr__(self, "pool", pool)
        if len(pool) < TEAM_SIZE:
            raise ValueError(f"候选角色至少要有 {TEAM_SIZE} 个（当前 {len(pool)} 个）")
        groups = tuple(self.groups)
        names = [group.name for group in groups]
        if len(set(names)) != len(names):
            raise ValueError("分组名不能重复")
        members: list[int] = []
        for group in groups:
            for unit in group.members:
                if unit not in pool:
                    raise ValueError(f"分组 {group.name} 里的角色 {unit} 不在候选池里")
            members.extend(group.members)
        if len(set(members)) != len(members):
            raise ValueError("同一个角色不能出现在两个分组里")
        quota = sum(group.minimum for group in groups if group.used)
        if quota > TEAM_SIZE:
            raise ValueError(f"各分组的最少人数加起来是 {quota}，超过了 {TEAM_SIZE} 人")
        object.__setattr__(self, "groups", groups)

    # -------------------------------------------------------- 派生
    def seeds(self) -> tuple[int, ...]:
        seed = int(self.seed)
        return (seed,) + tuple((abs(seed) + index) % (2**31 - 1) for index in range(1, int(self.samples)))

    def to_dict(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "battle_time": int(self.battle_time),
            "seed": int(self.seed),
            "samples": int(self.samples),
            "batch_size": int(self.batch_size),
            "timeout": float(self.timeout),
            "pool": list(self.pool),
            "groups": [group.to_dict() for group in self.groups],
            "to_max_attr": bool(self.to_max_attr),
            "verify": bool(self.verify),
            "every_combo": bool(self.every_combo),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RosterSettings":
        known = set(cls.__slots__)
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"配置里有不认识的字段：{'、'.join(sorted(unknown))}")
        return cls(
            endpoint=str(data.get("endpoint") or DEFAULT_ENDPOINT),
            battle_time=int(data.get("battle_time") or 90),
            seed=int(data.get("seed") if data.get("seed") is not None else 666),
            samples=int(data.get("samples") or 1),
            batch_size=int(data.get("batch_size") or 32),
            timeout=float(data.get("timeout") or 180.0),
            pool=tuple(int(unit) for unit in (data.get("pool") or ())),
            groups=tuple(
                GroupSpec.from_dict(item) if isinstance(item, dict) else GroupSpec(str(item))
                for item in (data.get("groups") or ())
            ),
            to_max_attr=bool(data.get("to_max_attr", True)),
            verify=bool(data.get("verify", True)),
            every_combo=bool(data.get("every_combo", True)),
        )

    def with_changes(self, **changes) -> "RosterSettings":
        return replace(self, **changes)


# ---------------------------------------------------------------- 枚举
def fixed_groups(settings: RosterSettings) -> tuple[GroupSpec, ...]:
    """真正参与配额的分组：设了最少人数，且和候选池有交集。"""
    return tuple(
        group
        for group in settings.groups
        if group.used and group_members(settings, group)
    )


def group_members(settings: RosterSettings, group: GroupSpec) -> tuple[int, ...]:
    """分组里同时也在候选池里的角色（分组本身可以写得比候选池大）。"""
    pool = set(settings.pool)
    return tuple(sorted(unit for unit in group.members if unit in pool))


def grouped_units(settings: RosterSettings) -> set[int]:
    """所有被某个生效分组覆盖的候选角色。"""
    return {unit for group in fixed_groups(settings) for unit in group_members(settings, group)}


def ungrouped(settings: RosterSettings) -> tuple[int, ...]:
    """候选池里不属于任何分组的角色（补位用）。"""
    taken = grouped_units(settings)
    return tuple(unit for unit in settings.pool if unit not in taken)


def team_quota(settings: RosterSettings) -> int:
    """所有分组要占掉的座位数（没设最少人数的分组不算）。"""
    return sum(group.minimum for group in fixed_groups(settings))


def team_count(settings: RosterSettings) -> int:
    """一共要打多少支队伍（每支队伍再乘 32 种 AUTO/SET）。"""
    return sum(1 for _ in iter_teams(settings))


def _group_pools(settings: RosterSettings) -> list[list[tuple[int, ...]]]:
    pools: list[list[tuple[int, ...]]] = []
    for group in fixed_groups(settings):
        members = group_members(settings, group)
        if len(members) < group.minimum:
            pools.append([])
            continue
        pools.append([tuple(part) for part in itertools.combinations(members, group.minimum)])
    return pools


def iter_teams(settings: RosterSettings) -> Iterator[tuple[int, ...]]:
    """枚举所有 5 人队。

    规则（按需求）：
    - 分组只看「设了最少人数」的组；组里同时也在候选池里的角色才算这个组的人。
    - 每个组先抽够自己的最少人数。
    - 候选池里不属于任何分组的角色用来补满剩下的座位。
    - 分组之间允许重叠；抽出来的人重复（同一个角色上两次）或整队重复的排列会被跳过，
      其余排列全部跑一遍。
    """
    quotas = fixed_groups(settings)
    quota = team_quota(settings)
    rest = TEAM_SIZE - quota
    if rest < 0:
        return
    pools = _group_pools(settings)
    if any(not pool for pool in pools):
        return
    free = ungrouped(settings)
    if len(free) < rest:
        return
    extra_pool = list(itertools.combinations(free, rest))
    seen: set[frozenset[int]] = set()
    for chosen in itertools.product(*pools) if pools else [()]:
        used: list[int] = [unit for part in chosen for unit in part]
        if len(set(used)) != len(used):
            continue  # 组之间抽到了同一个角色：这个排列不成立
        for extra in extra_pool:
            team = tuple(used) + tuple(extra)
            if len(set(team)) != TEAM_SIZE:
                continue
            key = frozenset(team)
            if key in seen:
                continue
            seen.add(key)
            yield team


# ---------------------------------------------------------------- 评估
@dataclass(frozen=True, slots=True)
class TeamScore:
    damage: int
    frames: int
    result: int
    toggles: tuple[bool, ...] = ()

    @property
    def is_win(self) -> bool:
        return self.result == 0

    def to_dict(self) -> dict:
        return {
            "damage": int(self.damage),
            "frames": int(self.frames),
            "result": int(self.result),
            "toggles": [bool(flag) for flag in self.toggles],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TeamScore":
        return cls(
            damage=int(data.get("damage") or 0),
            frames=int(data.get("frames") or 0),
            result=int(data.get("result") if data.get("result") is not None else -1),
            toggles=tuple(bool(flag) for flag in (data.get("toggles") or ())),
        )


def toggle_text(toggles: Sequence[bool]) -> str:
    """展开写：O = SET（一有机会就按）、X = AUTO（角色自己找时机）。"""
    return "、".join("SET（O）" if flag else "AUTO（X）" for flag in toggles) or "全部 AUTO"


def combo_text(toggles: Sequence[bool]) -> str:
    """紧凑写法：O = SET、X = AUTO（以前用 S / A，容易和「上/下」等说法混）。"""
    return "".join("O" if flag else "X" for flag in toggles)


# ------------------------------------------------------- 站位（真实排列）
def lineup_order(units: Sequence[int], *, width=None) -> tuple[int, ...]:
    """按游戏里的真实站位排（1 号位在最前）。

    战斗日志回放（``ExcelHelper.cs`` 里 ``OrderBy(searchAreaWidth).ThenBy(unitId)``）
    就是这么排的，所以「攻击范围小的在前、相同的按 unitId」＝游戏里的站位顺序。
    队伍里的排列顺序只是我们枚举出来的先后，跟站位无关。
    """
    getter = width or unit_width

    def key(unit: int) -> tuple[int, int]:
        value = getter(unit)
        return (int(value) if value is not None else 10**6 + int(unit), int(unit))

    return tuple(sorted((int(unit) for unit in units), key=key))


def _flag_of(units: Sequence[int], toggles: Sequence[bool], unit: int) -> bool:
    pairs = [int(item) for item in units]
    if int(unit) not in pairs:
        return False
    index = pairs.index(int(unit))
    return bool(toggles[index]) if index < len(toggles) else False


def lineup_names(units: Sequence[int], names: dict[int, str] | None = None, *, width=None) -> str:
    """按站位排好的名字串。"""
    labels = names or {}
    return "、".join(str(labels.get(int(unit)) or unit) for unit in lineup_order(units, width=width))


def lineup_combo(units: Sequence[int], toggles: Sequence[bool], *, width=None) -> str:
    """紧凑 OX，但按站位排（不是队伍里的排列顺序）。"""
    flags = [_flag_of(units, toggles, unit) for unit in lineup_order(units, width=width)]
    return combo_text(flags)


def lineup_text(
    units: Sequence[int],
    toggles: Sequence[bool],
    names: dict[int, str] | None = None,
    *,
    width=None,
) -> str:
    """「1 花音 SET（O）、2 纳娜 AUTO（X）…」，按站位排，带站位号。"""
    labels = names or {}
    parts = []
    for position, unit in enumerate(lineup_order(units, width=width), start=1):
        label = str(labels.get(int(unit)) or unit)
        parts.append(f"{position} {label} {toggle_text((_flag_of(units, toggles, unit),))}")
    return "、".join(parts)


class TeamEvaluator:
    """把一支队伍发给摸轴器打一场（满属性），每队再试 2^n 种 AUTO/SET。"""

    def __init__(self, save: SaveFile, settings: RosterSettings, *, log=print):
        self.save = save
        self.settings = settings
        self.log = log
        self.endpoint = simulation_url(settings.endpoint)
        self.battles = 0
        self._client = httpx.Client(timeout=float(settings.timeout))
        self._template = [dict(player) for player in save.players]

    # -------------------------------------------------------- 出口
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TeamEvaluator":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -------------------------------------------------------- 组请求
    def team_players(self, units: Sequence[int]) -> list[dict]:
        players = []
        for index, unit in enumerate(units):
            template = self._template[index % len(self._template)]
            player = {key: template[key] for key in ROLE_KEYS if key in template}
            player["unitId"] = int(unit)
            players.append(player)
        return players

    def combos(self, size: int = TEAM_SIZE) -> list[tuple[bool, ...]]:
        """一支队伍要试的 AUTO / SET 组合。

        ``every_combo`` 打开时是每人两种、5 人共 32 种；关掉时默认**全 SET**。

        实测（8099，同一支队伍、同一个种子、都按满属性算）：
        - 不带帧轴 + 全 SET（O）= 100,978,474 —— `SetToggles` 只决定 `pressing`，和帧轴无关
        - 不带帧轴 + 全 AUTO（X）= 149,119,769 —— 要开 `IsAutoMode`，角色才会自己按 UB
        - 不带帧轴 + 既不开 AUTO 也不 SET = 40,505,442 —— 根本不放 UB，所以 AUTO 必须开 `IsAutoMode`
        - 带轴里的帧 = 363,472,384 —— 那是「帧模式」的手动刀表，不是 AUTO / SET，阵容搜索一律不用
        """
        size = max(1, int(size))
        if not self.settings.every_combo:
            # 不逐个试：默认全 SET（SET = 一有机会就按，和帧轴无关）
            return [(True,) * size]
        return [tuple(bool(flag) for flag in flags) for flags in itertools.product((False, True), repeat=size)]

    def request_for(self, units: Sequence[int], seed: int | None = None,
                   toggles: Sequence[bool] = ()) -> dict:
        # SetToggles[i] 在服务器里是按「站位槽位」生效的（内部按 searchAreaWidth 排序后
        # ``playerUnitCtrl[i].pressing = allSet || setToggles[i]``），跟 PlayerList 里的先后无关。
        # 实测：同一组 toggles 换队伍顺序，伤害只跟「第几个位置是 true」有关（只花音 SET =
        # 只涅妃 SET 当两者都排在第 1 位时结果完全相同）。所以请求一律按站位排、toggles 跟着排，
        # 这样「第 K 位是 O」就真的是那个站位的角色 SET。
        order = lineup_order(tuple(int(unit) for unit in units))
        flags = tuple(_flag_of(units, toggles, unit) for unit in order)
        body = self.save.request_context(
            battle_time=int(self.settings.battle_time),
            battle_seed=int(self.settings.seed if seed is None else seed),
        )
        body.update(
            PlayerList=self.team_players(order),
            CalcStats=1,
            IsSkipping=True,
            ToMaxUnitAttr=bool(self.settings.to_max_attr),
            # 轴里的帧是「帧模式」的手动刀表，不是 AUTO / SET，阵容搜索一律不带
            UBExecTimeList=[],
            SemanUBExecTimeList=[],
            SemanUBSequenceData=[],
            UseSequenceUB=False,
            IsSemanMode=False,
            # 有一个人是 AUTO 就开 AUTO 模式：没设 SET 的靠它自己按（全 SET 时开不开都一样）
            IsAutoMode=any(not bool(flag) for flag in flags),
            IsSetMode=False,
            SetToggles=[bool(flag) for flag in flags],
            NeedOnceResult=False,
        )
        return body

    # -------------------------------------------------------- 打分
    def evaluate_one(self, units: Sequence[int], toggles: Sequence[bool]) -> TeamScore:
        """只打一支队伍的一种组合（界面「试这队」用）。"""
        scores = self.evaluate([tuple(int(unit) for unit in units)], combos_override=[tuple(toggles)])
        return scores[0]

    def evaluate(
        self,
        teams: Sequence[Sequence[int]],
        combos_override: Sequence[Sequence[bool]] | None = None,
    ) -> list[TeamScore]:
        """每支队伍试完所有 AUTO/SET 组合（×种子），取伤害最高的那种。"""
        plans: list[tuple[int, tuple[bool, ...]]] = []
        requests: list[dict] = []
        combos = (
            [tuple(bool(flag) for flag in combo) for combo in combos_override]
            if combos_override is not None
            else self.combos(TEAM_SIZE)
        )
        seeds = self.settings.seeds()
        for index, team in enumerate(teams):
            for combo in combos:
                plans.append((index, combo))
                for seed in seeds:
                    requests.append(self.request_for(team, seed, combo))
        replies = self._post(requests)
        best: dict[int, TeamScore] = {}
        cursor = 0
        for index, combo in plans:
            damages = []
            frames = 0
            worst = -1
            for _ in seeds:
                reply = replies[cursor]
                cursor += 1
                damages.append(int(reply["damage"]))
                frames = max(frames, int(reply["frames"]))
                worst = max(worst, int(reply["result"]))
            damage = sum(damages) // len(damages)
            current = best.get(index)
            if current is None or damage > current.damage:
                best[index] = TeamScore(damage, frames, worst, combo)
        return [best[index] for index in range(len(teams))]

    def evaluate_all(self, units: Sequence[int]) -> list[tuple[tuple[bool, ...], TeamScore]]:
        """把一支队伍的每种 AUTO/SET 都打一遍，返回 [(组合, 分数)]（按伤害降序）。

        请求一次性并发发出去（和搜索时同一条通道），比一种组合发一次快得多。
        """
        team = tuple(int(unit) for unit in units)
        if len(team) != TEAM_SIZE:
            raise ValueError(f"队伍必须是 {TEAM_SIZE} 个人（现在是 {len(team)} 个）")
        if len(set(team)) != len(team):
            raise ValueError("同一支队伍里不能有重复角色")
        combos = self.combos(TEAM_SIZE)
        seeds = self.settings.seeds()
        requests: list[dict] = []
        for combo in combos:
            for seed in seeds:
                requests.append(self.request_for(team, seed, combo))
        replies = self._post(requests)
        results: list[tuple[tuple[bool, ...], TeamScore]] = []
        cursor = 0
        for combo in combos:
            damages = []
            frames = 0
            worst = -1
            for _ in seeds:
                reply = replies[cursor]
                cursor += 1
                damages.append(int(reply["damage"]))
                frames = max(frames, int(reply["frames"]))
                worst = max(worst, int(reply["result"]))
            damage = sum(damages) // len(damages)
            results.append((combo, TeamScore(damage, frames, worst, combo)))
        results.sort(key=lambda item: -int(item[1].damage))
        return results

    def _post(self, requests: list[dict]) -> list[dict]:
        """按 batch_size 拆成若干批并行发（POST_WORKERS 个），回包顺序与请求一致。"""
        if not requests:
            return []
        batch = max(1, int(self.settings.batch_size))
        chunks = [requests[index:index + batch] for index in range(0, len(requests), batch)]
        workers = max(1, min(POST_WORKERS, len(chunks)))
        if workers == 1:
            payloads = [self._fetch(chunk) for chunk in chunks]
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="roster-post") as pool:
                payloads = list(pool.map(self._fetch, chunks))
        results: list[dict] = []
        for chunk, payload in zip(chunks, payloads):
            for offset, item in enumerate(payload):
                results.append(self._item(item, chunk[offset], len(results)))
            self.battles += len(chunk)
        return results

    def _fetch(self, chunk: list[dict]) -> list[dict]:
        try:
            reply = self._client.post(self.endpoint, json=chunk)
        except httpx.HTTPError as exc:
            raise TuneError(f"接口请求失败（{type(exc).__name__}：{exc}）") from exc
        if reply.status_code >= 400:
            raise TuneError(f"接口返回 HTTP {reply.status_code}：{reply.text[:200]}")
        try:
            payload = reply.json()
        except ValueError as exc:
            raise TuneError("接口回包无法解析为 JSON") from exc
        if not isinstance(payload, list):
            raise TuneError("接口回包必须是数组（请求是数组，回包也应是数组）")
        if len(payload) != len(chunk):
            raise TuneError(f"接口返回数量与请求不匹配（请求 {len(chunk)} 项，回包 {len(payload)} 项）")
        return payload

    def _item(self, item: dict, request: dict, index: int) -> dict:
        if not isinstance(item, dict):
            raise TuneError(f"接口回包第 {index + 1} 项不是对象")
        if item.get("Success") is not True:
            raise TuneError(f"战斗失败（第 {index + 1} 项）：{item.get('Message')}")
        seed = int(request["Settings"]["RandomSeed"])
        current = item.get("CurrentSeed")
        if type(current) is not int or current != seed:
            raise TuneError(f"接口回包的 CurrentSeed 与固定战斗种子不符（第 {index + 1} 项）")
        stats = item.get("Stats")
        if not isinstance(stats, dict) or "TotalDamage" not in stats:
            raise TuneError("接口回包里没有 Stats.TotalDamage")
        return {
            "damage": int(round(float(stats["TotalDamage"]))),
            "frames": int(item.get("BattleFrames") or 0),
            "result": int(item.get("Result") if item.get("Result") is not None else -1),
            "seed": seed,
        }


# ---------------------------------------------------------------- 结果轴
def save_with_team(save: SaveFile, team: Sequence[int], *, template_players: list[dict] | None = None) -> SaveFile:
    """把结果轴里的玩家换成选中的队伍（其余字段照抄原轴）。"""
    from copy import deepcopy

    raw = deepcopy(save.raw)
    group = raw["playerGroupData"]
    players = group["playerData"]["playrCharacters"]
    templates = template_players or save.players
    for index, unit in enumerate(team):
        template = templates[index % len(templates)]
        player = dict(players[index]) if index < len(players) else dict(template)
        for key in ROLE_KEYS:
            if key in template and key != "unitId":
                player[key] = template[key]
        player["unitId"] = int(unit)
        if index < len(players):
            players[index] = player
    return replace(save, raw=raw, players=[dict(player) for player in players])


def evaluate_team(
    save: SaveFile,
    settings: RosterSettings,
    team: Sequence[int],
    *,
    log: Callable[[str], None] = print,
    evaluator: TeamEvaluator | None = None,
) -> list[tuple[tuple[bool, ...], TeamScore]]:
    """把指定的一支队伍按「每人 AUTO / SET」的所有组合打一遍，返回 [(组合, 分数)]。

    和搜索时的口径一致：跑满 2^人数 种（轴里没有帧轴时退回全 AUTO）。
    """
    units = tuple(int(unit) for unit in team)
    if len(units) != TEAM_SIZE:
        raise ValueError(f"队伍必须是 {TEAM_SIZE} 个人（现在是 {len(units)} 个）")
    if len(set(units)) != len(units):
        raise ValueError("同一支队伍里不能有重复角色")
    engine = evaluator or TeamEvaluator(save, settings, log=log)
    own = evaluator is None
    try:
        results = engine.evaluate_all(units)
        for flags, score in results:
            log(f"{lineup_combo(units, flags)} → {int(score.damage):,}（战斗帧 {int(score.frames)}）")
        return results
    finally:
        if own:
            engine.close()


# ---------------------------------------------------------------- 任务
class RosterJob:
    """把所有队伍组合排完的目标。"""

    def __init__(self, save: SaveFile, settings: RosterSettings, *, log=print,
                 evaluator: TeamEvaluator | None = None, cancel=None):
        self.save = save
        self.settings = settings
        self.log = log
        self._evaluator = evaluator
        self._cancel = cancel
        self.teams_total = team_count(settings)
        self.teams_done = 0
        self.battles_done = 0
        self.battle_seconds = 0.0
        self.chunks = 0
        self.best_team: tuple[int, ...] | None = None
        self.best_score: TeamScore | None = None
        self.reference: TeamScore | None = None
        self.cancelled = False
        self.names: dict[int, str] = {}
        self.export: dict | None = None
        self.results: list[dict] = []
        self._started = 0.0
        self._skip = 0

    # -------------------------------------------------------- 出口
    @property
    def evaluator(self) -> TeamEvaluator:
        if self._evaluator is None:
            self._evaluator = TeamEvaluator(self.save, self.settings, log=self.log)
        return self._evaluator

    @property
    def simulations(self) -> int:
        return self.battles_done

    @property
    def battles(self) -> int:
        return self.battles_done

    def cancel_run(self) -> None:
        self.cancelled = True
        if self._cancel is not None:
            self._cancel.set()

    @property
    def cancelled_requested(self) -> bool:
        return self.cancelled or bool(self._cancel is not None and self._cancel.is_set())

    def close(self) -> None:
        if self._evaluator is not None:
            self._evaluator.close()

    # -------------------------------------------------------- 主流程
    def _check_cancel(self) -> None:
        if self.cancelled_requested:
            raise RosterCancelled()

    def _chunk_size(self) -> int:
        """一次拿几支队伍去打：刚好够铺满 POST_WORKERS 个并行请求。"""
        combos = len(self.evaluator.combos(TEAM_SIZE))
        seeds = max(1, int(self.settings.samples))
        per_team = max(1, combos * seeds)
        room = max(1, int(self.settings.batch_size)) * max(1, POST_WORKERS)
        return max(1, min(room // per_team, POST_WORKERS * 2))

    def run(self) -> dict:
        started = time.time()
        self._started = started
        combos = len(self.evaluator.combos(TEAM_SIZE))
        seeds = max(1, int(self.settings.samples))
        total_battles = self.teams_total * combos * seeds
        self.log(f"=== 阵容搜索：共 {self.teams_total} 支队伍 × {combos} 种 AUTO/SET × {seeds} 个种子 = {total_battles} 场 ===")
        for group in self.settings.groups:
            if group.members:
                self.log(f"分组 {group.label}：{','.join(str(unit) for unit in group.members)}")
        free = ungrouped(self.settings)
        self.log(f"未分组 {len(free)} 人：{','.join(str(unit) for unit in free)}")

        if all(unit in self.settings.pool for unit in self.save.unit_ids) and len(self.save.unit_ids) == TEAM_SIZE:
            try:
                self.reference = self.evaluator.evaluate([self.save.unit_ids])[0]
                self.battles_done += combos * seeds
                self.log(f"参考（当前队伍）：期望伤害 {self.reference.damage:,} × 战斗帧 {self.reference.frames} / 结果 {self.reference.result}"
                         f"（{lineup_combo(self.save.unit_ids, self.reference.toggles)}）")
            except TuneError as exc:  # 参考打不出来不影响搜索
                self.log(f"[warn] 参考队伍打不出来：{exc}")
        if self.cancelled_requested:
            self.cancelled = True
            self.log("已停止：没有导出任何阵容。")
            return self.summary(time.time() - started)

        chunk = self._chunk_size()
        combos_per_team = len(self.evaluator.combos(TEAM_SIZE)) * seeds
        self.log(
            f"每批 {chunk} 支队伍（{chunk * combos_per_team} 场）。第一批要等摸轴器把数据读进来并预热，"
            "通常几秒到几十秒，之后就快了。"
        )
        buffer: list[tuple[int, ...]] = []
        report_every = max(1, min(200, self.teams_total // 10 or 1))
        skip = int(self._skip)
        try:
            for index, team in enumerate(iter_teams(self.settings)):
                if index < skip:
                    continue
                buffer.append(team)
                if len(buffer) < chunk:
                    continue
                self._run_chunk(buffer)
                buffer.clear()
                self._check_cancel()
                if self.teams_done % report_every == 0 or self.teams_done == self.teams_total:
                    self._log_progress()
            if buffer:
                self._run_chunk(buffer)
                self._log_progress()
            self._check_cancel()
        except RosterCancelled:
            self.cancelled = True
            self.log(f"已停止：排了 {self.teams_done}/{self.teams_total} 支队伍，没有导出任何阵容。")
            return self.summary(time.time() - started)

        if self.best_team is None or self.best_score is None:
            raise TuneError("没有可用的队伍（候选池太小或分组约束太严）")
        self.export = {
            "team": list(self.best_team),
            "toggles": list(self.best_score.toggles),
            "damage": int(self.best_score.damage),
            "frames": int(self.best_score.frames),
            "result": int(self.best_score.result),
        }
        self.log(
            f"=== 阵容搜索完成：最优期望伤害 {self.best_score.damage:,}"
            f"（{lineup_combo(self.best_team, self.best_score.toggles)}） ==="
        )
        self.log(f"最优阵容（按站位）：{lineup_names(self.best_team, self.names)}")
        self.log(f"每位角色的操作：{lineup_text(self.best_team, self.best_score.toggles, self.names)}")
        self.log(f"共 {self.teams_done}/{self.teams_total} 支队伍 / {self.battles_done} 场 / 用时 {time.time() - started:.1f}s")
        return self.summary(time.time() - started)

    def _run_chunk(self, teams: list[tuple[int, ...]]) -> None:
        began = time.time()
        scores = self.evaluator.evaluate(teams)
        spent = time.time() - began
        combos = len(self.evaluator.combos(TEAM_SIZE))
        seeds = max(1, int(self.settings.samples))
        self.battles_done += len(teams) * combos * seeds
        self.battle_seconds += spent
        self.chunks += 1
        if self.chunks <= 3 or spent >= 2.0:
            total = time.time() - (self._started or began)
            self.log(
                f"第 {self.chunks} 批 {len(teams)} 支队伍 / {len(teams) * combos * seeds} 场"
                f"用了 {spent:.2f} 秒（累计 {total:.1f} 秒）"
            )
        for team, score in zip(teams, scores):
            self.teams_done += 1
            improved = self.best_score is None or score.damage > self.best_score.damage
            if improved:
                self.best_score = score
                self.best_team = tuple(int(unit) for unit in team)
            self._remember(team, score)
            self.log(
                f"[{self.teams_done}/{self.teams_total}] {self._team_text(team)} → "
                f"{int(score.damage):,}（{lineup_combo(team, score.toggles)}）"
                + ("  新的最优" if improved else "")
            )

    def _remember(self, team: Sequence[int], score: TeamScore) -> None:
        """只保留伤害最高的 ``RESULT_LIMIT`` 支队伍（结果表也只列这些）。"""
        entry = {
            "team": [int(unit) for unit in team],
            "damage": int(score.damage),
            "frames": int(score.frames),
            "result": int(score.result),
            "toggles": [bool(flag) for flag in score.toggles],
        }
        if len(self.results) < RESULT_LIMIT:
            self.results.append(entry)
            self.results.sort(key=lambda item: -int(item["damage"]))
            return
        if int(entry["damage"]) <= int(self.results[-1]["damage"]):
            return
        self.results[-1] = entry
        self.results.sort(key=lambda item: -int(item["damage"]))

    def _team_text(self, team: Sequence[int]) -> str:
        return lineup_names(team, self.names or {})

    def _log_progress(self) -> None:
        best = self.best_score
        text = f"{best.damage:,}" if best else "-"
        combo = f"（{lineup_combo(self.best_team, best.toggles)}）" if best and self.best_team else ""
        self.log(
            f"--- 进度 {self.teams_done}/{self.teams_total}：当前最优 {text}{combo} / 已打 {self.battles_done} 场"
        )

    # -------------------------------------------------------- 产物
    def summary(self, elapsed: float) -> dict:
        export = self.export or {}
        return {
            "mode": "roster",
            "teams": [dict(item) for item in self.results],
            "excel": str(self.save.path),
            "endpoint": self.settings.endpoint,
            "battle_time": int(self.settings.battle_time),
            "seed": int(self.settings.seed),
            "samples": int(self.settings.samples),
            "teams_total": int(self.teams_total),
            "teams_done": int(self.teams_done),
            "combos": len(self.evaluator.combos(TEAM_SIZE)),
            "every_combo": bool(self.settings.every_combo),
            "to_max_attr": bool(self.settings.to_max_attr),
            "groups": [group.to_dict() for group in self.settings.groups],
            "pool": list(self.settings.pool),
            "reference": self.reference.to_dict() if self.reference else None,
            "best_team": list(export.get("team") or self.best_team or ()),
            "best_toggles": list(export.get("toggles") or (self.best_score.toggles if self.best_score else ())),
            "final": int(export.get("damage") or (self.best_score.damage if self.best_score else 0)),
            "final_frames": int(export.get("frames") or (self.best_score.frames if self.best_score else 0)),
            "final_result": int(export.get("result") if export.get("result") is not None else (self.best_score.result if self.best_score else -1)),
            "battles": int(self.battles_done),
            "cancelled": bool(self.cancelled_requested),
            "elapsed_s": round(float(elapsed), 1),
            "export": export,
        }

    def state_dict(self) -> dict:
        return {
            "mode": "roster",
            "cursor": int(self.teams_done),
            "battles": int(self.battles_done),
            "best_team": list(self.best_team or ()),
            "best_score": self.best_score.to_dict() if self.best_score else None,
            "reference": self.reference.to_dict() if self.reference else None,
            "export": self.export,
        }

    def apply_state(self, data: dict) -> None:
        self.teams_done = int(data.get("cursor") or 0)
        self.battles_done = int(data.get("battles") or 0)
        team = data.get("best_team") or []
        if team:
            self.best_team = tuple(int(unit) for unit in team)
        score = data.get("best_score")
        if isinstance(score, dict):
            self.best_score = TeamScore.from_dict(score)
        reference = data.get("reference")
        if isinstance(reference, dict):
            self.reference = TeamScore.from_dict(reference)
        self.export = data.get("export") if isinstance(data.get("export"), dict) else None
        self._skip = self.teams_done

    def resume(self) -> dict:
        """续跑时跳过已经打完的队伍。"""
        if self._skip:
            self.log(f"续跑：跳过前 {self._skip} 支队伍（已完成 {self._skip}/{self.teams_total}）")
        return self.run()
