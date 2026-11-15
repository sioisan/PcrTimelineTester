"""Frame integers and the ``0.001`` same-frame ordering tail.

Game frames are floats: ``4485.001`` / ``5264.002``.  The fraction is not noise,
it is the order of two actions inside the same frame, so moving ``4485.001`` to
``4480`` has to be written as ``4480.001`` or the same-frame order is lost.

``Frames`` / ``FrameOption`` keep both halves (整数帧 + 小数尾) 一起走，往返一次也不会
把同一帧里的两个动作换个先后。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math

#: Rows of ``UBExecTimeData`` that belong to the five player slots.
CHARACTER_ROWS = 5

#: Label used when the workbook carries no name for a slot.
FALLBACK_BOSS = "Boss"


def base_frame(raw: float) -> int:
    """Integer frame of ``raw`` (C# ``Math.Round(..., AwayFromZero)``)."""
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"帧必须是有限数值：{raw!r}")
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


def frac_of(raw: float) -> float:
    """Ordering tail relative to :func:`base_frame` (``0.001`` style)."""
    frame = base_frame(raw)
    tail = round(float(raw) - frame, 3)
    return 0.0 if tail == 0.0 else tail


def with_frac(raw: float, frame: int) -> float:
    """Integer ``frame`` carrying the ordering tail of the original ``raw``."""
    tail = frac_of(raw)
    return float(frame) if tail <= 0.0 else float(frame) + tail


def text(raw: float) -> str:
    """Display text of a frame, keeping the tail: ``4485.001`` / ``4480``."""
    value = float(raw)
    if value.is_integer():
        return str(int(value))
    return f"{value:.3f}".rstrip("0").rstrip(".")


@dataclass(frozen=True, slots=True)
class FrameTarget:
    """One adjustable item: the ``index``-th UB of row ``row``."""

    row: int
    index: int
    label: str
    raw: float

    @property
    def key(self) -> str:
        return f"{self.row}:{self.index}"

    @property
    def frame(self) -> int:
        return base_frame(self.raw)

    @property
    def raw_text(self) -> str:
        return text(self.raw)

    def to_raw(self, frame: int) -> float:
        return with_frac(self.raw, frame)


def row_labels(
    unit_ids: list[int],
    *,
    names: dict[int, str] | None = None,
    boss_name: str | None = None,
    rows: int = CHARACTER_ROWS,
) -> list[str]:
    """Per-row display names: the character name, ``角色{slot}`` when unknown."""
    names = names or {}
    labels: list[str] = []
    for row in range(rows):
        if row >= CHARACTER_ROWS:
            labels.append(boss_name or FALLBACK_BOSS)
            continue
        unit_id = unit_ids[row] if row < len(unit_ids) else 0
        label = names.get(unit_id) if unit_id else None
        if not label:
            label = f"角色{row + 1}"
        labels.append(label)
    # Two units can share a name (same character, different costume), so append the
    # id only where the plain name would be ambiguous.
    counts = Counter(labels[:CHARACTER_ROWS])
    for row in range(min(CHARACTER_ROWS, len(labels))):
        if counts[labels[row]] > 1:
            unit_id = unit_ids[row] if row < len(unit_ids) else 0
            labels[row] = f"{labels[row]}#{unit_id or row + 1}"
    return labels


def build_targets(
    frames: list[list[float]],
    unit_ids: list[int],
    *,
    names: dict[int, str] | None = None,
    boss_name: str | None = None,
    include_boss: bool = False,
    items: set[tuple[int, int]] | None = None,
    max_items: int | None = None,
) -> list[FrameTarget]:
    """Enumerable testable items, ordered by frame (the earliest UB first).

    ``items`` uses one-based ``(slot, ub)`` pairs taken from the command line, so
    ``1:2`` means "the second UB of the first character slot".  Labels show the
    character name only (``春花 UB2``) — the slot number is an implementation
    detail and is never displayed.

    Ordering is by the UB's own frame (not by character), because that is the order
    the UBs actually fire in: ties keep the table order (slot, then UB index).
    """
    rows = len(frames) if include_boss else min(CHARACTER_ROWS, len(frames))
    labels = row_labels(
        unit_ids, names=names, boss_name=boss_name, rows=max(rows, CHARACTER_ROWS),
    )
    collected: list[FrameTarget] = []
    for row in range(rows):
        prefix = labels[row] if row < len(labels) else FALLBACK_BOSS
        for index, raw in enumerate(frames[row]):
            if items is not None and (row + 1, index + 1) not in items:
                continue
            if not isinstance(raw, (int, float)) or not math.isfinite(float(raw)) or raw <= 0:
                continue
            collected.append(FrameTarget(row, index, f"{prefix} UB{index + 1}", float(raw)))
    collected.sort(key=lambda target: (target.frame, target.row, target.index))
    if max_items is not None:
        return collected[: max(int(max_items), 0)]
    return collected
