"""The eight-stage window search, replaying the C# decision order."""

from __future__ import annotations

import pytest

from PcrTimelineTester.search import (
    MetricBook,
    SearchSettings,
    ThresholdMode,
    fmt,
    run,
    signed,
)


def landscape(frame: int, peak: int = 480, height: float = 1000.0) -> float:
    return height - abs(frame - peak) * 10.0


def make_book(metric, settings: SearchSettings, calls: list | None = None) -> MetricBook:
    def fetch(frames: list[int], tag: str) -> list[float]:
        if calls is not None:
            calls.append((tag, list(frames)))
        return [metric(frame) for frame in frames]

    return MetricBook(fetch=fetch, settings=settings, log=lambda _: None)


def test_finds_the_peak_frame() -> None:
    settings = SearchSettings(step=5, max_offset=20, chunk=4)
    book = make_book(landscape, settings)
    result = run(settings, 470, landscape(470), book, log=lambda _: None)
    assert result.ok and not result.cancelled
    assert result.peak_frame == 480
    assert result.peak_metric == pytest.approx(1000.0)
    assert result.gain == pytest.approx(100.0)
    assert result.threshold_start == min(result.visited)  # 绝对阈值很宽，全部帧都可接受
    assert result.battles == book.battles


def test_batching_does_not_change_the_outcome() -> None:
    outcomes = []
    for chunk in (1, 3, 16):
        settings = SearchSettings(step=5, max_offset=40, chunk=chunk)
        book = make_book(landscape, settings)
        result = run(settings, 470, landscape(470), book, log=lambda _: None)
        outcomes.append((result.peak_frame, dict(result.visited)))
    assert outcomes[0] == outcomes[1] == outcomes[2]


def test_each_frame_is_only_battled_once() -> None:
    settings = SearchSettings(step=5, max_offset=40, chunk=8)
    calls: list = []
    book = make_book(landscape, settings, calls)
    run(settings, 470, landscape(470), book, log=lambda _: None)
    requested = [frame for _, frames in calls for frame in frames]
    assert len(requested) == len(set(requested))


def test_early_stop_when_nothing_is_acceptable() -> None:
    """A strictly falling hill makes every upward probe unacceptable (strict mode)."""
    settings = SearchSettings(
        step=5, max_offset=30, chunk=8, threshold_mode=ThresholdMode.STRICT, threshold_value=0.0,
    )
    metric = lambda frame: -float(frame)  # noqa: E731
    book = make_book(metric, settings)
    result = run(settings, 470, metric(470), book, log=lambda _: None)
    assert result.ok
    # 向上第一帧就掉出阈值，外探也全更低，于是峰值落到向下（伤害更高）的窗口下界，
    # ⑥ 的逐帧补齐再往下走 fillCap = 30 + (3+2)*5 = 55 帧 → 440-55 = 385。
    assert result.peak_frame == 385
    assert result.threshold_start != -1


def test_absolute_threshold_keeps_probing_but_never_picks_worse() -> None:
    settings = SearchSettings(step=5, max_offset=20, chunk=8, threshold_value=1_000.0)
    book = make_book(landscape, settings)
    result = run(settings, 470, landscape(470), book, log=lambda _: None)
    assert result.peak_frame == 480
    assert result.threshold_start == 450 and result.threshold_end == 490


def test_battle_cap_aborts_the_item() -> None:
    settings = SearchSettings(step=5, max_offset=100, chunk=8, max_battles=4)
    book = make_book(landscape, settings)
    result = run(settings, 470, landscape(470), book, log=lambda _: None)
    assert result.ok is False
    assert "场次上限" in result.message
    assert result.peak_frame == 470 and result.peak_metric == pytest.approx(900.0)


def test_cancel_restores_the_baseline() -> None:
    settings = SearchSettings(step=5, max_offset=20, chunk=8)
    book = make_book(landscape, settings)
    result = run(settings, 470, landscape(470), book, log=lambda _: None, cancelled=lambda: True)
    assert result.cancelled
    assert result.peak_frame == 470 and result.peak_metric == pytest.approx(900.0)


def test_sample_failure_aborts_instead_of_counting_zero() -> None:
    settings = SearchSettings(step=5, max_offset=20, chunk=4)

    def fetch(frames: list[int], tag: str) -> list[float]:
        raise RuntimeError("接口请求失败")

    book = MetricBook(fetch=fetch, settings=settings, log=lambda _: None)
    with pytest.raises(RuntimeError):
        run(settings, 470, landscape(470), book, log=lambda _: None)


def test_number_formatting() -> None:
    assert fmt(1234567.0) == "1,234,567"
    assert fmt(0.5) == "0.5"
    assert signed(100.0) == "+100"
    assert signed(-100.0) == "-100"


def test_settings_reject_silly_values() -> None:
    with pytest.raises(ValueError):
        SearchSettings(step=0)
    with pytest.raises(ValueError):
        SearchSettings(frame_min=100, frame_max=100)
    with pytest.raises(ValueError):
        SearchSettings(threshold_mode=ThresholdMode.PERCENT, threshold_value=2)


def test_settings_round_trip() -> None:
    settings = SearchSettings(step=3, outer_probes=5, threshold_mode=ThresholdMode.PERCENT, threshold_value=0.02)
    same = SearchSettings.from_dict(settings.to_dict())
    assert same == settings
    assert "外探 5" in same.describe()
