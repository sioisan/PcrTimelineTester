"""Read and write PCRCalculator's compressed Excel save (``savePage`` sheet).

The calculator exports its whole guild timeline into a hidden worksheet:

    savePage!A1 = human readable header (axis name)
    savePage!A2 = gzip(base64(utf-8 JSON)) of ``GuildTimelineData``

The frame axis we tune lives at ``playerGroupData.UBExecTimeData`` (five character
rows plus an optional boss row) and is mirrored at the top level ``UBExecTime``.

This module keeps only the frame axis parts (no semantic genome); the raw JSON is
preserved verbatim so unrelated fields survive a round trip.
"""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass, field
import gzip
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Callable
from zipfile import BadZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.utils.exceptions import InvalidFileException

DURATION = re.compile(r"(?:尾刀|补偿)\s*(\d+(?:\.\d+)?)\s*(?:s|秒)", re.IGNORECASE)
CHARACTER_ROWS = 5
SAVE_SHEET = "savePage"
SUMMARY_SHEET = "调帧结果"
NAME_SHEET = "基础数据"
CELL_LIMIT = 32767
UNIT_FIELDS = (
    "enemyid", "unitId", "level", "rarity", "love", "rank", "equipLevel", "skillLevel",
    "playLoveDic", "uniqueEqLv", "uniqueEq2Lv", "exEquip", "exEquipLevel", "exEquipSubs",
    "PrefabId", "love2", "connectRank", "connectRankLimit",
)

FrameAxis = list[list[float]]


def object_fields(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是对象")
    return value


def integer(value: Any, label: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} 必须是整数")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} 不能小于 {minimum}")
    return value


def finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是数值")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} 必须是有限数值")
    return number


def frame_axis(value: Any, label: str = "UBExecTimeData") -> FrameAxis:
    """Validate a ``[[frame, ...], ...]`` axis (five rows, optional boss row)."""
    if value is None or value == [] or value == ():
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} 必须是数组")
    if not CHARACTER_ROWS <= len(value) <= CHARACTER_ROWS + 1:
        raise ValueError(f"{label} 必须有 {CHARACTER_ROWS} 条角色行（可多一条 Boss 行），实际 {len(value)} 行")
    rows: FrameAxis = []
    for row_index, row in enumerate(value):
        if not isinstance(row, (list, tuple)):
            raise ValueError(f"{label} 第 {row_index + 1} 行必须是数组")
        rows.append([finite_number(item, f"{label} 第 {row_index + 1} 行帧") for item in row])
    return rows


def _notice(log: Callable[[str], None] | None, message: str) -> None:
    """把提示交给调用方的日志；没给就打到 stdout（冻结版 stdout 是 None，跳过）。"""
    if log is not None:
        log(message)
        return
    stream = sys.stdout
    if stream is None:
        return
    try:
        print(message)
    except (ValueError, OSError):
        pass


def semantic_axis(
    value: Any,
    label: str = "SemanUBExecTimeData",
    *,
    log: Callable[[str], None] | None = None,
) -> list[list[int]]:
    """校验语义 UB 轴（五条基因序列），允许为空。

    摸轴器只按位号 0..4 取车道（``SemanUbManager.IsUbExec(pos)`` 里
    ``pos > 4 || pos >= queues.Length`` 直接返回 false），多出来的行永远不会被
    消费。所以超过 ``CHARACTER_ROWS`` 行时保留前五行、丢掉其余并打一行提示；
    不足五行则整条语义轴当成没有（空），只打一行提示。
    """
    if value is None or value == [] or value == ():
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} 必须是数组")
    rows = list(value)
    if len(rows) > CHARACTER_ROWS:
        dropped = len(rows) - CHARACTER_ROWS
        rows = rows[:CHARACTER_ROWS]
        _notice(
            log,
            f"提示：{label} 有 {len(value)} 行，只保留前 {CHARACTER_ROWS} 行"
            f"（丢掉了后面 {dropped} 行——摸轴器只认前 {CHARACTER_ROWS} 条车道，"
            f"多出来的行永远不会被执行）",
        )
    if len(rows) != CHARACTER_ROWS:
        _notice(
            log,
            f"提示：{label} 只有 {len(rows)} 行（凑不满 {CHARACTER_ROWS} 条车道），"
            f"当成没有语义数据",
        )
        return []
    lanes: list[list[int]] = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, (list, tuple)):
            raise ValueError(f"{label} 第 {row_index + 1} 行必须是数组")
        lanes.append([integer(item, f"{label} 第 {row_index + 1} 行基因") for item in row])
    return lanes


