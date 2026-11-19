"""优化模式接口（modes.py）的测试：注册表、参数类型、两种内置模式。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from conftest import make_save, write_save

from PcrTimelineTester import modes
from PcrTimelineTester.ga.semantics import SemanSkillType, SemanUBType
from PcrTimelineTester.ga.settings import GaSettings
from PcrTimelineTester.modes import FrameSettings, ModeSpec, Param
from PcrTimelineTester.search import ThresholdMode


def test_builtin_modes_are_registered():
    assert modes.keys() == ("frame", "ga", "anneal", "roster", "zero", "cycle")
    assert modes.get("frame").needs_frames is True
    assert modes.get("ga").needs_frames is False
    assert modes.get("ga").workbook_name == "优化结果.xlsx"
    assert modes.get("anneal").needs_frames is False
    assert modes.get("anneal").workbook_name == "退火结果.xlsx"
    assert modes.get("roster").needs_frames is False
    assert modes.get("roster").workbook_name == "阵容结果.xlsx"
    assert modes.get("zero").needs_frames is False
    assert modes.get("zero").workbook_name == "0押结果.xlsx"
    assert modes.labels() == (
        ("frame", "逐帧搜索"),
        ("ga", "语义 GA"),
        ("anneal", "退火"),
        ("roster", "阵容搜索"),
        ("zero", "0押轴"),
        ("cycle", "循环搜索"),
    )


def test_unknown_mode_lists_the_known_ones():
    with pytest.raises(ValueError) as info:
        modes.get("nope")
    assert "不认识的优化模式：nope" in str(info.value)
    assert "frame" in str(info.value) and "ga" in str(info.value)


def test_param_coerces_every_kind():
    assert Param("a", "甲", "", "int", 0, 1, 9).coerce("4") == 4
    assert Param("b", "乙", "", "float", 0.0).coerce("2.5") == 2.5
    assert Param("c", "丙", "", "bool", False).coerce("开") is True
    assert Param("c", "丙", "", "bool", False).coerce(0) is False
    choice = Param("d", "丁", "", "choice", 1, choices=((1, "一"), (2, "二")))
    assert choice.coerce("二") == 2
    assert choice.coerce(1) == 1
    assert choice.text(2) == "二"


def test_param_rejects_out_of_range_and_wrong_kind():
    with pytest.raises(ValueError) as info:
        Param("a", "甲", "", "int", 0, 1, 9).coerce("40")
    assert "不能大于 9" in str(info.value)
    with pytest.raises(ValueError):
        Param("a", "甲", "", "int").coerce("1.5")
    with pytest.raises(ValueError):
        Param("a", "甲", "", "choice", 0, choices=((0, "零"),)).coerce("三")
    with pytest.raises(ValueError):
        Param("a", "甲", "", "bool").coerce("大概")


def test_frame_mode_builds_both_settings_objects(tmp_path, five_rows):
    save = make_save(tmp_path, five_rows)
    settings = modes.get("frame").from_values(
        {"step": 3, "threshold_mode": "百分比", "threshold_value": "0.02", "passes": "2"}, save,
    )
    assert isinstance(settings, FrameSettings)
    assert settings.search.step == 3
    assert settings.search.threshold_mode is ThresholdMode.PERCENT
    assert settings.search.threshold_value == pytest.approx(0.02)
    assert settings.job.passes == 2


def test_frame_mode_rejects_unknown_names(tmp_path, five_rows):
    save = make_save(tmp_path, five_rows)
    with pytest.raises(ValueError) as info:
        modes.get("frame").from_values({"population": 3}, save)
    assert "没有这些参数" in str(info.value)


def test_ga_mode_builds_ga_settings(tmp_path, five_rows):
    save = make_save(tmp_path, five_rows)
    settings = modes.get("ga").from_values(
        {"generations": "5", "ub_type": "SERIES_4", "skill_type": SemanSkillType.SERIES_10,
         "own_skill": "关"},
        save,
    )
    assert isinstance(settings, GaSettings)
    assert settings.generations == 5
    assert settings.ub_type is SemanUBType.SERIES_4
    assert settings.skill_type is SemanSkillType.SERIES_10
    assert settings.own_skill is False


def test_ga_values_round_trip_through_the_registry(tmp_path, five_rows):
    save = make_save(tmp_path, five_rows)
    spec = modes.get("ga")
    settings = spec.from_values(spec.defaults(), save)
    values = spec.values(settings)
    assert values["population"] == settings.population
    assert spec.from_values(values, save) == settings


@dataclass(frozen=True)
class _DummySettings:
    rounds: int = 2


def test_registry_accepts_a_new_mode_and_rejects_duplicates():
    calls: list[str] = []

    class Session:
        job = None

        def run(self) -> int:
            calls.append("run")
            return 0

        def close(self) -> None:
            calls.append("close")

    def build(values, save):
        return _DummySettings(rounds=values["rounds"])

    def session(save, settings, run_dir, **kwargs):
        assert settings.rounds == 3
        return Session()

    spec = ModeSpec(
        key="dummy", label="假模式", description="只用来验证扩展点", needs_frames=False,
        params=(Param("rounds", "轮数", "随便", "int", 2, 1, 9),),
        build=build, session=session, plan=lambda save, settings: [f"轮数 {settings.rounds}"],
    )
    modes.register(spec)
    try:
        assert modes.get("dummy") is spec
        assert "dummy" in modes.keys()
        settings = modes.get("dummy").from_values({"rounds": "3"}, None)
        assert settings.rounds == 3
        modes.get("dummy").open_session(None, settings, None).run()
        assert calls == ["run"]
        with pytest.raises(ValueError) as info:
            modes.register(spec)
        assert "已经注册过了" in str(info.value)
        modes.register(spec, replace=True)
    finally:
        modes.unregister("dummy")
    assert "dummy" not in modes.keys()


def test_read_save_follows_needs_frames(tmp_path, five_rows):
    save = make_save(tmp_path, five_rows, name="语义轴")
    save.raw["playerGroupData"]["UBExecTimeData"] = []
    save.raw["UBExecTime"] = []
    save.raw["playerGroupData"]["SemanUBExecTimeData"] = [[1, 2], [3], [4], [5], [6]]
    save.raw["playerGroupData"]["useSemanMode"] = True
    path = tmp_path / "semantic.xlsx"
    write_save(path, save.raw)
    with pytest.raises(ValueError) as info:
        modes.get("frame").read_save(path)
    assert "没有帧数据" in str(info.value)
    loaded = modes.get("ga").read_save(path)
    assert loaded.has_semantic is True


