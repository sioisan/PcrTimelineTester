"""阵容搜索：分组枚举 + 每队 32 种 AUTO/SET。"""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from PcrTimelineTester.roster import (
    lineup_combo,
    lineup_names,
    lineup_order,
    lineup_text,
    GroupSpec,
    RosterJob,
    RosterSettings,
    TeamEvaluator,
    TeamScore,
    combo_text,
    evaluate_team,
    fixed_groups,
    iter_teams,
    save_with_team,
    team_count,
    team_quota,
    toggle_text,
    ungrouped,
)
from conftest import make_save

POOL = tuple(range(1001, 1013))  # 12 个角色


def _save(tmp_path, frames=None):
    frames = frames if frames is not None else [[470], [500], [600], [700], [800]]
    save = make_save(tmp_path, frames)
    save.enemy_ids = [401911401]
    return save


def _settings(**overrides):
    base = dict(
        pool=POOL,
        groups=(),
        battle_time=90,
        seed=666,
        samples=1,
        batch_size=64,
        every_combo=False,
        to_max_attr=True,
    )
    base.update(overrides)
    return RosterSettings(**base)


# ---------------------------------------------------------------- 分组与枚举
def test_group_spec_validates_and_round_trips():
    group = GroupSpec(name="A", members=(1001, 1002, 1003), minimum=2)
    assert group.used is True
    assert group.to_dict() == {"name": "A", "members": [1001, 1002, 1003], "minimum": 2}
    assert GroupSpec.from_dict(group.to_dict()) == group
    assert "A" in group.label and "2" in group.label
    with pytest.raises(ValueError):
        GroupSpec(name="", members=(1001,), minimum=1)
    with pytest.raises(ValueError):
        GroupSpec(name="A", members=(1001,), minimum=2)
    # 重复成员会被去重，不会报错
    assert GroupSpec(name="A", members=(1001, 1001), minimum=1).members == (1001,)


def test_settings_reject_bad_groups():
    with pytest.raises(ValueError):
        _settings(pool=(1001, 1002, 1003, 1004), groups=())
    with pytest.raises(ValueError):
        _settings(groups=(GroupSpec("A", (1001, 1002), 1), GroupSpec("A", (1003,), 1)))
    assert team_count(_settings(groups=(GroupSpec("A", (9999,), 1),))) == 0
    with pytest.raises(ValueError):
        _settings(groups=(GroupSpec("A", (1001, 1002), 3), GroupSpec("B", (1003, 1004), 3)))


def test_group_helpers_split_members_and_quota():
    settings = _settings(groups=(GroupSpec("A", (1001, 1002, 1003), 2), GroupSpec("B", (1004,), 1)))
    assert [group.name for group in fixed_groups(settings)] == ["A", "B"]
    assert team_quota(settings) == 3
    assert 1001 not in ungrouped(settings) and 1005 in ungrouped(settings)


def test_team_count_matches_iter_teams():
    settings = _settings(groups=(GroupSpec("A", (1001, 1002, 1003), 2), GroupSpec("B", (1004,), 1)))
    teams = list(iter_teams(settings))
    assert len(teams) == team_count(settings)
    # A 可以上两人或三人。B 的必上角色占一席。
    assert len(teams) == 3 * 28 + 8
    for team in teams:
        assert len(team) == 5
        assert len(set(team)) == 5
        assert sum(1 for unit in team if unit in (1001, 1002, 1003)) >= 2
        assert 1004 in team


def test_overlap_deduplicates_group_seats_before_filling_the_team():
    groups = (GroupSpec("A", (1001, 1002, 1003), 1), GroupSpec("B", (1001, 1002, 1004), 1))
    settings = _settings(groups=groups)
    teams = list(iter_teams(settings))
    expected = {frozenset(team) for team in itertools.combinations(POOL, 5)
                if all(len(set(group.members).intersection(team)) >= group.minimum for group in groups)}
    assert set(map(frozenset, teams)) == expected
    assert len(teams) == team_count(settings) == len(expected)