def sequence_axis(value: Any, lanes: list[list[int]]) -> tuple[int, ...] | None:
    """校验角色序列（``SemanUBSequenceData``）：每行基因数必须与角色出现次数一致。"""
    if value is None or value == [] or value == ():
        return None
    if not isinstance(value, (list, tuple)):
        raise ValueError("SemanUBSequenceData 必须是数组")
    roles = tuple(integer(role, "序列角色") for role in value)
    if any(role >= CHARACTER_ROWS for role in roles):
        raise ValueError("序列角色必须在 0..4 内")
    if not lanes:
        raise ValueError("有角色序列但没有语义 UB 轴")
    for role, lane in enumerate(lanes):
        if roles.count(role) != len(lane):
            raise ValueError("角色序列必须恰好消费五条UB序列中的基因")
    return roles


def _role_mastery(value: Any) -> dict[str, list[int]]:
    normalized: dict[str, list[int]] = {}
    for key, pair in object_fields(value, "roleMastery").items():
        if isinstance(pair, dict):
            pair = [
                pair.get("slotLevel", pair.get("Item1", pair.get("item1", 0))),
                pair.get("enhanceLevel", pair.get("Item2", pair.get("item2", 0))),
            ]
        if not isinstance(pair, (list, tuple)) or len(pair) > 2:
            raise ValueError("roleMastery 必须提供槽位和强化等级")
        normalized[str(key)] = [integer(item, "roleMastery") for item in pair] + [0] * (2 - len(pair))
    return normalized


