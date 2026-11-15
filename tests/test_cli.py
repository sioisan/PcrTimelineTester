"""命令行那点纯逻辑：``--set`` 的解析与类型转换。

真机跑过一轮之后补的：``--set restarts=2`` 传进来是字符串，必须按模式声明的
``Param`` 强转成 int，否则 GaSettings 会报「重启轮数 必须是大于等于 1 的整数」；
同一个 ``--set`` 又会同时套到 job 与 search 两个对象上，缺字段不算错。
"""

from __future__ import annotations

import argparse

from PcrTimelineTester import cli, modes
from PcrTimelineTester.ga.semantics import SemanUBType
from PcrTimelineTester.ga.settings import GaSettings
from PcrTimelineTester.job import JobSettings
from PcrTimelineTester.search import SearchSettings


def test_overrides_parses_name_value_pairs() -> None:
    args = argparse.Namespace(set_values=["restarts=2", " elite_count = 4 "])
    assert cli._overrides(args) == {"restarts": "2", "elite_count": "4"}
    assert cli._overrides(argparse.Namespace(set_values=None)) == {}
    try:
        cli._overrides(argparse.Namespace(set_values=["restarts"]))
    except ValueError as error:
        assert "--set 需要 名字=值 的形式" in str(error)
    else:  # pragma: no cover - 没报错就是失败
        raise AssertionError("缺少 = 时应该报错")


def test_ga_overrides_are_coerced_to_declared_types() -> None:
    spec = modes.get(modes.GA)
    settings = cli._apply_overrides(
        spec,
        GaSettings(),
        {"restarts": "2", "elite_count": "4", "mutation_decay": "0.85", "ub_type": "SERIES_4"},
    )
    assert isinstance(settings, GaSettings)
    assert settings.restarts == 2 and settings.elite_count == 4
    assert settings.mutation_decay == 0.85
    assert settings.ub_type is SemanUBType.SERIES_4


def test_frame_overrides_land_only_on_objects_that_have_the_field() -> None:
    spec = modes.get(modes.FRAME)
    overrides = {"passes": "3", "step": "10", "threshold_mode": "百分比", "threshold_value": "0.02"}
    job = cli._apply_overrides(spec, JobSettings(), overrides)
    search = cli._apply_overrides(spec, SearchSettings(), overrides)
    assert isinstance(job, JobSettings) and job.passes == 3
    assert isinstance(search, SearchSettings) and search.step == 10
    assert search.threshold_mode.label == "百分比"
    assert search.threshold_value == 0.02


def test_overrides_reject_names_the_mode_does_not_have() -> None:
    spec = modes.get(modes.GA)
    try:
        cli._apply_overrides(spec, GaSettings(), {"step": "10"})
    except ValueError as error:
        assert "语义 GA 模式没有这些参数：step" in str(error)
    else:  # pragma: no cover - 没报错就是失败
        raise AssertionError("未知参数应该报错")


def test_overrides_keep_the_original_object_when_empty() -> None:
    spec = modes.get(modes.FRAME)
    settings = JobSettings()
    assert cli._apply_overrides(spec, settings, {}) is settings