def test_iter_teams_without_groups_is_plain_combinations():
    settings = _settings(pool=(1001, 1002, 1003, 1004, 1005, 1006))
    teams = list(iter_teams(settings))
    assert len(teams) == 6
    assert teams == [tuple(sorted(team)) for team in itertools.combinations(sorted(settings.pool), 5)]


def test_ungrouped_group_is_merged_into_the_pool():
    # minimum = 0 的分组等于没分组
    settings = _settings(groups=(GroupSpec("A", (1001, 1002), 0),))
    assert fixed_groups(settings) == ()
    assert team_quota(settings) == 0
    assert 1001 in ungrouped(settings)


# ---------------------------------------------------------------- 评估
class FakeEvaluator:
    def warm_pool(self, cancelled):
        return len(self.settings.pool)

    """按 unitId 权重给分；toggles 只影响 0.5 的系数，方便验证「取最高」。"""

    def __init__(self, save, settings, weights=None):
        self.save = save
        self.settings = settings
        self.weights = weights or {unit: unit - 1000 for unit in POOL}
        self.calls = []
        self.battles = 0

    def combos(self, size=5):
        return [(False,) * size] if not self.settings.every_combo else list(
            itertools.product((False, True), repeat=size)
        )

    def evaluate(self, teams):
        scores = []
        for team in teams:
            combos = self.combos(len(team))
            best = None
            for toggles in combos:
                self.battles += 1
                self.calls.append((tuple(team), toggles))
                damage = int(sum(self.weights.get(unit, 0) for unit in team) * (10 if not any(toggles) else 5))
                score = TeamScore(damage=damage, frames=1800, result=0, toggles=tuple(toggles))
                if best is None or score.damage > best.damage:
                    best = score
            scores.append(best)
        return scores

    def close(self):
        pass


def test_evaluator_tries_every_combo_and_keeps_the_best(tmp_path):
    save = _save(tmp_path)
    settings = _settings(every_combo=True, pool=(1001, 1002, 1003, 1004, 1005))
    fake = FakeEvaluator(save, settings)
    scores = fake.evaluate([(1001, 1002, 1003, 1004, 1005)])
    assert len(fake.calls) == 32
    assert len({toggles for _team, toggles in fake.calls}) == 32
    assert scores[0].toggles == (False,) * 5


def test_real_evaluator_builds_set_toggles(tmp_path):
    save = _save(tmp_path)
    evaluator = TeamEvaluator(save, _settings(every_combo=True))
    body = evaluator.request_for([1001, 1002, 1003, 1004, 1005], toggles=(True, False, True, False, True))
    evaluator.close()
    assert body["SetToggles"] == [True, False, True, False, True]
    # 有 AUTO（X）的槽位靠 IsAutoMode 出手，SET（O）的槽位靠 SetToggles
    assert body["IsSetMode"] is False and body["IsAutoMode"] is True
    assert [player["unitId"] for player in body["PlayerList"]] == [1001, 1002, 1003, 1004, 1005]


def test_combo_and_toggle_text():
    # O = SET（一有机会就按），X = AUTO（角色自己找时机）
    assert combo_text((True, False, True, False, True)) == "OXOXO"
    assert combo_text((True,) * 5) == "OOOOO" and combo_text((False,) * 5) == "XXXXX"
    assert toggle_text((True, False)) == "SET（O）、AUTO（X）"