def _unit_id(value: Any) -> int | None:
    """``角色ID`` cell -> int, or ``None`` when the cell is not an id."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _looks_numeric(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def read_names(path: Path) -> dict[int, str]:
    """Character / boss names from the exported workbook's ``基础数据`` sheet.

    The save JSON carries no names at all (``PlayerList`` is unitId + levels), so the
    only place the Chinese names exist is the workbook the calculator exports:
    ``基础数据`` lists ``角色ID`` / ``角色名字`` for the five characters and, below a
    ``BOSS详情`` marker row, the boss id with ``unit_name``.

    A workbook without that sheet (or an unreadable one) simply yields no names and
    callers fall back to ``角色{slot}``.
    """
    path = Path(path)
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except (OSError, BadZipFile, InvalidFileException, KeyError, ValueError):
        return {}
    try:
        if NAME_SHEET not in workbook.sheetnames:
            return {}
        rows = list(workbook[NAME_SHEET].iter_rows(values_only=True))
    except (KeyError, OSError, ValueError, BadZipFile, InvalidFileException):
        return {}
    finally:
        workbook.close()

    names: dict[int, str] = {}
    characters = 0
    boss_block = False
    for row in rows:
        cells = list(row) + [None, None]
        joined = " ".join(str(cell) for cell in cells if cell is not None).upper()
        if "BOSS" in joined:
            boss_block = True
            continue
        if characters >= CHARACTER_ROWS and not boss_block:
            # Everything below the character block (角色UB时间 …) is not a roster.
            continue
        unit_id = _unit_id(cells[0])
        label = cells[1].strip() if isinstance(cells[1], str) else ""
        if unit_id is None or not label or _looks_numeric(label):
            continue
        names.setdefault(unit_id, label)
        if not boss_block:
            characters += 1
    return names


@dataclass
class SaveFile:
    """One imported timeline: unit roster, enemy, seed and the frame axis."""

    path: Path
    header: str
    raw: dict
    players: list[dict]
    enemy_ids: list[int]
    settings: dict = field(default_factory=dict)
    battle_seed: int | None = None
    frames: FrameAxis = field(default_factory=list)
    semantic: list[list[int]] = field(default_factory=list)
    sequence: tuple[int, ...] | None = None
    names: dict[int, str] = field(default_factory=dict)

    @property
    def has_frames(self) -> bool:
        return bool(self.frames) and any(self.frames)

    @property
    def has_semantic(self) -> bool:
        return bool(self.semantic) and any(self.semantic)

    @property
    def group(self) -> dict:
        return self.raw["playerGroupData"]

    @property
    def unit_ids(self) -> list[int]:
        return [int(player["unitId"]) for player in self.players]

    @property
    def unit_names(self) -> list[str]:
        return [
            self.names.get(unit_id) or f"角色{index + 1}"
            for index, unit_id in enumerate(self.unit_ids)
        ]

    @property
    def boss_name(self) -> str | None:
        for enemy_id in self.enemy_ids:
            if self.names.get(enemy_id):
                return self.names[enemy_id]
        return None

    @property
    def boss_label(self) -> str:
        return self.boss_name or "Boss"

    @property
    def unit_count(self) -> int:
        return len(self.players)

    def frames_copy(self) -> FrameAxis:
        return [list(row) for row in self.frames]

    def suggested_duration(self, *names: str) -> int:
        """Battle seconds implied by the axis name ("尾刀 35s" 之类), else 90."""
        for text in (*names, self.raw.get("timeLineName"), self.raw.get("detail")):
            if not isinstance(text, str):
                continue
            match = DURATION.search(text)
            if match:
                seconds = float(match[1])
                if seconds.is_integer() and 1 <= seconds <= 90:
                    return int(seconds)
        return 90

    def request_context(self, *, battle_time: int, battle_seed: int) -> dict:
        """Request fields that do not depend on the frame axis."""
        group = self.group
        settings = object_fields(group.get("timeLineData", {}), "timeLineData")
        is_talent = group.get("isTalentQuestBoss")
        if is_talent is None:
            is_talent = any(
                group.get(key, 0)
                for key in ("talentQuestMap", "talentQuestArea", "talentQuestIndex")
            )
        return {
            "PlayerList": deepcopy(self.players),
            "EnemyList": [],
            "GuildEnemyList": list(self.enemy_ids),
            "Settings": {**settings, "UseFixedRandomSeed": True, "RandomSeed": battle_seed},
            "TalentSkillLevels": object_fields(group.get("talentLevel", {}), "talentLevel"),
            "TalentSkillNodeLevels": object_fields(group.get("talentSkill", {}), "talentSkill"),
            "MasterSkillNodeLevels": object_fields(group.get("teamSkill", {}), "teamSkill"),
            "RoleMastery": _role_mastery(group.get("roleMastery", {})),
            "TalentFullInfo": True,
            "IsTalentQuest": bool(is_talent),
            "ToMaxUnitAttr": False,
            "IsGuildBattle": True,
            "BattleTime": battle_time,
            "WaveGroupId": integer(group.get("waveGroupId", 0), "waveGroupId"),
            "SetToggles": [],
            "NeedOnceResult": False,
        }


def parse_save(
    path: Path,
    raw: dict,
    header: str,
    *,
    names: dict[int, str] | None = None,
    require_frames: bool = True,
    allow_empty: bool = False,
    log: Callable[[str], None] | None = None,
) -> SaveFile:
    """Validate a decoded timeline save and pull out the axes.

    ``require_frames=False`` 允许读「只有语义 UB 数据」的轴（语义 GA 模式需要）。
    """
    group = object_fields(raw.get("playerGroupData"), "playerGroupData")
    players = object_fields(group.get("playerData"), "playerData").get("playrCharacters")
    if not isinstance(players, list) or len(players) != CHARACTER_ROWS:
        raise ValueError(f"Excel 必须包含 {CHARACTER_ROWS} 个角色")
    units = tuple(
        {name: unit[name] for name in UNIT_FIELDS if name in unit}
        for unit in (object_fields(player, "角色") for player in players)
    )
    ids = [integer(unit.get("unitId"), "unitId", 1) for unit in units]
    if len(set(ids)) != len(ids):
        raise ValueError("Excel 角色 ID 必须唯一")
    enemies = group.get("selectedEnemyIDs")
    if not isinstance(enemies, list) or not enemies:
        raise ValueError("Excel 缺少 selectedEnemyIDs")
    enemy_ids = [integer(enemy, "Boss ID", 1) for enemy in enemies]
    settings = object_fields(group.get("timeLineData", {}), "timeLineData")
    battle_seed = settings.get("RandomSeed")
    if battle_seed is not None and (isinstance(battle_seed, bool) or not isinstance(battle_seed, int)):
        raise ValueError("Excel 的 timeLineData.RandomSeed 必须是整数")

    frames = frame_axis(group.get("UBExecTimeData") or [])
    if not any(frames) and raw.get("UBExecTime"):
        frames = frame_axis(raw["UBExecTime"], "UBExecTime")
    semantic = semantic_axis(group.get("SemanUBExecTimeData") or [], log=log)
    sequence = sequence_axis(group.get("SemanUBSequenceData"), semantic) if semantic else None
    if (not frames or not any(frames)) and not semantic and not allow_empty:
        raise ValueError(
            "这条轴里既没有帧数据也没有语义 UB 数据。请确认导出的是 PCRCalculator 的轴（savePage 页）"
        )
    if require_frames and (not frames or not any(frames)):
        raise ValueError(
            "这条轴里没有帧数据（只有语义 UB 数据）。帧模式需要帧轴，"
            "可以把优化模式切到「语义 GA」来调这条轴"
        )
    return SaveFile(
        path=Path(path), header=header, raw=raw, players=[dict(unit) for unit in units],
        enemy_ids=enemy_ids, settings=settings, battle_seed=battle_seed,
        frames=[list(row) for row in frames], semantic=semantic, sequence=sequence,
        names=dict(names or {}),
    )


def read_save(
    path: Path,
    *,
    require_frames: bool = True,
    allow_empty: bool = False,
    log: Callable[[str], None] | None = None,
) -> SaveFile:
    """Read any PCRCalculator exported ``.xlsx`` timeline."""
    path = Path(path)
    if path.suffix.lower() != ".xlsx":
        raise ValueError("请选择 PCRCalculator 导出的 .xlsx 轴")
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            sheet = workbook[SAVE_SHEET]
            header, encoded = sheet["A1"].value, sheet["A2"].value
        finally:
            workbook.close()
        if not isinstance(encoded, str):
            raise ValueError(f"{SAVE_SHEET}!A2 缺少压缩轴数据")
        raw = object_fields(json.loads(gzip.decompress(base64.b64decode(encoded))), "Excel 轴")
    except (KeyError, OSError, EOFError, ValueError, BadZipFile, InvalidFileException) as exc:
        raise ValueError(f"无法读取 Excel 的 {SAVE_SHEET} 轴数据，文件 {path.name}，{exc}") from exc
    return parse_save(
        path, raw, str(header or SAVE_SHEET), names=read_names(path),
        require_frames=require_frames, allow_empty=allow_empty, log=log,
    )


def write_tuned_save(
    path: Path,
    save: SaveFile,
    frames: FrameAxis,
    *,
    battle_seed: int,
    damage: int,
    summary: list[list[Any]],
    semantic: list[list[int]] | None = None,
    sequence: tuple[int, ...] | None = None,
    sheet_name: str = SUMMARY_SHEET,
) -> None:
    """Write a fresh importable workbook with the tuned axis.

    A fresh workbook (instead of editing the source file) keeps the source axis
    untouched and gives the calculator exactly one hidden ``savePage`` plus a
    human readable summary sheet.  ``semantic`` 不为空时写的是语义轴（GA 模式），
    帧轴同时写进去当作可读的对照。
    """
    raw = deepcopy(save.raw)
    group = raw["playerGroupData"]
    axis = [list(row) for row in frames]
    group["UBExecTimeData"] = axis
    group["useAutoMode"] = False
    group["useSetMode"] = False
    if semantic is not None:
        group["SemanUBExecTimeData"] = [list(lane) for lane in semantic]
        group["SemanUBSequenceData"] = list(sequence) if sequence is not None else []
        group["useSemanMode"] = True
        group["useSequenceUB"] = sequence is not None
    else:
        # Frame mode: 语义轴留在原处但关掉，摸轴器只会重放这份帧轴。
        group["useSemanMode"] = False
        group["useSequenceUB"] = False
    group.setdefault("timeLineData", {}).update(RandomSeed=battle_seed, UseFixedRandomSeed=True)
    raw["UBExecTime"] = axis
    raw["currentRandomSeed"] = battle_seed
    raw["totalDamage"] = int(damage)
    raw["exceptDamage"] = 0
    raw["backDamage"] = 0
    raw.pop("uBDetails", None)
    encoded = base64.b64encode(gzip.compress(json.dumps(raw, ensure_ascii=False).encode())).decode()
    if len(encoded) > CELL_LIMIT:
        raise ValueError(f"调帧后的 {SAVE_SHEET} 超出 Excel 单元格容量（{len(encoded)} > {CELL_LIMIT}）")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    try:
        sheet = workbook.active
        sheet.title = sheet_name[:31] or SUMMARY_SHEET
        for row in summary:
            sheet.append(list(row))
        sheet.column_dimensions["A"].width = 22
        sheet.column_dimensions["B"].width = 52
        sheet.column_dimensions["C"].width = 22
        base = _source_base_rows(Path(save.path))
        if base:
            # Keep 基础数据 so the tuned workbook still shows Chinese names when re-imported here.
            extra = workbook.create_sheet(NAME_SHEET)
            for row in base:
                extra.append(list(row))
        saved = workbook.create_sheet(SAVE_SHEET)
        saved.append([save.header])
        saved.append([encoded])
        saved.sheet_state = "hidden"
        workbook.save(path)
    finally:
        workbook.close()


def _source_base_rows(path: Path, limit: int = 200) -> list[list[Any]]:
    """Copy the source workbook's 基础数据 rows (names only live there)."""
    if not path.is_file():
        return []
    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except (OSError, BadZipFile, InvalidFileException, KeyError):
        return []
    try:
        if NAME_SHEET not in workbook.sheetnames:
            return []
        sheet = workbook[NAME_SHEET]
        rows: list[list[Any]] = []
        for index, row in enumerate(sheet.iter_rows(values_only=True)):
            if index >= limit:
                break
            if any(value not in (None, "") for value in row):
                rows.append([value for value in row])
        return rows
    finally:
        workbook.close()
