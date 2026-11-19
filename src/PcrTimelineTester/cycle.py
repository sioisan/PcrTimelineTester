"""按实际 TP、出手和跳伤事件扩展可变刀数的帧轴。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from itertools import zip_longest
from random import Random
from pathlib import Path
from threading import Event
from tempfile import TemporaryDirectory

from .client import DEFAULT_ENDPOINT, SimulatorClient, TuneError
from .frames import base_frame, text, with_frac
from .job import JobSettings
from .report import CONFIG, REPORT_JSON, REPORT_MD, STATE, write_json
from .savefile import read_save, write_tuned_save
from .search import FRAME_CEILING, FRAME_FLOOR
from .zero import tp_ready_frames


@dataclass(frozen=True)
class CycleSettings:
    endpoint: str = DEFAULT_ENDPOINT
    battle_time: int = 90
    seed: int = 666
    samples: int = 1
    batch_size: int = 16
    timeout: float = 180
    depth: int = 4
    width: int = 4
    candidates: int = 64


Axis = tuple[tuple[float, ...], ...]


def axis_key(rows) -> Axis:
    return tuple(tuple(row) for row in rows)


@dataclass(frozen=True)
class Branch:
    axis: Axis
    metric: float
    counts: tuple[int, ...]
    path: tuple[str, ...]

    @classmethod
    def restore(cls, data):
        return cls(axis_key(data["axis"]), data["metric"], tuple(data["counts"]), tuple(data["path"]))


@dataclass(frozen=True)
class Move:
    kind: str
    role: int
    old: float | None
    new: float | None
    reason: str

    def apply(self, axis, end):
        lane = list(axis[self.role])
        if self.old is not None:
            lane.remove(self.old)
        if self.new is not None:
            frame = base_frame(self.new)
            if not FRAME_FLOOR <= frame <= end or any(base_frame(v) == frame for v in lane):
                return None
            lane.append(self.new)
            lane.sort()
        return axis[:self.role] + (tuple(lane),) + axis[self.role + 1:]

    def describe(self, names):
        old = text(self.old) if self.old is not None else "空"
        new = text(self.new) if self.new is not None else "空"
        return f"{names[self.role]} {self.kind} {old}→{new}（{self.reason}）"


def interleave(groups):
    for row in zip_longest(*groups):
        yield from (item for item in row if item is not None)


class Trace:
    def __init__(self, stats, units):
        self.ready = tp_ready_frames({"Stats": stats})
        self.actual = axis_key(stats.get("UBTime") or ())
        if len(self.actual) < 5 or "TpChangeEvents" not in stats:
            raise TuneError("循环搜索需要算器返回五人实际 UB 帧和 TP 变化事件。")
        slot = {u: i for i, u in enumerate(units)}
        self.charges = []
        self.energy = [0.] * 5
        self.hits = defaultdict(list)
        self.edges = [set() for _ in units]
        for e in stats.get("TpChangeEvents") or ():
            r = slot.get(e.get("TargetUnitId"))
            if r is None:
                continue
            frame = int(e["Frame"])
            cap = e.get("EnergyCap") or 0
            if cap:
                self.energy[r] = e["EnergyAfter"] / cap
            src = slot.get(e.get("SourceUnitId"))
            if src is not None and (src != r or e.get("OverflowDelta", 0) > 0) and e.get("IsIncrease"):
                self.charges.append((frame, src, r, e.get("OverflowDelta", 0)))
        for e in stats.get("HitTraceEvents") or ():
            r = slot.get(e.get("SourceUnitId"))
            if r is not None and e.get("Damage", 0) > 0:
                f = e["Frame"]
                self.edges[r].add(f)
                self.hits[r, e.get("SkillId"), e.get("ActionId")].append(f)
        for e in stats.get("SkillExecEvents") or ():
            r = slot.get(e.get("UnitId"))
            if r is not None:
                self.edges[r].update((e.get("StartFrame", 0), e.get("EndFrame", 0)))
        for e in stats.get("BuffDebuffEvents") or ():
            for uid in (e.get("SourceUnitId"), e.get("TargetUnitId")):
                if uid in slot:
                    self.edges[slot[uid]].update((e.get("StartFrame", 0), e.get("EndFrame", 0)))
        progress = stats.get("ActionProgress") or {}
        self.progress = dict(zip(progress.get("Frames", ()), progress.get("Progress", ())))

    def opportunities(self):
        # 末次实际 UB 后仍满过 TP 的角色，可以尝试补一发。
        return sum(any(f > max(actual, default=0) for f in ready)
                   for actual, ready in zip(self.actual, self.ready))

    def earlier_hits(self, prior):
        # 同一来源、技能、动作的同一次命中；再确认相同采样帧上的动作进度有分歧。
        if not any(prior.progress.get(f) != p for f, p in self.progress.items() if f in prior.progress):
            return 0
        return sum(a < b for key, times in self.hits.items()
                   for a, b in zip(times, prior.hits.get(key, ())))


@dataclass(frozen=True)
class Suffix:
    role: int
    start: int
    tail: tuple
    reason: str
    kind: str = "重排"

    def apply(self, axis, end):
        if any(not FRAME_FLOOR <= base_frame(v) <= end for v in self.tail):
            return None
        lane = tuple(v for v in axis[self.role] if v < self.start) + self.tail
        return axis[:self.role] + (lane,) + axis[self.role + 1:]

    def describe(self, names):
        return f"{names[self.role]} 从第 {self.start} 帧起重排为 {'、'.join(map(text, self.tail))}（{self.reason}）"


def suffix_moves(axis, trace, donors):
    by_role = []
    for r, lane in enumerate(axis[:5]):
        variants = []
        # 从每一发轮流切开。借用另一场的后段，或让这一段全部在本场满 TP 时开。
        cuts = [base_frame(v) for v in lane] or [FRAME_FLOOR]
        for cut in reversed(cuts):
            tails = [(trace.ready[r], "按满 TP 帧连续释放")]
            tails += [(d[r], "借用另一开场的后段循环") for d in donors]
            for values, reason in tails:
                tail = tuple(v for v in values if v >= cut)
                if tail:
                    variants.append(Suffix(r, cut, tail, reason))
        by_role.append(variants)
    return interleave(by_role)


@dataclass(frozen=True)
class GroupMove:
    edits: tuple[Move | Suffix, ...]
    kind: str = "联动"

    def apply(self, axis, end):
        for move in self.edits:
            axis = move.apply(axis, end)
            if axis is None:
                break
        return axis

    def describe(self, names):
        return "；".join(m.describe(names) for m in self.edits)


def joint_moves(axis, trace):
    # 同一次群充涉及的队友可一起改。在同一候选里完成消耗旧 TP、让充能落地两步。
    pairs = []
    for frame, src, dst, overflow in reversed(trace.charges):
        if src == dst or not axis[src] or not axis[dst]:
            continue
        giver = min(axis[src], key=lambda v: abs(v - frame))
        taker = min(axis[dst], key=lambda v: abs(v - frame))
        if abs(giver - frame) > 180 or abs(taker - frame) > 180:
            continue
        ready = [f for f in trace.ready[dst] if f <= frame]
        first = (ready[-1] if ready and frame - ready[-1] <= 180 else frame - 1)
        for landing in (first, frame + 1):
            edits = (Move("调帧", src, giver, with_frac(giver, frame + 1), "等充能落地"),
                     Move("调帧", dst, taker, with_frac(taker, landing), "联动受充角色"))
            pairs.append(GroupMove(edits))
    return pairs


def phase_moves(axis, trace):
    # 将一刀移到前后较远的真实技能边界，并把队友紧随的 UB 成组平移。
    per_role = []
    for role, lane in enumerate(axis[:5]):
        per_ub = []
        for old in lane:
            f = base_frame(old)
            edges = sorted(e for e in trace.edges[role] if 8 <= abs(e - f) <= 180)
            before = [e for e in edges if e < f]
            after = [e for e in edges if e > f]
            candidates = []
            for e in dict.fromkeys(before[-1:] + after[:1] + before[:1] + after[-1:]):
                shift = e + 1 - f
                anchor = Move("换循环", role, old, with_frac(old, f + shift), "跨技能结算点")
                candidates.append(anchor)
                nearby = [(abs(other - old), r, other) for r, row in enumerate(axis[:5]) if r != role
                          for other in row if 0 <= other - old <= 120]
                if nearby:
                    _, r, other = min(nearby)
                    follow = Move("调帧", r, other, with_frac(other, base_frame(other) + shift), "跟随队友换循环")
                    candidates.append(GroupMove((anchor, follow)))
            per_ub.append(candidates)
        per_role.append(interleave(per_ub))
    return interleave(per_role)


def segment_moves(axis, trace, donors, rng, limit):
    """同时改一个时间段内数位角色，允许这一段整体换成另一次回放的出手次序。"""
    times = sorted({base_frame(v) for lane in axis[:5] for v in lane})
    if not times:
        return
    for _ in range(limit):
        start = rng.choice(times)
        stop = start + rng.choice((90, 240, 600, 1200))
        donor = rng.choice(donors) if donors and rng.randrange(2) else trace.ready
        changes = []
        for r in rng.sample(range(5), rng.randint(2, 4)):
            original = axis[r]
            old = [v for v in original if start <= v < stop]
            new = [v for v in donor[r] if start <= v < stop]
            if old != new:
                tail = tuple(new) + tuple(v for v in original if v >= stop)
                changes.append(Suffix(r, start, tail, "同段改用另一循环"))
        if len(changes) >= 2:
            yield GroupMove(tuple(changes), "换段")


def moves(axis, trace, end, limit, donors=(), layer=0):
    inserts, deletes, adjustments, charges = [[] for _ in range(4)]
    for role, lane in enumerate(axis[:5]):
        ready = trace.ready[role]
        actual = {base_frame(v) for v in trace.actual[role]}
        inserts.append([Move("补刀", role, None, f, "TP 满")
                        for f in reversed(ready) if f not in lane and base_frame(f) not in actual])
        deletes.append([Move("删刀", role, v, None, "比较保留小技能的整场伤害") for v in reversed(lane)])
        by_ub = []
        for v in reversed(lane):
            f = base_frame(v)
            edges = sorted((e for e in trace.edges[role] if e > 0 and abs(e - f) <= 120),
                           key=lambda e: (abs(e - f), e))
            offsets = [e + delta for e in edges[:4] for delta in (1, -1, 0)]
            # 没有临近效果时仍可试帧差，下一轮据新回包重建队友动作。
            by_ub.append([Move("调帧", role, v, with_frac(v, n), "技能或增减益边界")
                          for n in dict.fromkeys(offsets) if n != f])
        adjustments.append(list(interleave(by_ub)))
    for frame, src, dst, overflow in sorted(trace.charges, key=lambda c: (-c[3], -c[0])):
        if overflow > 0:
            later = next((v for v in axis[dst] if v >= frame), None)
            if later is not None:
                charges.append(Move("调充能", dst, later, with_frac(later, frame - 1), "在溢出前消耗 TP"))
        if src != dst and axis[src]:
            v = min(axis[src], key=lambda n: abs(n - frame))
            if abs(v - frame) <= 150:
                for offset in (1, -1):
                    charges.append(Move("调充能", src, v, with_frac(v, frame + offset),
                                        f"跨过给 {dst + 1} 号位的充能帧 {frame}"))
    found = {}
    # 重复扩展相似的父轴时轮换刀位，避免各层永远只检查同一批收尾帧。
    rng = Random(6151 + layer)
    groups = [charges, list(interleave(inserts)), list(interleave(deletes)), list(interleave(adjustments)),
              list(suffix_moves(axis, trace, donors)), joint_moves(axis, trace), list(phase_moves(axis, trace))]
    if layer:
        for group in groups:
            rng.shuffle(group)
    groups.append(segment_moves(axis, trace, donors, rng, limit))
    for move in interleave(groups):
        key = move.apply(axis, end)
        if key is not None and key != axis:
            found.setdefault(key, move)
            if len(found) >= limit:
                break
    return found


class CycleClient(SimulatorClient):
    def verify_export(self, exported, settings):
        # 从刚读回的文件重新取队伍、Boss、练度、世界线和种子。
        if exported.battle_seed != settings.seed:
            raise TuneError("导出文件中的种子与搜索种子不符。")
        context = exported.request_context(battle_time=settings.battle_time, battle_seed=exported.battle_seed)
        with SimulatorClient(context=context, endpoint=settings.endpoint, seeds=self.seeds,
                             batch_size=self.batch_size, timeout=settings.timeout, log=self.log) as replay:
            outcome = replay.evaluate_one(exported.frames)
            self.battles += replay.battles
            return outcome


    def trace(self, axis, units, duration):
        request = self.request_for(axis, self.seeds[0])
        request.update(CalcStats=511, ActionProgressFrames=list(range(30, duration * 60, 30)))
        row = self._post([request])[0]
        self._outcome(row, self.seeds[0], 1)
        return Trace(row["Stats"], units)

    def openings(self, boss):
        flags = [(False,) * 5, (True,) * 5, (True, False, True, False, True),
                 (False, True, False, True, False)]
        requests = [dict(self.request_for([[] for _ in range(5)] + list(boss) if boss else [], self.seeds[0]), IsAutoMode=True, SetToggles=list(t)) for t in flags]
        for t, response in zip(flags, self._post(requests), strict=True):
            outcome = self._outcome(response, self.seeds[0], 1)
            if len(outcome.ub_frames) >= 5:
                yield "".join("O" if v else "X" for v in t), outcome.ub_frames[:5]


class CycleJob:
    def __init__(self, save, settings, client, log, state=None):
        self.save, self.settings, self.client, self.log = save, settings, client, log
        self.names = [save.names.get(u, f"{i + 1} 号位") for i, u in enumerate(save.unit_ids)]
        self.stop = Event()
        self.best = self.baseline = None
        self.frontier = []
        self.expanded = set()
        self.history = []
        self.behavior = {}
        self.results = {}
        self.prior_battles = 0
        self.traces = {}
        self.donors = []
        self.by_count = {}
        if state and state.get("mode") != "cycle":
            raise ValueError("续跑状态与循环搜索模式不匹配。")
        if state and state.get("baseline"):
            self.best = Branch.restore(state["best"])
            self.baseline = Branch.restore(state["baseline"])
            self.frontier = [Branch.restore(v) for v in state["frontier"]]
            self.expanded = {axis_key(v) for v in state["expanded"]}
            self.history = list(state["history"])
            self.donors = [axis_key(d) for d in state.get("donors", ())]
            self.by_count = {int(k): Branch.restore(v) for k,v in state.get("by_count", {}).items()}
            self.prior_battles = state["battles"]

    def cancel(self):
        self.stop.set()

    @property
    def simulations(self):
        return self.prior_battles + self.client.battles

    def checkpoint(self):
        return dict(mode="cycle", baseline=asdict(self.baseline) if self.baseline else None,
                    best=asdict(self.best) if self.best else None, frontier=[asdict(b) for b in self.frontier],
                    expanded=sorted(self.expanded), history=self.history, battles=self.simulations, donors=self.donors,
                    by_count={k: asdict(v) for k, v in self.by_count.items()})

    def evaluate(self, proposals):
        keys = list(proposals)
        result = []
        width = max(1, self.settings.batch_size // self.settings.samples)
        missing = [axis for axis in keys if axis not in self.results]
        for start in range(0, len(missing), width):
            if self.stop.is_set():
                return []
            batch = missing[start:start + width]
            for axis, out in zip(batch, self.client.evaluate(batch), strict=True):
                if len(out.ub_frames) < 5:
                    raise TuneError("算器没有返回五人的实际 UB 帧，请检查算器版本。")
                self.results[axis] = (out.metric, out.ub_frames[:5])
        for axis in keys:
            metric, actual = self.results[axis]
            self.behavior[axis] = actual
            result.append(Branch(axis, metric, tuple(map(len, actual)), proposals[axis]))
        if len(self.results) > 12000:
            self.results = {axis: self.results[axis] for axis in keys}
        return result

    def initialize(self):
        axis = axis_key(self.save.frames)
        axis += ((),) * max(0, 5 - len(axis))
        seeds = {}
        if self.save.has_frames:
            seeds[axis] = ("原帧轴",)
        for name, rows in self.client.openings(axis[5:]):
            key = axis_key(rows) + axis[5:]
            seeds.setdefault(key, (f"{name} 开场转为手动帧轴",))
        if not seeds:
            raise TuneError("算器没有返回可用于起轴的实际 UB 帧。")
        starts = self.evaluate(seeds)
        if self.stop.is_set():
            return
        self.donors = list(seeds)
        self.record_counts(starts)
        self.baseline = starts[0]
        self.best = max(starts, key=lambda b: b.metric)
        self.frontier = sorted(starts, key=lambda b: -b.metric)[:self.settings.width]
        self.log(f"基线 {self.baseline.path[0]} {self.baseline.metric:,.0f}，实放 UB {self.baseline.counts}")

    def record_counts(self, branches):
        for b in branches:
            count = sum(b.counts)
            if count not in self.by_count or self.by_count[count].metric < b.metric:
                self.by_count[count] = b

    def trace(self, b):
        if b.axis not in self.traces:
            self.traces[b.axis] = self.client.trace(b.axis, self.save.unit_ids, self.settings.battle_time)
        return self.traces[b.axis]

    def advance(self):
        proposals, sources = {}, {}
        end = min(FRAME_CEILING, self.settings.battle_time * 60 - 1)
        parents = {b.axis for b in self.frontier}
        for b in self.frontier:
            if self.stop.is_set():
                return
            for axis, move in moves(b.axis, self.trace(b), end, self.settings.candidates, self.donors, len(self.history)).items():
                if axis in self.expanded or axis in parents or axis in proposals:
                    continue
                proposals[axis] = (*b.path, move.describe(self.names))
                sources[axis] = b, move.kind
        ranked = sorted(self.evaluate(proposals), key=lambda b: -b.metric)
        if self.stop.is_set():
            return
        width = self.settings.width
        if ranked:
            # 同分同实放帧通常只是输入表中多了一个没有放出的 UB。
            # 只折叠本层父代名额，下一层仍可由不同循环生成该输入。
            representatives = {}
            for b in ranked:
                key = (b.metric, self.behavior[b.axis])
                representatives.setdefault(key, b)
            ranked = list(representatives.values())
            # 低伤的前置改动也有独立的复跑名额。
            self.record_counts(ranked)
            shortlist = list(ranked[:width])
            # 给已多开 UB 的新循环留独立名额，即使刚改完时还没把伤害调好。
            variants = {}
            for b in ranked:
                variants.setdefault(b.counts, b)
            more = sorted(variants.values(), key=lambda b: (sum(b.counts), b.metric), reverse=True)
            shortlist.extend(more[:width])
            for kind in ("调充能", "删刀", "补刀", "调帧", "重排", "联动", "换循环", "换段"):
                shortlist.extend([b for b in ranked if sources[b.axis][1] == kind][:width])
            distinct = list(dict.fromkeys(shortlist))
            evidence = {}
            for b in distinct:
                if self.stop.is_set():
                    return
                parent, _ = sources[b.axis]
                t, prior = self.trace(b), self.trace(parent)
                earlier = sum(a < prev for lane, old in zip(t.ready, prior.ready) for a, prev in zip(lane, old))
                evidence[b] = (t.opportunities(), earlier, t.earlier_hits(prior), max(t.energy), b.metric)
            picks = [ranked[0]]
            if width >= 3 and more[0] != picks[0] and sum(more[0].counts) > sum(picks[0].counts):
                picks.append(more[0])
            for idx in range(4):
                if len(picks) >= width:
                    break
                remaining = [b for b in distinct if b not in picks and evidence[b][idx] > 0]
                if remaining:
                    picks.append(max(remaining, key=lambda b: (evidence[b][idx], b.metric)))
            if len(picks) < width:
                diverse = [b for b in more if b.counts not in {p.counts for p in picks}]
                if diverse:
                    picks.append(max(diverse, key=lambda b: b.metric))
            picks.extend(b for b in distinct if b not in picks)
            next_frontier = picks[:width]
            if ranked[0].metric > self.best.metric:
                self.best = ranked[0]
                self.log(f"整场新高 {self.best.metric:,.0f}，实放 UB {self.best.counts}，{self.best.path[-1]}")
        else:
            next_frontier = []
        self.behavior.clear()
        self.expanded.update(parents)
        self.frontier = next_frontier
        self.traces = {b.axis: self.traces[b.axis] for b in next_frontier if b.axis in self.traces}
        self.history.append(dict(damage=self.best.metric, simulations=self.simulations))
        self.log(f"第 {len(self.history)} 层测试 {len(proposals)} 条轴，保留 {len(next_frontier)} 个分支。")


class CycleSession:
    def __init__(self, save, settings, run_dir, *, log=print, resume_state=None, client=None):
        self.save, self.settings, self.log = save, settings, log
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.workbook = None
        self.summary = {}
        client = client or CycleClient(context=save.request_context(battle_time=settings.battle_time, battle_seed=settings.seed),
                                       endpoint=settings.endpoint, seeds=JobSettings(seed=settings.seed, samples=settings.samples).seeds(),
                                       timeout=settings.timeout, batch_size=settings.batch_size, log=log)
        self.job = CycleJob(save, settings, client, log, resume_state)
        write_json(self.run_dir / CONFIG, dict(mode="cycle", excel=str(save.path), settings=asdict(settings)))

    @property
    def cancelled_requested(self):
        return self.job.stop.is_set()

    def cancel(self):
        self.job.cancel()

    def close(self):
        self.job.client.close()

    def run(self):
        job = self.job
        if job.baseline is None and not self.cancelled_requested:
            job.initialize()
        while job.frontier and len(job.history) < self.settings.depth and not self.cancelled_requested:
            job.advance()
            write_json(self.run_dir / STATE, job.checkpoint())
        write_json(self.run_dir / STATE, job.checkpoint())
        if self.cancelled_requested:
            return 130
        best, base = job.best, job.baseline
        lines = [f"起点期望伤害 {base.metric:,.0f}，实际 UB {base.counts}",
                 f"最终期望伤害 {best.metric:,.0f}，实际 UB {best.counts}",
                 f"实际 UB 次数按首个种子统计，位号顺序为 {'、'.join(job.names)}。",
                 *best.path, "实放总次数及各自最高整场期望伤害",
                 *(f"{n} 发　{b.metric:,.0f}　{b.counts}" for n,b in sorted(job.by_count.items()))]
        winners = {"循环搜索结果.xlsx": best}
        extra = [b for n, b in job.by_count.items() if n > sum(base.counts)]
        if extra:
            for b in (max(extra, key=lambda x: x.metric), max(extra, key=lambda x: sum(x.counts))):
                if b != best:
                    winners[f"{sum(b.counts)}发分支.xlsx"] = b
        # 先在临时目录验证全套结果，中断或复核不一致时不发布候选。
        with TemporaryDirectory(dir=self.run_dir) as staging:
            for name, b in winners.items():
                if self.cancelled_requested:
                    return 130
                out = job.client.evaluate([b.axis])[0]
                rows = lines if b == best else [f"期望伤害 {b.metric:,.0f}，实际 UB {b.counts}", *b.path]
                pending = Path(staging) / name
                write_tuned_save(pending, self.save, b.axis, battle_seed=self.settings.seed,
                                 damage=out.damage, summary=[["循环搜索"]] + [[line] for line in rows])
                exported = read_save(pending, require_frames=False, allow_empty=True)
                check = job.client.verify_export(exported, self.settings)
                if check.metric != b.metric or tuple(map(len, check.ub_frames[:5])) != b.counts:
                    raise TuneError(f"{name} 读回复核与搜索成绩或实放 UB 次数不一致，请检查算器和数据版本。")
            if self.cancelled_requested:
                return 130
            for name in winners:
                (Path(staging) / name).replace(self.run_dir / name)
        self.workbook = self.run_dir / "循环搜索结果.xlsx"
        self.summary = dict(mode="cycle", baseline=base.metric, final=best.metric, ub_before=base.counts,
                            ub_after=best.counts, path=best.path, branches=list(winners)[1:],
                            best_by_count={n: b.metric for n, b in job.by_count.items()},
                            battles=job.simulations, result_workbook=str(self.workbook))
        write_json(self.run_dir / REPORT_JSON, self.summary)
        (self.run_dir / REPORT_MD).write_text("# 循环搜索\n\n" + "\n\n".join(lines), encoding="utf8")
        self.log(f"读回复核通过，结果轴 {self.workbook}")
        return 0