def test_labyrinth_uses_each_units_gear_in_search_and_result(tmp_path, monkeypatch):
    save = _save(tmp_path)
    save.enemy_ids = [770000001]
    original = save.unit_ids[0]
    save.players[0]["level"] = 67
    newcomer = 1009
    team = (*save.unit_ids[:4], newcomer)
    flags = (True, False, False, True, True)
    probes = []

    def fetch(requests):
        body, = requests
        players = body["PlayerList"]
        if body["ToMaxUnitAttr"]:
            probes.append(body)
            players = [{"unitId": p["unitId"], "level": 412,
                        "exEquip": [4110501, 4203501, 4305511] if p["unitId"] == newcomer
                        else [4101501, 4201501, 4302514]} for p in reversed(players)]
        return [{"Success": True, "CurrentSeed": body["Settings"]["RandomSeed"],
                 "PlayerList": players, "Stats": {"TotalDamage": 777, "UBTime": [[310]] * 5}}]

    for full in (False, True):
        probes.clear()
        with TeamEvaluator(save, _settings(to_max_attr=full)) as evaluator:
            monkeypatch.setattr(evaluator, "_fetch", fetch)
            body = evaluator.request_for(team, toggles=flags)
            assert not body["ToMaxUnitAttr"]
            players = {p["unitId"]: p for p in body["PlayerList"]}
            assert players[original]["level"] == (412 if full else 67)
            assert players[original]["exEquip"] == [4101511, 4201503, 4302514]
            assert players[newcomer]["exEquip"] == [4110511, 4203501, 4305511]
            assert players[newcomer]["exEquipExceedRank"] == [0, 0, 5]
            assert players[newcomer]["exEquipSubs"] == {"0": [5, 4]*4, "1": [5, 4]*4, "2": [2, 4]*4}
            assert players[original]["exEquipSubs"]["2"] == [1, 4]*4
            tuned = evaluator.result_save(team, flags)
            assert tuned.group["playerData"]["playrCharacters"] == body["PlayerList"]
            assert tuned.frames == [[310]] * 5
            assert len(probes) == 1


# ---------------------------------------------------------------- 任务
def _job(tmp_path, **overrides):
    save = _save(tmp_path)
    settings = _settings(**overrides)
    job = RosterJob(save, settings, log=lambda _line: None, evaluator=FakeEvaluator(save, settings))
    return job, settings


def test_chunk_size_feeds_the_parallel_posts(tmp_path):
    """一批要凑够 POST_WORKERS 个请求，否则并发发不出去（1 万支队伍会慢 4 倍）。"""
    from PcrTimelineTester.roster import POST_WORKERS

    both, _ = _job(tmp_path, pool=(1001, 1002, 1003, 1004, 1005, 1006), every_combo=True)
    assert len(both.evaluator.combos(5)) == 32
    assert both._chunk_size() >= POST_WORKERS

    single, _ = _job(tmp_path, pool=(1001, 1002, 1003, 1004, 1005, 1006))
    assert len(single.evaluator.combos(5)) == 1
    assert single._chunk_size() >= POST_WORKERS


def test_job_enumerates_all_teams_and_exports_best(tmp_path):
    job, settings = _job(tmp_path, pool=(1001, 1002, 1003, 1004, 1005, 1006, 1007))
    summary = job.run()
    assert summary["teams_total"] == 21
    assert summary["teams_done"] == 21
    assert summary["battles"] == 21  # 原队的参考成绩可复用
    assert summary["best_team"] == [1003, 1004, 1005, 1006, 1007]
    assert summary["export"]["team"] == [1003, 1004, 1005, 1006, 1007]
    assert summary["reference"] is not None


def test_job_cancel_does_not_export(tmp_path):
    job, _settings_obj = _job(tmp_path, pool=(1001, 1002, 1003, 1004, 1005, 1006))

    class CancelAfterOne(FakeEvaluator):
        def evaluate(self, teams):
            scores = super().evaluate(teams)
            job.cancel_run()
            return scores

    job._evaluator = CancelAfterOne(job.save, job.settings)
    summary = job.run()
    assert summary["cancelled"] is True
    assert job.export is None


def test_state_round_trip_skips_done_teams(tmp_path):
    job, _settings_obj = _job(tmp_path, pool=(1001, 1002, 1003, 1004, 1005, 1006))
    job.run()
    state = job.state_dict()
    assert state["cursor"] == job.teams_total

    resumed, _ = _job(tmp_path, pool=(1001, 1002, 1003, 1004, 1005, 1006))
    resumed.apply_state(state)
    assert resumed.teams_done == state["cursor"]
    assert resumed.best_team == tuple(state["best_team"])


def test_save_with_team_replaces_the_roster(tmp_path):
    save = _save(tmp_path)
    tuned = save_with_team(save, (1003, 1004, 1005, 1006, 1007))
    assert tuned.unit_ids == [1003, 1004, 1005, 1006, 1007]
    assert [player["unitId"] for player in tuned.group["playerData"]["playrCharacters"]] == [
        1003, 1004, 1005, 1006, 1007
    ]
    assert save.unit_ids == [1001, 1002, 1003, 1004, 1005]  # 原轴不动


