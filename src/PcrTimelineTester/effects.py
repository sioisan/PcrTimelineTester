"""从一场真实回放中提取效果边界，生成单刀、两刀联动和同帧换序候选。"""

from __future__ import annotations

from .frames import FrameTarget, base_frame, with_frac
from .search import SearchSettings


def effect_frames(stats: dict, units: list[int]) -> list[set[int]]:
    by_unit = {unit: set() for unit in units}
    boss_changes = set()
    for key, owners, times in (
        ("HitTraceEvents", ("SourceUnitId",), ("Frame",)),
        ("TpChangeEvents", ("TargetUnitId", "SourceUnitId"), ("Frame",)),
        ("BuffDebuffEvents", ("SourceUnitId", "TargetUnitId"), ("StartFrame", "EndFrame")),
        ("BossDefEvents", (), ("Frame",)),
        ("BossMagicDefEvents", (), ("Frame",)),
    ):
        for event in stats.get(key) or ():
            if not isinstance(event, dict):
                continue
            frames = [event[t] for t in times if type(event.get(t)) is int and event[t] > 0]
            if not owners:
                boss_changes.update(frames)
            for owner in owners:
                if event.get(owner) in by_unit:
                    by_unit[event[owner]].update(frames)
    return [by_unit[unit] | boss_changes for unit in units]


def _valid(frames, target, value, settings):
    lane = frames[target.row]
    i = target.index
    frame = base_frame(value)
    return (settings.frame_min <= frame <= settings.frame_max
            and abs(frame - base_frame(lane[i])) <= settings.max_offset
            and (i == 0 or lane[i - 1] < value)
            and (i + 1 == len(lane) or value < lane[i + 1]))


def paired_alternates(frames, target, partner, alternatives, settings):
    for a in alternatives.get(target.key, ()):
        left = with_frac(frames[target.row][target.index], a)
        if left == frames[target.row][target.index] or not _valid(frames, target, left, settings):
            continue
        for b in alternatives.get(partner.key, ()):
            right = with_frac(frames[partner.row][partner.index], b)
            if right != frames[partner.row][partner.index] and _valid(frames, partner, right, settings):
                yield ((target, left), (partner, right))


def proposals(frames, target: FrameTarget, partner: FrameTarget | None, events, settings: SearchSettings):
    """每项候选含实际要改的输入，保留角色自己的小数尾。"""
    raw = frames[target.row][target.index]
    center = base_frame(raw)
    window = settings.max_offset
    near = [f for f in events[target.row] if abs(f - center) <= window]
    before = sorted((f for f in near if f < center), reverse=True)[:3]
    after = sorted(f for f in near if f >= center)[:4]

    for event in before + after:
        for offset in (0, 1, -1):
            value = with_frac(raw, event + offset)
            if value != raw and _valid(frames, target, value, settings):
                yield ((target, value),)
    if partner is None:
        return
    other = frames[partner.row][partner.index]
    second = base_frame(other)
    if second == center and raw != other:
        if _valid(frames, target, other, settings) and _valid(frames, partner, raw, settings):
            yield ((target, other), (partner, raw))
    offsets = tuple(dict.fromkeys((1, settings.step, -1, -settings.step)))
    for a in offsets:
        for b in offsets:
            left, right = with_frac(raw, center + a), with_frac(other, second + b)
            if _valid(frames, target, left, settings) and _valid(frames, partner, right, settings):
                yield ((target, left), (partner, right))
    # 两人各自跨越一次事件边界；照顾离起始帧较远的跳伤和回能。
    def edges(t, original):
        f = base_frame(original)
        near = sorted(e for e in events[t.row] if 0 < abs(e - f) <= window)
        lower = [e for e in near if e < f]
        upper = [e for e in near if e > f]
        return dict.fromkeys((*lower[:1], *lower[-1:], *upper[:1], *upper[-1:]))

    for a in edges(target, raw):
        for b in edges(partner, other):
            left, right = with_frac(raw, a), with_frac(other, b)
            if _valid(frames, target, left, settings) and _valid(frames, partner, right, settings):
                yield ((target, left), (partner, right))
    # 第一刀跨到效果落点、队友微移一帧，覆盖两处单改都不涨的组合。
    for event in before[:1] + after[:1]:
        for b in (-1, 1):
            left, right = with_frac(raw, event + 1), with_frac(other, second + b)
            if left != raw and _valid(frames, target, left, settings) and _valid(frames, partner, right, settings):
                yield ((target, left), (partner, right))
