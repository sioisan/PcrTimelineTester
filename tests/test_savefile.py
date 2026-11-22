"""Reading and writing PCRCalculator's compressed excel save."""

from __future__ import annotations

import base64
import gzip
import json

import pytest
from openpyxl import load_workbook

from PcrTimelineTester.frames import build_targets
from PcrTimelineTester.savefile import (
    SAVE_SHEET,
    SUMMARY_SHEET,
    read_save,
    write_tuned_save,
)

from conftest import make_save, timeline_raw, write_save


def test_read_save(tmp_path, five_rows) -> None:
    save = make_save(tmp_path, five_rows)
    assert save.frames == five_rows
    assert save.unit_ids == [1001, 1002, 1003, 1004, 1005]
    assert save.enemy_ids == [800100]
    assert save.battle_seed == 666
    assert save.unit_names[0] == "角色1"
    assert save.boss_label == "Boss"


def test_names_come_from_the_base_data_sheet(tmp_path, five_rows) -> None:
    path = write_save(tmp_path / "带名字.xlsx", timeline_raw(five_rows))
    workbook = load_workbook(path)
    sheet = workbook.create_sheet("基础数据")
    sheet.append(["角色基础参数"])
    sheet.append(["角色ID", "角色名字", "角色等级"])
    for unit_id, name in zip((1001, 1002, 1003, 1004, 1005), ("春花", "水狐", "春星", "爱梅斯", "sms")):
        sheet.append([unit_id, name, 200])
    sheet.append(["BOSS详情"])
    sheet.append([800100, "哥布林", "一周目"])
    workbook.save(path)
    workbook.close()

    save = read_save(path)
    assert save.unit_names == ["春花", "水狐", "春星", "爱梅斯", "sms"]
    assert save.names[800100] == "哥布林"
    assert save.boss_label == "哥布林"
    targets = build_targets(save.frames, save.unit_ids, names=save.names, boss_name=save.boss_name)
    assert targets[0].label == "春花 UB1"


def test_duration_comes_from_the_axis_name(tmp_path, five_rows) -> None:
    assert make_save(tmp_path, five_rows, name="尾刀 30s").suggested_duration() == 30
    assert make_save(tmp_path, five_rows, name="补偿 12.5秒").suggested_duration() == 90  # 非整数秒回落
    assert make_save(tmp_path, five_rows, name="全程").suggested_duration() == 90


def test_request_context(tmp_path, five_rows) -> None:
    context = make_save(tmp_path, five_rows).request_context(battle_time=35, battle_seed=1234)
    assert context["BattleTime"] == 35
    assert context["IsGuildBattle"] is True
    assert context["GuildEnemyList"] == [800100]
    assert context["EnemyList"] == []
    assert context["SetToggles"] == []
    assert context["Settings"]["RandomSeed"] == 1234
    assert context["Settings"]["UseFixedRandomSeed"] is True
    assert len(context["PlayerList"]) == 5
    assert context["PlayerList"][0]["unitId"] == 1001


def test_request_preserves_saved_connect_ranks(tmp_path, five_rows) -> None:
    raw = timeline_raw(five_rows)
    units = raw["playerGroupData"]["playerData"]["playrCharacters"]
    units[0].update(connectRank=12, connectRankLimit=15)
    units[1].update(connectRank=0, connectRankLimit=5)
    save = read_save(write_save(tmp_path / "rank.xlsx", raw))
    players = save.request_context(battle_time=90, battle_seed=666)["PlayerList"]
    assert [(p["connectRank"], p["connectRankLimit"]) for p in players[:2]] == [(12, 15), (0, 5)]
    assert "connectRank" not in players[2] and "connectRankLimit" not in players[2]