class _StubPostEvaluator(TeamEvaluator):
    """不联网：伤害 = 组合的二进制值（SET 越多越大），帧/结果固定。"""

    def _post(self, requests):
        replies = []
        for request in requests:
            flags = tuple(bool(flag) for flag in request["SetToggles"])
            value = sum(1 << index for index, flag in enumerate(flags) if flag)
            replies.append({"damage": 1000 + value, "frames": 1800, "result": 1})
        return replies


def test_evaluate_all_posts_every_combo_at_once_and_sorts_by_damage(tmp_path):
    """「试这队 32 种」一次性把 2^5 条请求发出去，并按伤害降序返回。"""
    save = _save(tmp_path)
    settings = _settings(every_combo=True, pool=(1001, 1002, 1003, 1004, 1005))
    evaluator = _StubPostEvaluator(save, settings, log=lambda line: None)
    try:
        rows_all = evaluator.evaluate_all((1001, 1002, 1003, 1004, 1005))
        assert len(rows_all) == 32
        assert rows_all[0][0] == (True,) * 5 and rows_all[-1][0] == (False,) * 5
        damages = [row[1].damage for row in rows_all]
        assert damages == sorted(damages, reverse=True)
        with pytest.raises(ValueError):
            evaluator.evaluate_all((1001, 1001, 1003, 1004, 1005))
        with pytest.raises(ValueError):
            evaluator.evaluate_all((1001, 1002, 1003))
    finally:
        evaluator.close()


def test_evaluate_team_uses_the_batch_channel(tmp_path):
    save = _save(tmp_path)
    settings = _settings(every_combo=False, pool=(1001, 1002, 1003, 1004, 1005))
    evaluator = _StubPostEvaluator(save, settings, log=lambda line: None)
    lines = []
    try:
        rows_all = evaluate_team(
            save, settings, (1001, 1002, 1003, 1004, 1005), log=lines.append, evaluator=evaluator
        )
    finally:
        evaluator.close()
    # 单队试算固定跑满 32 种，即使阵容搜索只勾了全 SET。
    assert {combo for combo, _score in rows_all} == set(itertools.product((False, True), repeat=5))
    assert lines and "→" in lines[0]


def test_requests_never_carry_the_axis_frames_and_wire_auto_mode(tmp_path):
    """轴里的帧是帧模式的手动刀表，不是 AUTO / SET：阵容搜索一律不带。

    AUTO 靠 `IsAutoMode`（角色自己按 UB），SET 靠 `SetToggles[i]`（pressing）。
    """
    save = _save(tmp_path)
    assert save.frames  # 这条轴是有帧的，照样不能带上
    settings = _settings(every_combo=True, pool=(1001, 1002, 1003, 1004, 1005))
    evaluator = TeamEvaluator(save, settings, log=lambda line: None)
    team = [1001, 1002, 1003, 1004, 1005]
    try:
        body_set = evaluator.request_for(team, toggles=(True,) * 5)
        body_auto = evaluator.request_for(team, toggles=(False,) * 5)
        body_mixed = evaluator.request_for(team, toggles=(True, False, True, False, True))
    finally:
        evaluator.close()
    for body in (body_set, body_auto, body_mixed):
        assert body["UBExecTimeList"] == []
        assert body["SemanUBExecTimeList"] == [] and body["IsSemanMode"] is False
        assert body["IsSetMode"] is False
    assert body_set["IsAutoMode"] is False
    assert body_auto["IsAutoMode"] is True
    assert body_mixed["IsAutoMode"] is True
    assert body_set["SetToggles"] == [True] * 5
    assert body_auto["SetToggles"] == [False] * 5
    assert body_mixed["SetToggles"] == [True, False, True, False, True]


