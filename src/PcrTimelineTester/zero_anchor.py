"""在前缀回放中寻找可以实际操作的满 TP、队友 UB 及自然收招时机。"""
from collections import OrderedDict
from dataclasses import replace

from .zero import TEAM_SIZE, STATS_BITS, ub_frames


def cast_order(lanes):
    return tuple(sorted((float(value), role) for role, row in enumerate(lanes[:TEAM_SIZE]) for value in row))


def player_lanes(casts):
    rows = [[] for _ in range(TEAM_SIZE)]
    for frame, role in casts:
        rows[role].append(frame)
    return tuple(map(tuple, rows))


def next_operations(prefix, stats, units):
    """只发前缀，观察没有下一发 UB 打断时的 TP 状态和技能自然结束帧。"""
    last = int(prefix[-1][0]) if prefix else 0
    next_order = sum(int(at) == last for at, _ in prefix)
    choices = {}
    tp = [[] for _ in units]
    skills = {uid: [] for uid in units}
    for event in stats.get("TpChangeEvents") or ():
        role = event.get("TargetLaneIndex", -1)
        if 0 <= role < len(units):
            tp[role].append(event)
    for event in stats.get("SkillExecEvents") or ():
        if event["UnitId"] in skills:
            skills[event["UnitId"]].append((event["StartFrame"], event["EndFrame"]))
    def full(events, frame):
        for event in reversed(events):
            if event["Frame"] <= frame:
                return event.get("IsReadyAfter", False) or (
                    event.get("EnergyCap", 0) > 0 and event.get("EnergyAfter", 0) >= event["EnergyCap"])
        return False

    for role, events in enumerate(tp):
        triggers = [(int(at), source) for at, source in prefix if source != role]
        triggers += [(e["Frame"], None) for e in events if e.get("CrossedReady")]
        for at, source in triggers:
            if not full(events, at):
                continue
            origin = "自身 TP 满" if source is None else f"{at} 帧 {source + 1} 号位 UB"
            end = next((b for a, b in skills[units[role]] if a <= at and (b < 0 or at < b)), at)
            for dest, flag in ((at, True), (end, False)):
                if dest < last or dest <= 0 or not full(events, dest):
                    continue
                # 同一角色不能在一个逻辑帧重复消费 TP。
                if any(r == role and int(t) == dest for t, r in prefix):
                    continue
                frame = round(dest + (next_order / 1000 if dest == last else 0), 3)
                if not flag and any(at < e["Frame"] <= dest and not e.get("IsReadyAfter", True)
                                    for e in events):
                    continue
                choices.setdefault((frame, role), (flag, f"{origin} → {role + 1} 号位"
                                                f" {'SET' if flag else 'AUTO'}（{dest} 帧）"))
    return choices


class AnchorSearch:
    def __init__(self, evaluator, save):
        self.evaluator = evaluator
        self.tail = tuple(map(tuple, save.frames[TEAM_SIZE:]))
        self.cache = OrderedDict()
        self.tried = OrderedDict()

    def request(self, prefix, seed, trace):
        body = self.evaluator.request_for(player_lanes(prefix) + self.tail, seed=seed)
        body.update(IsAutoMode=False, CalcStats=STATS_BITS | 16 if trace else 3)
        return body

    def warm(self, prefixes, seed):
        missing = list(dict.fromkeys(p for p in prefixes if (seed, p) not in self.cache))
        for start in range(0, len(missing), 12):
            part = missing[start:start + 12]
            requests = [self.request(p, seed, True) for p in part]
            for p, body, row in zip(part, requests, self.evaluator._post(requests), strict=True):
                self.evaluator._score(row, body)
                actual = cast_order(ub_frames(row))
                self.cache[seed, p] = (next_operations(p, row["Stats"], self.evaluator.units)
                                       if actual == p else {})

    def validate(self, casts, seed):
        prefixes = [casts[:i] for i in range(len(casts))]
        self.warm(prefixes, seed)
        steps = []
        for prefix, cast in zip(prefixes, casts):
            choices = self.cache[seed, prefix]
            if cast not in choices:
                return None
            steps.append(choices[cast])
        while len(self.cache) > 512:
            self.cache.popitem(last=False)
        return steps

    def evaluate(self, variants, floor=float("-inf")):
        seeds = self.evaluator.settings.seeds()
        variants = list(dict.fromkeys(c for c in variants if c))
        requests = [self.request(casts, seed, False) for casts in variants for seed in seeds]
        replies = self.evaluator._post(requests)
        ranked = []
        for i, casts in enumerate(variants):
            start = i * len(seeds)
            group = replies[start:start + len(seeds)]
            scores = [self.evaluator._score(row, body) for row, body in
                      zip(group, requests[start:start + len(seeds)], strict=True)]
            damage = sum(s.damage for s in scores) / len(scores)
            if damage >= floor and all(cast_order(ub_frames(row)) == casts for row in group):
                ranked.append((damage, casts, scores[0]))
        for damage, casts, score in sorted(ranked, key=lambda x: -x[0]):
            steps = self.validate(casts, seeds[0])
            if steps is None or any(self.validate(casts, seed) != steps for seed in seeds[1:]):
                continue
            flags = [[] for _ in range(TEAM_SIZE)]
            for (_, role), (flag, _) in zip(casts, steps):
                flags[role].append(flag)
            return replace(score, damage=damage, lanes=player_lanes(casts) + self.tail,
                           plan=tuple(map(tuple, flags)), steps=tuple(label for _, label in steps))
        return None

    def neighbors(self, score, rng, limit, count):
        casts = cast_order(score.lanes)
        seed = self.evaluator.settings.seed
        self.warm([casts[:i] for i in range(len(casts) + 1)], seed)
        variants = []
        for i in range(len(casts) + 1):
            prefix = casts[:i]
            choices = self.cache[seed, prefix]
            for candidate in choices:
                frame, role = candidate
                if i < len(casts) and casts[i] == candidate:
                    continue
                # 调整一人的下一刀或在收尾补刀，保留其他队友的操作供整场复核。
                suffix = list(casts[i:])
                old = next((v for v in suffix if v[1] == role), None)
                if old:
                    suffix.remove(old)
                elif sum(r == role for _, r in casts) >= limit:
                    continue
                if (frame, role) not in suffix:
                    variants.append(tuple(sorted((*prefix, candidate, *suffix))))
        while len(self.cache) > 512:
            self.cache.popitem(last=False)
        unique = [c for c in dict.fromkeys(variants) if c not in self.tried and c != casts]
        selected = rng.sample(unique, min(count, len(unique)))
        self.tried.update((c, None) for c in selected)
        while len(self.tried) > 2048:
            self.tried.popitem(last=False)
        return selected

