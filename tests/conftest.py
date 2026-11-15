"""Shared fixtures: synthetic PCRCalculator saves and a deterministic fake client."""

from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path
from typing import Callable

from openpyxl import Workbook
import pytest

from PcrTimelineTester.client import BattleOutcome
from PcrTimelineTester.frames import base_frame
from PcrTimelineTester.savefile import SAVE_SHEET, parse_save

UNIT_IDS = (1001, 1002, 1003, 1004, 1005)
ENEMY_IDS = (800100,)


def timeline_raw(frames: list[list[float]], *, seed: int = 666, name: str = "尾刀 30s", semantic: list[list[int]] | None = None) -> dict:
    """A minimal but structurally faithful GuildTimelineData save."""
    players = [
        {"unitId": unit_id, "level": 250, "rarity": 5, "rank": 30, "equipLevel": 1, "skillLevel": 250}
        for unit_id in UNIT_IDS
    ]
    return {
        "version": 2,
        "timeLineName": name,
        "playerGroupData": {
            "playerData": {"playrCharacters": players},
            "selectedEnemyIDs": list(ENEMY_IDS),
            "timeLineData": {"RandomSeed": seed, "UseFixedRandomSeed": True},
            "UBExecTimeData": [list(row) for row in frames],
            "talentLevel": {},
            "talentSkill": {},
            "teamSkill": {},
            "roleMastery": {},
            "waveGroupId": 1,
        },
        "UBExecTime": [list(row) for row in frames],
        "currentRandomSeed": seed,
        "totalDamage": 0,
    }


def write_save(path: Path, raw: dict, header: str = "测试轴") -> Path:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = SAVE_SHEET
    encoded = base64.b64encode(gzip.compress(json.dumps(raw, ensure_ascii=False).encode())).decode()
    sheet.append([header])
    sheet.append([encoded])
    workbook.save(path)
    workbook.close()
    return path


def make_save(tmp_path: Path, frames: list[list[float]], **kwargs):
    raw = timeline_raw(frames, **kwargs)
    semantic = kwargs.get("semantic")
    if semantic is not None:
        group = raw["playerGroupData"]
        group["SemanUBExecTimeData"] = [list(lane) for lane in semantic]
        group["useSemanMode"] = True
        group["SemanUBSequenceData"] = []
    path = write_save(tmp_path / "轴.xlsx", raw)
    return parse_save(path, raw, "测试轴", require_frames=semantic is None)


def landscape(frame: int, peak: int = 480, height: float = 1000.0) -> float:
    """Triangular hill: strictly increasing up to ``peak``, then falling."""
    return height - abs(frame - peak) * 10.0


def hills(cells: dict[tuple[int, int], tuple[int, float]]) -> Callable[[list[list[float]]], float]:
    """Metric = sum of independent hills, one per ``(row, index) -> (peak, height)``."""

    def metric(table: list[list[float]]) -> float:
        total = 0.0
        for (row, index), (peak, height) in cells.items():
            total += landscape(base_frame(table[row][index]), peak=peak, height=height)
        return total

    return metric


class FakeClient:
    """Deterministic stand-in for SimulatorClient over a table -> metric function."""

    def __init__(self, metric: Callable[[list[list[float]]], float]) -> None:
        self._metric = metric
        self.battles = 0
        self.tables: list[list[list[float]]] = []
        self.closed = False

    def evaluate(self, candidates) -> list[BattleOutcome]:
        outcomes = []
        for table in candidates:
            copied = [list(row) for row in table]
            self.tables.append(copied)
            value = float(self._metric(copied))
            self.battles += 1
            outcomes.append(BattleOutcome(
                metric=value, damage=round(value), battle_frames=5400, result=0, seed=666,
            ))
        return outcomes

    def evaluate_one(self, frames) -> BattleOutcome:
        return self.evaluate([frames])[0]

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def five_rows() -> list[list[float]]:
    """Five character rows; all UB frames on round numbers."""
    return [[470.0, 1000.0], [500.0, 1100.0], [600.0, 1200.0], [700.0, 1300.0], [800.0, 1400.0]]