def test_requests_send_players_in_station_order_so_toggles_bind_to_characters(tmp_path, monkeypatch):
    """`SetToggles[i]` 是按站位槽位生效的，所以请求必须按站位排、toggles 跟着排。

    否则「涅妃 SET」会被服务器理解成「1 号位 SET」，界面上的 OX 就跟真实操作对不上。
    """
    from PcrTimelineTester import datas

    monkeypatch.setattr(
        datas,
        "_info_table",
        {
            int(unit): datas.UnitInfo(width=width)
            for unit, width in WIDTHS.items()
        },
    )
    save = _save(tmp_path)
    settings = _settings(every_combo=True, pool=(136001, 134901, 128701, 136201, 129701))
    evaluator = TeamEvaluator(save, settings, log=lambda line: None)
    team = [136001, 134901, 128701, 136201, 129701]  # 蜘蛛、花音、星猫、纳娜、涅妃（枚举顺序）
    toggles = (True, False, False, False, True)  # 蜘蛛 SET、花音 AUTO … 涅妃 SET
    try:
        body = evaluator.request_for(team, toggles=toggles)
    finally:
        evaluator.close()
    order = [int(player["unitId"]) for player in body["PlayerList"]]
    assert order == [134901, 136201, 128701, 129701, 136001]  # 花音、纳娜、星猫、涅妃、蜘蛛
    assert body["SetToggles"] == [False, False, False, True, True]  # 涅妃与蜘蛛 SET
    assert body["IsAutoMode"] is True


def test_combos_do_not_need_a_frame_axis(tmp_path):
    """SET 只设 pressing（一有机会就按），和帧轴无关：没有帧轴照样能试 32 种。"""
    # 只有语义数据、没有帧轴的轴（帧模式读不了，阵容搜索照样能用）
    save = make_save(tmp_path, [], semantic=[[5, 10, 15], [10], [12], [20], [7]])
    save.enemy_ids = [401911401]
    assert not save.frames and save.has_semantic
    settings = _settings(every_combo=True, pool=(1001, 1002, 1003, 1004, 1005))
    evaluator = TeamEvaluator(save, settings, log=lambda line: None)
    try:
        combos = evaluator.combos(5)
        assert len(combos) == 32
        assert (True,) * 5 in combos and (False,) * 5 in combos
        body = evaluator.request_for([1001, 1002, 1003, 1004, 1005], toggles=(True,) * 5)
        assert body["UBExecTimeList"] == []
        assert body["SetToggles"] == [True] * 5
        assert body["IsSetMode"] is False
    finally:
        evaluator.close()
    plain = TeamEvaluator(save, _settings(every_combo=False, pool=(1001, 1002, 1003, 1004, 1005)), log=lambda line: None)
    try:
        assert plain.combos(5) == [(True,) * 5]
    finally:
        plain.close()


# ------------------------------------------------------------ 站位（显示用）
WIDTHS = {136001: 732, 134901: 196, 128701: 226, 136201: 198, 129701: 540}


def _width(unit: int) -> int | None:
    return WIDTHS.get(unit)


def test_lineup_order_sorts_by_width_then_unit_id() -> None:
    team = (136001, 134901, 128701, 136201, 129701)
    assert lineup_order(team, width=_width) == (134901, 136201, 128701, 129701, 136001)
    # 宽度小的在前；没有宽度的排最后（按 unitId）
    assert lineup_order((9, 7, 136001), width=lambda unit: 500 if unit == 9 else _width(unit)) == (9, 136001, 7)
    assert lineup_order((5, 3), width=lambda unit: None) == (3, 5)
    # 攻击范围相同：角色 ID 小的站前面
    assert lineup_order((136201, 130001), width=lambda unit: 200) == (130001, 136201)
    assert lineup_order((120801, 110801, 100801), width=lambda unit: 100) == (100801, 110801, 120801)


def test_lineup_combo_follows_the_real_positions() -> None:
    # 队伍顺序是 蜘蛛、花音；站位是 花音（1 号）、蜘蛛（2 号）
    team = (136001, 134901)
    toggles = (True, False)  # 蜘蛛 SET、花音 AUTO
    assert combo_text(toggles) == "OX"  # 队伍顺序
    assert lineup_combo(team, toggles, width=_width) == "XO"  # 站位顺序：1 花音 AUTO、2 蜘蛛 SET