def test_write_tuned_save_is_a_new_importable_workbook(tmp_path, five_rows) -> None:
    save = make_save(tmp_path, five_rows)
    tuned = [[480.0, 1000.0], [500.0, 1100.0], [600.0, 1200.0], [700.0, 1300.0], [800.0, 1400.0]]
    target = tmp_path / "调帧结果.xlsx"
    write_tuned_save(
        target, save, tuned, battle_seed=666, damage=1234567,
        summary=[["PCR 调帧结果", str(save.path)], ["项", "原帧 → 新帧", "伤害"]],
    )
    reread = read_save(target)
    assert reread.frames == tuned
    assert reread.group["useSemanMode"] is False
    assert reread.group["useSequenceUB"] is False
    assert reread.raw["totalDamage"] == 1234567
    assert reread.raw["UBExecTime"] == tuned
    assert reread.group["timeLineData"]["RandomSeed"] == 666
    workbook = load_workbook(target)
    try:
        assert workbook[SAVE_SHEET].sheet_state == "hidden"
        assert workbook[SUMMARY_SHEET]["A1"].value == "PCR 调帧结果"
        assert workbook[SUMMARY_SHEET]["B1"].value == str(save.path)
        assert workbook[SUMMARY_SHEET]["A2"].value == "项"
    finally:
        workbook.close()
    # 原文件没有被改动
    assert save.frames == five_rows


def test_rejects_wrong_character_count(tmp_path) -> None:
    raw = timeline_raw([[470.0]] * 5)
    raw["playerGroupData"]["playerData"]["playrCharacters"].pop()
    path = write_save(tmp_path / "坏轴.xlsx", raw)
    with pytest.raises(ValueError, match="5 个角色"):
        read_save(path)


def test_rejects_semantic_only_axis(tmp_path) -> None:
    raw = timeline_raw([[470.0]] * 5)
    raw["playerGroupData"]["UBExecTimeData"] = [[], [], [], [], []]
    raw["UBExecTime"] = []
    path = write_save(tmp_path / "语义轴.xlsx", raw)
    with pytest.raises(ValueError, match="没有帧数据"):
        read_save(path)


def test_rejects_non_xlsx(tmp_path, five_rows) -> None:
    path = tmp_path / "轴.csv"
    path.write_text("nope", encoding="utf-8")
    with pytest.raises(ValueError, match="xlsx"):
        read_save(path)


def test_reads_a_hand_built_cell_string(tmp_path, five_rows) -> None:
    raw = timeline_raw(five_rows)
    encoded = base64.b64encode(gzip.compress(json.dumps(raw, ensure_ascii=False).encode())).decode()
    path = write_save(tmp_path / "手工.xlsx", json.loads(json.dumps(raw)))
    assert read_save(path).frames == five_rows
    assert len(encoded) < 32767
def test_keeps_the_first_five_semantic_rows_and_warns(tmp_path, five_rows) -> None:
    """多出来的语义车道摸轴器永远不会执行，所以只留前五行、丢掉其余并提示。"""
    raw = timeline_raw(five_rows)
    group = raw["playerGroupData"]
    group["SemanUBExecTimeData"] = [[1], [2], [3], [4], [5], [6, 7], [8]]
    group["useSemanMode"] = True
    group["SemanUBSequenceData"] = []
    path = write_save(tmp_path / "七行.xlsx", raw)

    notes: list[str] = []
    save = read_save(path, log=notes.append)

    assert save.semantic == [[1], [2], [3], [4], [5]]
    assert len(notes) == 1
    assert "只保留前 5 行" in notes[0]
    assert "丢掉了后面 2 行" in notes[0]


def test_treats_a_short_semantic_axis_as_empty(tmp_path, five_rows) -> None:
    """不足五条车道的语义数据用不了，整条当成没有（连角色序列一起忽略）。"""
    raw = timeline_raw(five_rows)
    group = raw["playerGroupData"]
    group["SemanUBExecTimeData"] = [[1], [2], [3]]
    group["useSemanMode"] = True
    group["SemanUBSequenceData"] = [0]
    path = write_save(tmp_path / "三行.xlsx", raw)

    notes: list[str] = []
    save = read_save(path, log=notes.append)

    assert save.semantic == []
    assert save.has_semantic is False
    assert save.sequence is None
    assert len(notes) == 1
    assert "只有 3 行" in notes[0] and "当成没有语义数据" in notes[0]