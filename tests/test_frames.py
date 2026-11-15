"""Frame arithmetic and target enumeration."""

from __future__ import annotations

import pytest

from conftest import UNIT_IDS

from PcrTimelineTester.frames import base_frame, build_targets, frac_of, text, with_frac

UNIT_IDS = [1001, 1002, 1003, 1004, 1005]


def test_base_frame_rounds_half_away_from_zero() -> None:
    assert base_frame(470.4) == 470
    assert base_frame(470.5) == 471
    assert base_frame(1799.5) == 1800
    assert base_frame(0.0) == 0


def test_fraction_is_kept_to_three_decimals() -> None:
    assert frac_of(470.001) == pytest.approx(0.001)
    assert frac_of(470.0) == 0.0
    # 四舍五入到 3 位之后多余的抖动被抹平
    assert frac_of(300.0014999) == pytest.approx(0.001)


def test_with_frac_preserves_same_frame_ordering_tail() -> None:
    assert with_frac(300.001, 305) == pytest.approx(305.001)
    assert with_frac(300.0, 305) == 305.0


def test_text_matches_calculator_display() -> None:
    assert text(4485.001) == "4485.001"
    assert text(4480.0) == "4480"
    assert text(410.05) == "410.05"


def test_build_targets_skips_empty_cells() -> None:
    frames = [[470.0, 0.0], [500.0], [0.0, 0.0], [700.0], [800.0]]
    targets = build_targets(frames, UNIT_IDS)
    assert [target.key for target in targets] == ["0:0", "1:0", "3:0", "4:0"]
    assert targets[0].label == "角色1 UB1"
    assert targets[0].frame == 470


def test_build_targets_uses_workbook_names() -> None:
    frames = [[470.0], [500.0], [600.0], [700.0], [800.0], [900.0]]
    names = {1001: "春花", 1002: "水狐", 1003: "春星", 1004: "爱梅斯", 1005: "sms"}
    targets = build_targets(frames, UNIT_IDS, names=names, boss_name="哥布林", include_boss=True)
    assert targets[0].label == "春花 UB1"
    assert targets[-1].label == "哥布林 UB1"
    # 重名时才补上 ID，避免两项看起来一样
    dupes = build_targets(frames[:1], UNIT_IDS[:1], names={1001: "春花"})
    assert dupes[0].label == "春花 UB1"
    clash = build_targets(frames[:2], UNIT_IDS[:2], names={1001: "春花", 1002: "春花"})
    assert [target.label for target in clash] == ["春花#1001 UB1", "春花#1002 UB1"]


def test_build_targets_boss_row_and_filters() -> None:
    frames = [[470.0], [500.0], [600.0], [700.0], [800.0], [900.0]]
    targets = build_targets(frames, UNIT_IDS, include_boss=True)
    assert targets[-1].label == "Boss UB1"
    assert targets[-1].key == "5:0"

    only = build_targets(frames, UNIT_IDS, items={(2, 1)})
    assert [target.key for target in only] == ["1:0"]

    first_two = build_targets(frames, UNIT_IDS, max_items=2)
    assert [target.key for target in first_two] == ["0:0", "1:0"]


def test_to_raw_keeps_original_tail() -> None:
    frames = [[300.001]]
    target = build_targets(frames, [1001])[0]
    assert target.raw_text == "300.001"
    assert target.to_raw(305) == pytest.approx(305.001)


def test_build_targets_are_ordered_by_frame() -> None:
    frames = [[900, 1000], [100, 120, 1100], [500], [700], [800]]
    targets = build_targets(frames, list(UNIT_IDS), names={UNIT_IDS[0]: "春花", UNIT_IDS[1]: "爱梅斯"})
    assert [target.frame for target in targets] == sorted(target.frame for target in targets)
    assert [(target.row, target.index) for target in targets[:3]] == [(1, 0), (1, 1), (2, 0)]
    assert targets[0].label == "爱梅斯 UB1"
    assert targets[-1].label == "爱梅斯 UB3"


def test_build_targets_max_items_keeps_the_earliest_frames() -> None:
    frames = [[900], [100], [500], [700], [800]]
    targets = build_targets(frames, list(UNIT_IDS), max_items=2)
    assert [target.frame for target in targets] == [100, 500]
