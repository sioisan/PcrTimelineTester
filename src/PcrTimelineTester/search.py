"""Eight-stage peak-earliest window search.

One sample is one battle on the standalone PCRCalculator server.

Stage order:

    ① 立基线
    ② 向上按步长探
    ③ 向上外探（最多 N 次）
    ④ 向下按步长探
    ⑤ 向下外探
    ⑥ 峰值左侧逐帧补齐
    ⑦ 峰值右侧逐帧补齐
    ⑧ 收尾择优（同伤害取最早帧）

The threshold only decides whether to keep probing outward; it never decides the
final frame.  Sampling is batched: each block of ``chunk`` frames goes out as one
``/simulate`` request array, but only the frames the walk actually consumes enter
the candidate set of stage ⑧, so batching never changes the outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
import time
from typing import Callable, Iterator


class ThresholdMode(str, Enum):
    """How much damage a probed frame may lose and still count as acceptable."""

    ABSOLUTE = "absolute"
    PERCENT = "percent"
    STRICT = "strict"

    @property
    def label(self) -> str:
        """Chinese label shown in the UI and in reports."""
        return {
            ThresholdMode.ABSOLUTE: "绝对伤害",
            ThresholdMode.PERCENT: "百分比",
            ThresholdMode.STRICT: "不允许掉伤害",
        }[self]

    @classmethod
    def parse(cls, value: "str | ThresholdMode") -> "ThresholdMode":
        """Accept the wire value, the Chinese label, or a mode instance."""
        if isinstance(value, ThresholdMode):
            return value
        text = str(value).strip()
        for mode in cls:
            if text in (mode.value, mode.label, mode.name):
                return mode
        raise ValueError(f"未知的阈值口径：{value}（可用：绝对伤害 / 百分比 / 不允许掉伤害）")


class SearchAbort(RuntimeError):
    """Fatal for the current item: sample failure, battle cap reached, ..."""


class SearchCancelled(RuntimeError):
    """The user interrupted the run."""


def fmt(value: float) -> str:
    number = float(value)
    return f"{number:,.0f}" if number.is_integer() else f"{number:,.1f}"


def signed(value: float) -> str:
    return f"+{fmt(value)}" if value > 0 else fmt(value)


# 一场公会战里能被按到的帧数：早于 90 帧还放不出 UB，晚于 5399 帧战斗已经结束，
# 两头都探没有意义，所以当成硬边界。
FRAME_FLOOR = 90
FRAME_CEILING = 5399
# 目标 UB 在某一帧放不出来（实际 UB 跑到更后面去了）时，向下方向最多再探几次就收手。
UB_MISS_LIMIT = 5
# 同帧多刀 UB 用小数尾（550.001）区分顺序，所以比较实际帧时留这么点余量。
UB_FRAME_TOLERANCE = 0.5


@dataclass(frozen=True, slots=True)
class SearchSettings:
    """Window parameters of the search (the plugin's ``SearchOptions``)."""

    step: int = 5
    max_offset: int = 100
    outer_probes: int = 3
    threshold_mode: ThresholdMode = ThresholdMode.ABSOLUTE
    threshold_value: float = 500_000.0
    frame_min: int = FRAME_FLOOR
    frame_max: int = FRAME_CEILING
    peak_tolerance: float = 0.5
    memoize: bool = True
    max_battles: int = 0          # 0 = 不限制场次
    chunk: int = 8

    def __post_init__(self) -> None:
        if self.step <= 0:
            raise ValueError("步长必须是正整数")
        if self.max_offset <= 0:
            raise ValueError("最大偏移必须是正整数")
        if self.outer_probes < 0:
            raise ValueError("外探次数不能是负数")
        # 帧范围夹到「游戏里按得到」的区间：低于 90 帧放不出 UB，高于 5399 帧没战斗了
        frame_min = min(max(int(self.frame_min), FRAME_FLOOR), FRAME_CEILING)
        frame_max = min(max(int(self.frame_max), FRAME_FLOOR), FRAME_CEILING)
        if frame_max <= frame_min:
            raise ValueError(f"帧范围无效：{frame_min}~{frame_max}（合法区间 {FRAME_FLOOR}~{FRAME_CEILING}）")
        object.__setattr__(self, "frame_min", frame_min)
        object.__setattr__(self, "frame_max", frame_max)
        if self.max_battles < 0:
            raise ValueError("场次上限不能是负数（0 表示不限制）")
        if self.chunk <= 0:
            raise ValueError("批大小必须是正整数")
        if not math.isfinite(self.threshold_value) or self.threshold_value < 0:
            raise ValueError("阈值不能是负数")
        if not math.isfinite(self.peak_tolerance) or self.peak_tolerance < 0:
            raise ValueError("同值容差不能是负数")
        if self.threshold_mode is ThresholdMode.PERCENT and self.threshold_value > 1:
            raise ValueError("百分比阈值用 0~1 表示（例如 0.02 就是允许掉 2%）")

    def acceptable(self, baseline: float, metric: float) -> bool:
        """Whether ``metric`` may keep the outward probe going."""
        gain = metric - baseline
        if self.threshold_mode is ThresholdMode.ABSOLUTE:
            return gain >= -self.threshold_value
        if self.threshold_mode is ThresholdMode.PERCENT:
            if baseline <= 0:
                return gain >= 0
            return gain / abs(baseline) >= -self.threshold_value
        return gain >= 0

    def describe(self) -> str:
        if self.threshold_mode is ThresholdMode.ABSOLUTE:
            mode = f"允许掉幅 ≤ {fmt(self.threshold_value)}（绝对伤害）"
        elif self.threshold_mode is ThresholdMode.PERCENT:
            mode = f"允许掉幅 ≤ {self.threshold_value * 100:g}%（百分比）"
        else:
            mode = "不允许掉伤害"
        text = f"步长 {self.step} / 偏移 ±{self.max_offset} / 外探 {self.outer_probes} / {mode}"
        if self.max_battles:
            text += f" / 场次上限 {self.max_battles}"
        return text

    def to_dict(self) -> dict:
        return {
            "step": self.step,
            "max_offset": self.max_offset,
            "outer_probes": self.outer_probes,
            "threshold_mode": self.threshold_mode.value,
            "threshold_value": self.threshold_value,
            "frame_min": self.frame_min,
            "frame_max": self.frame_max,
            "peak_tolerance": self.peak_tolerance,
            "memoize": self.memoize,
            "max_battles": self.max_battles,
            "chunk": self.chunk,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SearchSettings":
        fields = {name: data[name] for name in (
            "step", "max_offset", "outer_probes", "threshold_mode", "threshold_value", "frame_min",
            "frame_max", "peak_tolerance", "memoize", "max_battles", "chunk",
        ) if name in data}
        if "threshold_mode" in fields:
            fields["threshold_mode"] = ThresholdMode.parse(fields["threshold_mode"])
        try:
            return cls(**fields)
        except TypeError as exc:
            raise ValueError(f"搜索参数格式错误：{exc}") from exc


UbTable = "dict[int, tuple[tuple[float, ...], ...]]"


def _actual_ub(
    ub_of: Callable[[int], "tuple[tuple[float, ...], ...] | None"],
    ub_row: int,
    ub_index: int,
    frame: int,
) -> float | None:
    """这一帧上目标角色实际放出的那一刀 UB 的帧号（拿不到就是 None）。"""
    if ub_row < 0:
        return None
    rows = ub_of(frame)
    if not rows or ub_row >= len(rows):
        return None
    row = rows[ub_row]
    if ub_index >= len(row):
        return None
    return float(row[ub_index])


def _fired(
    ub_of: Callable[[int], "tuple[tuple[float, ...], ...] | None"],
    ub_row: int,
    ub_index: int,
    frame: int,
) -> bool | None:
    """这一帧上目标 UB 到底放出来了没有（None = 不知道，别据此下结论）。"""
    rows = ub_of(frame)
    if ub_row < 0 or not rows or ub_row >= len(rows):
        return None
    actual = _actual_ub(ub_of, ub_row, ub_index, frame)
    return actual is not None and abs(actual - frame) <= UB_FRAME_TOLERANCE


@dataclass
class MetricBook:
    """Frame → damage cache, filled in batches through ``fetch``.

    ``fetch(frames, tag)`` must return one metric per requested frame and raise on
    any failure (never a fake zero).
    """

    fetch: Callable[[list[int], str], list[float]]
    settings: SearchSettings
    log: Callable[[str], None] = print
    cancelled: Callable[[], bool] = lambda: False
    cache: dict[int, float] = field(default_factory=dict)
    battles: int = 0
    # 帧 → 每个玩家实际放 UB 的帧（由 fetch 顺手填，缺的帧就是不知道）
    ub: dict[int, tuple[tuple[float, ...], ...]] = field(default_factory=dict)

    def get(self, frame: int) -> float:
        return self.cache[frame]

    def ub_of(self, frame: int) -> tuple[tuple[float, ...], ...] | None:
        return self.ub.get(frame)

    def prime(self, frame: int, metric: float) -> None:
        self.cache[frame] = float(metric)

    def block(self, frames: list[int], tag: str) -> None:
        if self.cancelled():
            raise SearchCancelled("已取消")
        missing: list[int] = []
        for frame in frames:
            if not self.settings.frame_min <= frame <= self.settings.frame_max:
                raise SearchAbort(f"帧 {frame} 超出范围 {self.settings.frame_min}~{self.settings.frame_max}")
            if self.settings.memoize and frame in self.cache:
                continue
            if frame not in missing:
                missing.append(frame)
        if not missing:
            return
        if self.settings.max_battles and self.battles + len(missing) > self.settings.max_battles:
            raise SearchAbort(f"达到场次上限 {self.settings.max_battles}，搜索中止（请缩小偏移/步长）")
        metrics = self.fetch(missing, tag)
        if len(metrics) != len(missing):
            raise SearchAbort(f"采样返回数量与请求不符（请求 {len(missing)}，返回 {len(metrics)}）")
        self.battles += len(missing)
        for frame, metric in zip(missing, metrics):
            self.cache[frame] = float(metric)


@dataclass
class SearchResult:
    """Outcome of one item's window search."""

    ok: bool = True
    cancelled: bool = False
    message: str = ""
    baseline_frame: int = 0
    baseline_metric: float = 0.0
    peak_frame: int = 0
    peak_metric: float = 0.0
    threshold_start: int = -1
    threshold_end: int = -1
    battles: int = 0
    elapsed_ms: int = 0
    visited: dict[int, float] = field(default_factory=dict)

    @property
    def gain(self) -> float:
        return self.peak_metric - self.baseline_metric

    @property
    def same_as_baseline(self) -> bool:
        return self.peak_frame == self.baseline_frame and abs(self.peak_metric - self.baseline_metric) < 1e-9

    def describe(self) -> str:
        if not self.ok:
            return f"失败（{self.message}）"
        return (
            f"基线 {self.baseline_frame}→{fmt(self.baseline_metric)}"
            f" | 最优 {self.peak_frame}→{fmt(self.peak_metric)}"
            f" | 涨幅 {signed(self.gain)}"
            f" | 阈值区间 {self.threshold_start}~{self.threshold_end}"
            f" | 场次 {self.battles}"
        )


def run(
    settings: SearchSettings,
    baseline_frame: int,
    baseline_metric: float,
    book: MetricBook,
    *,
    ub_row: int = -1,
    ub_index: int = 0,
    log: Callable[[str], None] = print,
    cancelled: Callable[[], bool] = lambda: False,
) -> SearchResult:
    """Run the eight stages for one item and return the chosen frame."""
    baseline_metric = float(baseline_metric)
    result = SearchResult(
        baseline_frame=int(baseline_frame),
        baseline_metric=baseline_metric,
        peak_frame=int(baseline_frame),
        peak_metric=baseline_metric,
    )
    result.visited[int(baseline_frame)] = baseline_metric
    book.prime(int(baseline_frame), baseline_metric)
    log(f"① 基线：帧 {baseline_frame} 伤害={fmt(baseline_metric)}（{settings.describe()}）")
    started = time.monotonic()

    def walk(frames: list[int], tag: str) -> Iterator[tuple[int, float]]:
        ordered = [frame for frame in frames if settings.frame_min <= frame <= settings.frame_max]
        size = settings.chunk
        if tag in ("fillL", "fillR") and settings.memoize:
            # 粗探已经测过的低点一定会结束补齐，不必预发越过它的场次。
            cutoff = max(result.visited.values()) - settings.peak_tolerance
            for index, frame in enumerate(ordered):
                if frame in book.cache and book.get(frame) < cutoff:
                    ordered = ordered[:index + 1]
                    break
            # 左侧刚出现放不出 UB 的帧时，通常第一步就能确定边界。
            if tag == "fillL" and any(_fired(book.ub_of, ub_row, ub_index, f) is False
                                      for f in ordered[:settings.step]):
                size = 1
        downward = ub_row >= 0 and tag in ("down", "downouter", "fillL")
        misses = 0
        limit = min(UB_MISS_LIMIT, settings.outer_probes)
        start = 0
        while start < len(ordered):
            room = min(size, limit + 1 - misses) if downward else size
            block = ordered[start:start + room]
            start += len(block)
            size = min(settings.chunk, size * 2)
            book.block(block, tag)
            for frame in block:
                metric = book.get(frame)
                result.visited[frame] = metric
                yield frame, metric
                if downward:
                    misses = misses + 1 if _fired(book.ub_of, ub_row, ub_index, frame) is False else 0
                    if misses > limit:
                        return

    try:
        peak_frame, peak_metric = _probe(
            settings, int(baseline_frame), baseline_metric, walk, log, cancelled,
            ub_row=ub_row, ub_index=ub_index, ub_of=book.ub_of,
        )
        result.peak_frame, result.peak_metric = peak_frame, peak_metric
        _finalise(settings, result, log, fired=lambda frame: _fired(book.ub_of, ub_row, ub_index, frame))
    except SearchCancelled:
        result.cancelled = True
        result.message = "已取消"
        result.peak_frame, result.peak_metric = int(baseline_frame), baseline_metric
    except SearchAbort as exc:
        result.ok = False
        result.message = str(exc)
        result.peak_frame, result.peak_metric = int(baseline_frame), baseline_metric
    result.battles = book.battles
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    if result.ok and not result.cancelled:
        log(f"完成：{result.battles} 场 / {result.elapsed_ms / 1000:.1f}s")
    return result


def _probe(
    settings: SearchSettings,
    baseline_frame: int,
    baseline_metric: float,
    walk: Callable[[list[int], str], Iterator[tuple[int, float]]],
    log: Callable[[str], None],
    cancelled: Callable[[], bool],
    *,
    ub_row: int = -1,
    ub_index: int = 0,
    ub_of: Callable[[int], "tuple[tuple[float, ...], ...] | None"] = lambda frame: None,
) -> tuple[int, float]:
    """Stages ①–⑦: coarse probing around the baseline, then fill the peak."""
    peak_frame, peak_metric = baseline_frame, baseline_metric
    win_up = min(baseline_frame + settings.max_offset, settings.frame_max)
    win_down = max(baseline_frame - settings.max_offset, settings.frame_min)
    step = settings.step

    # ②③ upward
    safety = 0
    while safety < 400:
        safety += 1
        if cancelled():
            raise SearchCancelled("已取消")
        hit, threshold_frame = False, -1
        for frame, metric in walk(list(range(peak_frame + step, win_up + 1, step)), "up"):
            if metric > peak_metric:
                peak_metric, peak_frame = metric, frame
            if not settings.acceptable(baseline_metric, metric):
                hit, threshold_frame = True, frame
                break
        if not hit:
            log(f"② 向上探到窗口上界 {win_up}，未超阈值，转④")
            break
        log(f"③ 帧 {threshold_frame} 超阈值，向上外探最多 {settings.outer_probes} 次")
        found = False
        outer = [threshold_frame + step * k for k in range(1, settings.outer_probes + 1)]
        for frame, metric in walk(outer, "upouter"):
            if metric > peak_metric:
                peak_metric, peak_frame, found = metric, frame, True
                log(f"③ 外探发现更高值：{frame}→{fmt(metric)}，换峰值回到②")
                break
        if not found:
            break

    # ④⑤ downward
    safety = 0
    lower_bound = max(settings.frame_min, baseline_frame - settings.max_offset)
    miss_limit = min(UB_MISS_LIMIT, settings.outer_probes)
    misses = 0

    def cannot_fire(frame: int) -> bool:
        """向下探时发现这一刀放不出来，再往前就没有意义了。"""
        nonlocal misses
        state = _fired(ub_of, ub_row, ub_index, frame)
        if state is None or state:
            misses = 0
            return False
        actual = _actual_ub(ub_of, ub_row, ub_index, frame)
        misses += 1
        if misses > miss_limit:
            where = f"实际 UB 在 {fmt(actual)}" if actual is not None else "这一刀根本没放出来"
            log(f"⛔ 帧 {frame} 放不出目标 UB（{where}）：再往前没有意义，向下方向到此结束")
            return True
        return False

    stop_down = False
    blocked_frame = None
    while safety < 400:
        safety += 1
        if cancelled():
            raise SearchCancelled("已取消")
        hit, threshold_frame = False, -1
        for frame, metric in walk(list(range(peak_frame - step, win_down - 1, -step)), "down"):
            if metric > peak_metric:
                peak_metric, peak_frame = metric, frame
            if cannot_fire(frame):
                blocked_frame = frame
                stop_down = True
                break
            if not settings.acceptable(baseline_metric, metric):
                hit, threshold_frame = True, frame
                break
        if stop_down:
            break
        if not hit:
            log(f"④ 向下探到窗口下界 {win_down}，未超阈值，转⑥")
            break
        log(f"⑤ 帧 {threshold_frame} 超阈值，向下外探最多 {settings.outer_probes} 次")
        found = False
        outer = [
            threshold_frame - step * k
            for k in range(1, settings.outer_probes + 1)
            if threshold_frame - step * k >= lower_bound
        ]
        for frame, metric in walk(outer, "downouter"):
            if metric > peak_metric:
                peak_metric, peak_frame, found = metric, frame, True
                log(f"⑤ 外探发现更高值：{frame}→{fmt(metric)}，换峰值回到④")
                break
            if cannot_fire(frame):
                blocked_frame = frame
                stop_down = True
                break
        if stop_down or not found:
            break

    # ⑥⑦ fill both sides of the peak frame by frame
    fill_cap = settings.max_offset + (settings.outer_probes + 2) * max(1, step)
    log(f"⑥ 峰值 {peak_frame} 左侧逐帧补齐")
    left = max(settings.frame_min, peak_frame - fill_cap, blocked_frame or settings.frame_min)
    for frame, metric in walk(list(range(peak_frame - 1, left - 1, -1)), "fillL"):
        if metric > peak_metric:
            peak_metric, peak_frame = metric, frame
        if metric + settings.peak_tolerance < peak_metric:
            break
        if cannot_fire(frame):
            break
    log(f"⑦ 峰值 {peak_frame} 右侧逐帧补齐")
    right = min(settings.frame_max, peak_frame + fill_cap)
    for frame, metric in walk(list(range(peak_frame + 1, right + 1)), "fillR"):
        if metric > peak_metric:
            peak_metric, peak_frame = metric, frame
        if metric + settings.peak_tolerance < peak_metric:
            break
    return peak_frame, peak_metric


def _finalise(
    settings: SearchSettings,
    result: SearchResult,
    log: Callable[[str], None],
    *,
    fired: Callable[[int], bool | None] = lambda frame: None,
) -> None:
    """Stage ⑧: among the visited frames take the best metric, then the earliest frame.

    放不出目标 UB 的帧不算候选（写进去这一刀也不会按出来），除非全场只有这种帧。
    """
    visited = result.visited
    if not visited:
        return
    eligible = [frame for frame in visited if fired(frame) is not False] or list(visited)
    best_metric = max(visited[frame] for frame in eligible)
    # 容差内可取更早帧，但不能给它记上另一帧的伤害，也不能因此退步。
    floor = min(result.baseline_metric, best_metric)
    result.peak_frame = min(
        frame for frame in eligible
        if visited[frame] >= floor and best_metric - visited[frame] <= settings.peak_tolerance
    )
    result.peak_metric = visited[result.peak_frame]
    within = [
        frame for frame, metric in visited.items()
        if settings.acceptable(result.baseline_metric, metric)
    ]
    result.threshold_start = min(within) if within else -1
    result.threshold_end = max(within) if within else -1
    log(
        f"⑧ 择优：最高伤害={fmt(best_metric)}，同伤害里取最早帧 {result.peak_frame}；"
        f"阈值区间 {result.threshold_start}~{result.threshold_end}"
    )
