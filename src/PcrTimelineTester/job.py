"""Drive one tuning run: baseline → per-item window search → two gates → write-back.

The two gates are what protects a long run from drifting backwards:

* 闸一 退步保护 — an item whose best metric is below the best already written is
  not accepted (counted as failed, frame stays as it was);
* 闸二 跳过 — same metric *and* the search chose the current frame ⇒ nothing to do.

The search only accumulates in memory; the axis is written once at the end, so an
interrupted run never leaves a half-tuned timeline behind.
"""

from __future__ import annotations

from .recovery import verifying

from dataclasses import dataclass, field
import threading
import time
from typing import Callable

from .client import DEFAULT_CALC_STATS, DEFAULT_ENDPOINT, BattleOutcome, SimulatorClient, result_name
from .effects import paired_alternates, proposals
from .frames import FrameTarget, base_frame, build_targets, text
from .savefile import FrameAxis, SaveFile
from .search import UB_MISS_LIMIT, MetricBook, SearchResult, SearchSettings, fmt, run as search_run, signed

DECISION_TEXT = {
    "write": "写回",
    "skip": "跳过（本就最优）",
    "fail": "失败",
    "cancel": "中断",
    "flat": "本轮无提升",
    "done": "已完成（续跑）",
}


@dataclass(frozen=True, slots=True)
class JobSettings:
    """Everything that is not a search-window parameter."""

    endpoint: str = DEFAULT_ENDPOINT
    battle_time: int = 90
    seed: int = 666
    samples: int = 1
    batch_size: int = 16
    timeout: float = 180.0
    calc_stats: int = DEFAULT_CALC_STATS
    include_boss: bool = False
    items: frozenset[tuple[int, int]] | None = None
    max_items: int | None = None
    verify: bool = True
    passes: int = 1

    def seeds(self) -> tuple[int, ...]:
        seed = int(self.seed)
        count = max(1, int(self.samples))
        return (seed,) + tuple((abs(seed) + index) % (2**31 - 1) for index in range(1, count))

    def to_dict(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "battle_time": self.battle_time,
            "seed": self.seed,
            "samples": self.samples,
            "batch_size": self.batch_size,
            "timeout": self.timeout,
            "calc_stats": self.calc_stats,
            "include_boss": self.include_boss,
            "items": sorted([list(pair) for pair in self.items]) if self.items else None,
            "max_items": self.max_items,
            "verify": self.verify,
            "passes": self.passes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "JobSettings":
        known = {name: data[name] for name in (
            "endpoint", "battle_time", "seed", "samples", "batch_size", "timeout", "calc_stats",
            "include_boss", "max_items", "verify", "passes",
        ) if name in data}
        items = data.get("items")
        if items:
            known["items"] = frozenset((int(pair[0]), int(pair[1])) for pair in items)
        try:
            return cls(**known)
        except TypeError as exc:
            raise ValueError(f"运行参数格式错误：{exc}") from exc


@dataclass
class Accepted:
    row: int
    index: int
    label: str
    old_raw: float
    new_raw: float
    old_metric: float
    new_metric: float
    pass_no: int = 1


class TuneJob:
    """One tuning run over every item of a timeline."""

    def __init__(
        self,
        save: SaveFile,
        settings: JobSettings,
        search_settings: SearchSettings,
        *,
        log: Callable[[str], None] = print,
        client: SimulatorClient | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.save = save
        self.settings = settings
        self.search_settings = search_settings
        self.log = log
        self.frames: FrameAxis = save.frames_copy()
        self.targets: list[FrameTarget] = build_targets(
            save.frames, save.unit_ids,
            names=save.names, boss_name=save.boss_name,
            include_boss=settings.include_boss, items=settings.items, max_items=settings.max_items,
        )
        self._client = client
        self._cancel = cancel or threading.Event()
        self.accepted: list[Accepted] = []
        self.done: set[tuple[int, str]] = set()
        self.passes = max(1, int(settings.passes))
        self.pass_index = 0
        self.effect_done: set[str] = set()
        self.alternate_frames: dict[str, list[int]] = {}
        self.effect_spent: dict[str, int] = {}
        self.effect_progress: dict = {}
        self._probe_books: dict[str, MetricBook] = {}
        self.baseline: BattleOutcome | None = None
        self.baseline_metric = 0.0
        self.current_metric = 0.0
        self.last_best_metric = 0.0
        self.final: BattleOutcome | None = None
        self.written = 0
        self.skipped = 0
        self.failed = 0
        self.cancelled = False
        self.items: list[dict] = []
        self._resumed = False
        self._started = 0.0

    # ----------------------------------------------------------------- wiring

    @property
    def client(self) -> SimulatorClient:
        if self._client is None:
            self._client = SimulatorClient(
                context=self.save.request_context(
                    battle_time=self.settings.battle_time, battle_seed=self.settings.seed,
                ),
                endpoint=self.settings.endpoint,
                seeds=self.settings.seeds(),
                timeout=self.settings.timeout,
                batch_size=self.settings.batch_size,
                calc_stats=self.settings.calc_stats,
                log=self.log,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    @property
    def battles(self) -> int:
        """Battles actually simulated so far (0 before the client is created)."""
        return self._client.battles if self._client is not None else 0

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled_requested(self) -> bool:
        return self._cancel.is_set()

    # ------------------------------------------------------------------ frames

    def frames_copy(self) -> FrameAxis:
        """Current axis (already includes every accepted write of this run)."""
        return [list(row) for row in self.frames]

    def frame_of(self, target: FrameTarget) -> int:
        return base_frame(self.frames[target.row][target.index])

    def table_with(self, target: FrameTarget, frame: int) -> FrameAxis:
        table = [list(row) for row in self.frames]
        table[target.row][target.index] = target.to_raw(frame)
        return table

    def _fetch(
        self,
        target: FrameTarget,
        frames: list[int],
        tag: str,
        ub: dict[int, "tuple[tuple[float, ...], ...]"] | None = None,
    ) -> list[float]:
        outcomes = self.client.evaluate([self.table_with(target, frame) for frame in frames])
        if ub is not None:
            for frame, outcome in zip(frames, outcomes):
                ub[frame] = outcome.ub_frames
        return [outcome.metric for outcome in outcomes]

    # -------------------------------------------------------------------- run

    def run(self) -> dict:
        self._started = time.monotonic()
        self._establish_baseline()
        self.log(
            f"=== 共 {len(self.targets)} 项待测"
            f"{f'，循环 {self.passes} 轮（每轮都用上一轮的最优帧当起点）' if self.passes > 1 else ''}；"
            f"{self.search_settings.describe()}；"
            f"接口 {self.settings.endpoint}（战斗 {self.settings.battle_time} 秒）==="
        )
        total = self.passes * len(self.targets)
        position = 0
        for pass_no in range(1, self.passes + 1):
            if self._cancel.is_set():
                self.cancelled = True
                break
            self.pass_index = pass_no
            if self.passes > 1:
                self.log(
                    f"=== 第 {pass_no}/{self.passes} 轮：当前期望伤害 {fmt(self.current_metric)}，"
                    f"按这份最优重新调 {len(self.targets)} 项 ==="
                )
            before = (self.written, self.skipped, self.failed)
            for target in self.targets:
                if self._cancel.is_set():
                    self.cancelled = True
                    break
                position += 1
                if (pass_no, target.key) in self.done:
                    self.items.append(self._done_entry(target, pass_no))
                    continue
                entry = self._run_item(position, target, pass_no, total)
                self.items.append(entry)
                if entry["decision"] == "cancel":
                    self.cancelled = True
                    break
            if self.cancelled:
                break
            if self.passes > 1:
                self.log(
                    f"=== 第 {pass_no} 轮结束：写回 {self.written - before[0]} / "
                    f"跳过 {self.skipped - before[1]} / 失败 {self.failed - before[2]}，"
                    f"当前期望伤害 {fmt(self.current_metric)} ==="
                )
        if not self.cancelled:
            self._refine_effects()
        if not self.cancelled:
            with verifying(self.frames_copy(), self.current_metric):
                self._verify()
            self.cancelled = self._cancel.is_set()
        elapsed = time.monotonic() - self._started
        self.log(
            f"=== {'已中断：未写回任何最优解（存档保持原样）' if self.cancelled else '全部完成'}："
            f"写回 {self.written} 项 / 跳过 {self.skipped} 项 / 失败 {self.failed} 项 / "
            f"{self.client.battles if self._client else 0} 场 / 用时 {elapsed:.1f}s ==="
        )
        return self.summary(elapsed)

    def _refine_effects(self) -> None:
        settings = self.search_settings
        targets = [t for t in self.targets if t.row < len(self.save.unit_ids) and t.key not in self.effect_done]
        events = None
        if targets:
            self.log("开始按跳伤、TP 与增减益生效帧调刀，测试队友联动和同帧顺序。")
        while targets:
            if self._cancel.is_set():
                self.cancelled = True
                return
            targets.sort(key=lambda t: (self.frame_of(t), t.row, t.index))
            target = targets.pop(0)
            if not self.effect_progress and settings.max_battles and self.effect_spent.get(target.key, 0) >= settings.max_battles:
                self.effect_done.add(target.key)
                continue
            if events is None:
                trace = getattr(self.client, "effect_frames", None)
                events = trace(self.frames) if trace else [set() for _ in self.save.unit_ids]
            partner = next((t for t in targets if t.row != target.row
                            and self.frame_of(t) - self.frame_of(target) <= max(60, 2 * settings.max_offset)), None)
            changes = list(proposals(self.frames, target, partner, events, settings))
            # 前面单改接近峰值的帧，也可能改变后面另一位队友的回能或技能循环。
            later = [t for t in targets if t != partner and t.row != target.row
                     and self.frame_of(t) - self.frame_of(target) <= max(600, 2 * settings.max_offset)
                     and self.alternate_frames.get(t.key)]
            for other in dict.fromkeys(later[:1] + later[-1:]):
                changes.extend(paired_alternates(self.frames, target, other, self.alternate_frames, settings))
            changes = list(dict.fromkeys(changes))
            began = time.monotonic()
            before_battles = self.battles
            best = self._probe_effects(changes)
            if self.cancelled:
                return
            if best is not None:
                edits, metric = best
                old_metric = self.current_metric
                details = []
                for t, value in edits:
                    old = self.frames[t.row][t.index]
                    details.append(f"{t.label} {text(old)}→{text(value)}")
                    self.accepted.append(Accepted(t.row, t.index, t.label, old, value,
                                                  self.current_metric, metric, self.passes))
                    self.frames[t.row][t.index] = value
                    self.current_metric = metric  # 同一笔联动收益只记一次
                self.items.append({
                    "label": "、".join(t.label for t, _ in edits), "pass": self.passes,
                    "old_frame": "、".join(text(a.old_raw) for a in self.accepted[-len(edits):]),
                    "new_frame": "、".join(text(value) for _, value in edits),
                    "decision": "write", "decision_text": "效果帧联调",
                    "baseline_metric": old_metric, "peak_metric": metric,
                    "battles": self.battles - before_battles,
                    "elapsed_ms": round((time.monotonic() - began) * 1000),
                })
                self.last_best_metric = metric
                self.written += len(edits)
                self.log(f"效果帧联调：{'、'.join(details)}，整刀 +{fmt(metric - old_metric)}")
                events = None
            self.effect_progress.clear()
            self.effect_done.add(target.key)

    def _probe_effects(self, changes):
        """按批测试同一份轴上的候选，停止后从下一组继续。"""
        settings = self.search_settings
        progress = self.effect_progress
        misses = progress.setdefault("misses", {})
        index = progress.get("next", 0)
        best = progress.get("best")
        metric = progress.get("metric", max(self.current_metric, self.last_best_metric))
        miss_limit = min(UB_MISS_LIMIT, settings.outer_probes)
        while index < len(changes):
            if self._cancel.is_set():
                self.cancelled = True
                return None
            indices, axes = [], []
            downward = {}
            while index < len(changes) and len(axes) < settings.chunk:
                edits = changes[index]
                lower = [t.key for t, value in edits if base_frame(value) < self.frame_of(t)]
                if any(misses.get(key, 0) > miss_limit for key in lower) or any(
                    settings.max_battles and self.effect_spent.get(t.key, 0) >= settings.max_battles
                    for t, _ in edits
                ):
                    index += 1
                    continue
                if any(misses.get(key, 0) + downward.get(key, 0) > miss_limit for key in lower):
                    break  # 等本批实际 UB 帧回来，再决定能否继续向下探
                axis = self.frames_copy()
                for t, value in edits:
                    axis[t.row][t.index] = value
                    self.effect_spent[t.key] = self.effect_spent.get(t.key, 0) + 1
                for key in lower:
                    downward[key] = downward.get(key, 0) + 1
                indices.append(index)
                axes.append(axis)
                index += 1
            for i, outcome in zip(indices, self.client.evaluate(axes) if axes else (), strict=True):
                fired = True
                for t, value in changes[i]:
                    if t.row >= len(outcome.ub_frames):
                        continue
                    row = outcome.ub_frames[t.row]
                    hit = t.index < len(row) and base_frame(row[t.index]) == base_frame(value)
                    if base_frame(value) < self.frame_of(t):
                        misses[t.key] = 0 if hit else misses.get(t.key, 0) + 1
                        fired = fired and hit
                if fired and outcome.metric > metric:
                    best, metric = i, outcome.metric
            progress.update(next=index, best=best, metric=metric)
        return (changes[best], metric) if best is not None else None

    def _establish_baseline(self) -> None:
        if self._resumed and self.baseline_metric:
            self.log(f"=== 续跑：沿用上次基线伤害 {fmt(self.baseline_metric)}，当前 {fmt(self.current_metric)} ===")
            return
        outcome = self.client.evaluate([self.frames_copy()])[0]
        self.baseline = outcome
        self.baseline_metric = outcome.metric
        self.current_metric = outcome.metric
        self.last_best_metric = outcome.metric
        self.log(
            f"=== 基线：期望伤害 {fmt(outcome.metric)}（实战伤害 {outcome.damage:,}）× 战斗帧 {outcome.battle_frames} / "
            f"结果 {result_name(outcome.result)} / 种子 {outcome.seed} ==="
        )
        if outcome.metric <= 0:
            self.log("[warn] 基线伤害 ≤ 0，先确认这条轴在摸轴器里本身能打出伤害")

    def _verify(self) -> None:
        if not self.settings.verify:
            return
        outcome = self.client.evaluate([self.frames_copy()])[0]
        self.final = outcome
        gained = outcome.metric - self.baseline_metric
        self.log(
            f"=== 复核：调帧后期望伤害 {fmt(outcome.metric)}（实战伤害 {outcome.damage:,}）× 战斗帧 {outcome.battle_frames} / "
            f"结果 {result_name(outcome.result)}，相对基线 {signed(gained)} ==="
        )

    def _run_item(self, position: int, target: FrameTarget, pass_no: int, total: int) -> dict:
        current_frame = self.frame_of(target)
        option_base = self.current_metric
        round_text = f" 第{pass_no}轮" if self.passes > 1 else ""
        self.log(
            f"--- [{position}/{total}]{round_text} {target.label} @ {text(self.frames[target.row][target.index])}"
            f"（当前伤害 {fmt(option_base)}）---"
        )
        book = MetricBook(
            fetch=lambda frames, tag: [],
            settings=self.search_settings,
            log=self.log,
            cancelled=self._cancel.is_set,
        )
        if self.passes > 1 and self.search_settings.memoize:
            previous = self._probe_books.get(target.key)
            if previous is not None:
                book.cache, book.ub = previous.cache, previous.ub
            self._probe_books[target.key] = book
        # fetch 顺手把每个帧上「实际放出的 UB 帧」记进 book.ub，向下探时用来判断放不放得出来
        book.fetch = lambda frames, tag: self._fetch(target, frames, tag, book.ub)
        result = search_run(
            self.search_settings, current_frame, option_base, book,
            ub_row=target.row, ub_index=target.index,
            log=lambda line: self.log(f"    {line}"),
            cancelled=self._cancel.is_set,
        )
        entry = self._entry(target, current_frame, result, pass_no)

        if result.cancelled:
            entry.update(decision="cancel", decision_text=DECISION_TEXT["cancel"])
            self.log(f"[warn] {target.label} 已取消，本轮到此为止（未写回任何改动）")
            return entry
        self.done.add((pass_no, target.key))
        if pass_no == self.passes:
            self.effect_spent[target.key] = result.battles
            # 在峰值两侧各保留一个实测备选。伤害相同时选较远的帧，探索不同的配合。
            alternatives = []
            for side in (-1, 1):
                candidates = [f for f in result.visited if (f - result.peak_frame) * side > 0]
                if candidates:
                    alternatives.append(max(candidates, key=lambda f: (result.visited[f], abs(f - result.peak_frame))))
            self.alternate_frames[target.key] = alternatives
            if not result.ok or result.peak_metric < self.last_best_metric:
                self.effect_done.add(target.key)
        if not result.ok:
            self.failed += 1
            entry.update(decision="fail", decision_text=DECISION_TEXT["fail"])
            self.log(f"[error] {target.label} 搜索失败：{result.message}（本项保持原帧 {target.raw_text}）")
            return entry
        if result.peak_metric < self.last_best_metric:
            self.failed += 1
            entry.update(decision="fail", decision_text="退步保护")
            self.log(
                f"[error] 闸一退步保护：{target.label} 本项最高 {fmt(result.peak_metric)} < 上一项最优 "
                f"{fmt(self.last_best_metric)}，不采纳，保持原帧 {target.raw_text}"
            )
            return entry
        chosen = min(max(result.peak_frame, self.search_settings.frame_min), self.search_settings.frame_max)
        if abs(result.peak_metric - option_base) < 1e-9 and chosen == current_frame:
            self.skipped += 1
            entry.update(decision="skip", decision_text=DECISION_TEXT["skip"], new_frame=target.raw_text)
            self.log(f"[skip] {target.label} 伤害相同且帧未变（{target.raw_text}），无需改动")
            return entry

        if pass_no > 1 and result.peak_metric <= option_base + 1e-9:
            # 循环模式只为「继续提升」服务：这一轮没涨伤害就不动帧，
            # 否则平坦地形会一轮轮把帧蹭到更早的位置。
            self.skipped += 1
            entry.update(
                decision="skip", decision_text=DECISION_TEXT["flat"], new_frame=target.raw_text,
            )
            self.log(
                f"[skip] {target.label} 第{pass_no}轮没有提升（伤害仍是 {fmt(option_base)}），"
                f"保持当前帧 {target.raw_text}"
            )
            return entry

        old_raw = float(self.frames[target.row][target.index])
        new_raw = target.to_raw(chosen)
        self.frames[target.row][target.index] = new_raw
        if old_raw != new_raw and self._probe_books:
            # 其他刀的探针依赖整条轴，这一刀自己的候选表仍可复用。
            self._probe_books = {target.key: book}
        self.accepted.append(Accepted(
            row=target.row, index=target.index, label=target.label,
            old_raw=old_raw, new_raw=new_raw, old_metric=option_base, new_metric=result.peak_metric,
            pass_no=pass_no,
        ))
        self.written += 1
        self.last_best_metric = max(self.last_best_metric, result.peak_metric)
        if abs(result.peak_metric - option_base) >= 1e-9:
            self.current_metric = result.peak_metric
        entry.update(
            decision="write", decision_text=DECISION_TEXT["write"],
            old_frame=text(old_raw), new_frame=text(new_raw),
        )
        self.log(
            f"[plan] {target.label}：{text(old_raw)} → {text(new_raw)}"
            f"（伤害 {fmt(option_base)} → {fmt(result.peak_metric)}，{signed(result.peak_metric - option_base)}）已记入待写回"
        )
        return entry

    # ------------------------------------------------------------------ report

    def _entry(self, target: FrameTarget, current_frame: int, result: SearchResult, pass_no: int = 1) -> dict:
        return {
            "key": target.key,
            "label": target.label,
            "pass": pass_no,
            "row": target.row,
            "index": target.index,
            "old_frame": target.raw_text,
            "new_frame": None,
            "decision": "fail",
            "decision_text": DECISION_TEXT["fail"],
            "baseline_frame": result.baseline_frame,
            "baseline_metric": result.baseline_metric,
            "peak_frame": result.peak_frame,
            "peak_metric": result.peak_metric,
            "threshold_start": result.threshold_start,
            "threshold_end": result.threshold_end,
            "battles": result.battles,
            "elapsed_ms": result.elapsed_ms,
            "gain": result.gain,
            "ok": result.ok,
            "cancelled": result.cancelled,
            "message": result.message,
            "current_frame": current_frame,
        }

    def _done_entry(self, target: FrameTarget, pass_no: int = 1) -> dict:
        return {
            "key": target.key,
            "label": target.label,
            "pass": pass_no,
            "row": target.row,
            "index": target.index,
            "old_frame": target.raw_text,
            "new_frame": target.raw_text,
            "decision": "done",
            "decision_text": DECISION_TEXT["done"],
            "battles": 0,
            "elapsed_ms": 0,
            "message": "",
        }

    def summary(self, elapsed: float) -> dict:
        def battle(outcome: BattleOutcome | None) -> dict | None:
            if outcome is None:
                return None
            return {
                "damage": outcome.damage,
                "metric": outcome.metric,
                "battle_frames": outcome.battle_frames,
                "result": outcome.result,
                "result_name": result_name(outcome.result),
                "seed": outcome.seed,
            }

        return {
            "excel": str(self.save.path),
            "run_dir": None,
            "endpoint": self.settings.endpoint,
            "battle_time": self.settings.battle_time,
            "seed": self.settings.seed,
            "samples": self.settings.samples,
            "search": self.search_settings.to_dict(),
            "search_text": self.search_settings.describe(),
            "targets": len(self.targets),
            "passes": self.passes,
            "pass_index": self.pass_index,
            "baseline": battle(self.baseline) or {"metric": self.baseline_metric, "damage": round(self.baseline_metric)},
            "final": battle(self.final),
            "written": self.written,
            "skipped": self.skipped,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "battles": self._client.battles if self._client else 0,
            "elapsed_s": round(elapsed, 1),
            "result_workbook": None,
            "accepted": [
                {
                    "row": item.row, "index": item.index, "label": item.label,
                    "old_raw": item.old_raw, "new_raw": item.new_raw,
                    "old_metric": item.old_metric, "new_metric": item.new_metric,
                    "pass": item.pass_no,
                }
                for item in self.accepted
            ],
        }

    # ------------------------------------------------------------------- state

    def state(self) -> dict:
        return {
            "excel": str(self.save.path),
            "settings": self.settings.to_dict(),
            "search": self.search_settings.to_dict(),
            "baseline_metric": self.baseline_metric,
            "current_metric": self.current_metric,
            "last_best_metric": self.last_best_metric,
            "written": self.written,
            "skipped": self.skipped,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "pass_index": self.pass_index,
            "done": sorted([list(pair) for pair in self.done]),
            "effect_done": sorted(self.effect_done),
            "effect_spent": dict(self.effect_spent),
            "alternate_frames": self.alternate_frames,
            "effect_progress": dict(self.effect_progress),
            "accepted": [
                {
                    "row": item.row, "index": item.index, "label": item.label,
                    "old_raw": item.old_raw, "new_raw": item.new_raw,
                    "old_metric": item.old_metric, "new_metric": item.new_metric,
                    "pass": item.pass_no,
                }
                for item in self.accepted
            ],
        }

    def apply_state(self, data: dict) -> None:
        """Restore progress from a checkpoint (``resume``)."""
        for item in data.get("accepted") or []:
            try:
                row, index = int(item["row"]), int(item["index"])
                new_raw = float(item["new_raw"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"续跑状态里的写回记录无效：{exc}") from exc
            if not (0 <= row < len(self.frames) and 0 <= index < len(self.frames[row])):
                raise ValueError(f"续跑状态里的写回记录超出轴范围：{row}:{index}")
            self.frames[row][index] = new_raw
            self.accepted.append(Accepted(
                row=row, index=index, label=str(item.get("label", f"{row}:{index}")),
                old_raw=float(item.get("old_raw", new_raw)), new_raw=new_raw,
                old_metric=float(item.get("old_metric", 0.0)), new_metric=float(item.get("new_metric", 0.0)),
                pass_no=int(item.get("pass") or 1),
            ))
        done: set[tuple[int, str]] = set()
        for entry in data.get("done") or []:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                done.add((int(entry[0]), str(entry[1])))
            else:  # checkpoint from a single-pass run
                done.add((1, str(entry)))
        self.done = done
        self.effect_done = set(data.get("effect_done", ()))
        self.alternate_frames = dict(data.get("alternate_frames", {}))
        self.effect_spent = dict(data.get("effect_spent", {}))
        self.effect_progress = dict(data.get("effect_progress", {}))
        if self.search_settings.max_battles and "effect_spent" not in data:
            self.effect_done.update(key for n, key in done if n == self.passes)
        self.pass_index = int(data.get("pass_index") or 1)
        self.baseline_metric = float(data.get("baseline_metric") or 0.0)
        self.current_metric = float(data.get("current_metric") or self.baseline_metric)
        self.last_best_metric = float(data.get("last_best_metric") or self.baseline_metric)
        self.written = int(data.get("written") or len(self.accepted))
        self.skipped = int(data.get("skipped") or 0)
        self.failed = int(data.get("failed") or 0)
        self._resumed = True