def test_lineup_text_binds_compact_operations_in_station_order() -> None:
    names = {136001: "蜘蛛", 134901: "花音", 128701: "星猫", 136201: "纳娜", 129701: "涅妃"}
    team = (136001, 134901, 128701, 136201, 129701)
    toggles = (True, False, False, False, True)
    text = lineup_text(team, toggles, names, width=_width)
    assert text == (
        "花音(X)、纳娜(X)、星猫(X)、涅妃(O)、蜘蛛(O)"
    )
    assert lineup_names(team, names, width=_width) == "花音、纳娜、星猫、涅妃、蜘蛛"


def test_shared_members_can_satisfy_more_than_five_combined_minima():
    shared = POOL[:3]
    settings = _settings(groups=(GroupSpec("甲", shared, 3), GroupSpec("乙", shared + (9999,), 3)))
    teams = list(iter_teams(settings))
    assert len(teams) == team_count(settings) == 36
    assert all(set(shared) <= set(team) <= set(POOL) for team in teams)


def test_warm_pool_covers_new_roles_before_formal_requests(tmp_path, monkeypatch):
    pool = tuple(range(1001, 1013))
    settings = _settings(pool=pool)
    recorded = []
    with TeamEvaluator(_save(tmp_path), settings) as evaluator:
        monkeypatch.setattr(evaluator, "_post", lambda bodies: recorded.extend(bodies))
        assert evaluator.warm_pool(lambda: False) == len(pool)
    teams = [set(p["unitId"] for p in body["PlayerList"]) for body in recorded]
    assert len(teams) == 3 and teams[0].isdisjoint(teams[1])
    assert set.union(*teams) == set(pool)
    assert all(body["BattleTime"] == 1 for body in recorded)


def test_nonclan_gear_cross_product_scored_per_choice_and_seed(tmp_path):
    from PcrTimelineTester.roster import accessory_options, normal_equipment
    save = _save(tmp_path)
    save.enemy_ids = [410000001]  # 特殊 Boss
    team = tuple(reversed(save.unit_ids))
    settings = _settings(pool=team, every_combo=True, samples=2, batch_size=8,
                         accessories=("攻击", "防御"), accessory_locks={team[0]: "TP", team[1]: "防御"})
    assert not save.is_clan_battle
    class GearEvaluator(TeamEvaluator):
        def _post(self, requests):
            assert len(requests) <= self.settings.batch_size * POST_WORKERS
            result = []
            for body in requests:
                gear = {p["unitId"]: p["exEquip"][2] % 1000 for p in body["PlayerList"]}
                assert gear[team[0]] == 601 and gear[team[1]] == 519
                score = sum(gear.values()) + sum(body["SetToggles"])
                result.append(dict(damage=score, frames=500, result=2))
            self.battles += len(result)
            return result
    from PcrTimelineTester.roster import POST_WORKERS
    with GearEvaluator(save, settings) as evaluator:
        evaluator._max_players = {u: {"unitId": u, "exEquip": [4101511, 4201503, 4301514]} for u in team}
        best, = evaluator.evaluate([team])
        assert best.accessories == ("TP", "防御", "防御", "防御", "防御")
        assert all(best.toggles) and evaluator.battles == 8 * 32 * 2
        rows = evaluator.evaluate_all(team)
        assert len(rows) == 8 * 32 and rows[0][1] == best
    assert normal_equipment({"unitId": team[0], "exEquip": [4101511,4201503,4301514]}, "TP")["exEquipSubs"]["2"] == [5,4]*4
    assert accessory_options(settings, team, True) == ()
    assert RosterSettings.from_dict(settings.to_dict()) == settings


def test_accessory_defaults_and_clan_detection(tmp_path):
    from PcrTimelineTester.roster import accessory_options
    save = _save(tmp_path)
    team = tuple(save.unit_ids)
    assert accessory_options(_settings(pool=team), team, False) == (("攻击",),) * 5
    for boss, clan in [(401911401, True), (410000001, False), (770423002, False), (501000001, False)]:
        save.enemy_ids = [boss]
        assert save.is_clan_battle is clan
