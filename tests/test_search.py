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


def test_frames_are_clamped_to_the_playable_window() -> None:
    loose = SearchSettings(frame_min=0, frame_max=99_999)
    assert (loose.frame_min, loose.frame_max) == (90, 5399)
    assert SearchSettings(frame_min=10).frame_min == 90
    assert SearchSettings(frame_max=6000).frame_max == 5399
    with pytest.raises(ValueError):
        SearchSettings(frame_min=5399)


def test_stops_going_down_once_the_target_ub_cannot_fire() -> None:
    """放不出来的方向不再往前探：第一次发现后最多再探 min(5, 外探次数) 次。"""

    def make(ub_row: int):
        def fetch(frames: list[int], tag: str) -> list[float]:
            return [landscape(frame) for frame in frames]

        settings = SearchSettings(step=5, max_offset=100, outer_probes=3, chunk=1)
        book = MetricBook(fetch=fetch, settings=settings, log=lambda _: None)
        # 目标 UB 在任何请求帧上都放不出来：实际 UB 总比请求的帧晚 100 帧
        for frame in range(80, 6200):
            book.ub[frame] = ((float(frame + 100),),)
        result = run(
            settings, 500, landscape(500), book,
            ub_row=ub_row, ub_index=0, log=lambda _: None,
        )
        # visited = 真正消费掉的帧（≈ 实际打的那些帧）
        return result, sorted([frame for frame in result.visited if frame < 500], reverse=True)

    result, downward = make(0)
    assert result.ok
    # 向下从 495 开始，一次次都放不出来：最多再探 min(5, 3) = 3 次，到 480 就收手
    assert {495, 490, 485, 480} <= set(downward)
    assert min(downward) >= 470  # 没有一路探到窗口下界

    # 不给 UB 信息时照旧往下试到底（窗口下界 400）
    _, control = make(-1)
    assert min(control) == 400


def test_never_picks_a_frame_where_the_ub_cannot_fire() -> None:
    """伤害一样时取最早帧，但放不出 UB 的帧不算候选。"""

    def fetch(frames: list[int], tag: str) -> list[float]:
        return [1000.0 for _ in frames]  # 完全平坦

    settings = SearchSettings(step=5, max_offset=20, chunk=4)
    book = MetricBook(fetch=fetch, settings=settings, log=lambda _: None)
    # 500 帧之后要哪帧给哪帧；早于 500 帧按不出来，摸轴器会把它挪到 500
    for frame in range(90, 6200):
        book.ub[frame] = ((float(frame) if frame >= 500 else 500.0,),)
    result = run(settings, 500, 1000.0, book, ub_row=0, ub_index=0, log=lambda _: None)
    assert result.peak_frame == 500


def test_settings_round_trip() -> None:
    settings = SearchSettings(step=3, outer_probes=5, threshold_mode=ThresholdMode.PERCENT, threshold_value=0.02)
    same = SearchSettings.from_dict(settings.to_dict())
    assert same == settings
    assert "外探 5" in same.describe()
