"""Merge detector boxes into line regions (one region = one visual text segment)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .geometry import Box, iou, overlap_over_smaller, union, vertical_overlap_ratio
from .script import classify


@dataclass
class Region:
    id: int
    boxes: dict[str, Box]
    extent: Box
    members: list[Box] = field(default_factory=list, repr=False)


def _compatible(a: Box, b: Box) -> bool:
    if vertical_overlap_ratio(a, b) < 0.5:
        return False
    if iou(a, b) >= 0.3:
        return True
    big, small = (a, b) if a.area() >= b.area() else (b, a)
    return (
        overlap_over_smaller(big, small) >= 0.8
        and small.area() > 0
        and big.area() / small.area() <= 3
    )


def _drop_redundant_containers(boxes: list[Box]) -> list[Box]:
    keep = []
    for cand in boxes:
        inside = [
            o
            for o in boxes
            if o is not cand
            and o.area() < cand.area()
            and overlap_over_smaller(cand, o) >= 0.8
        ]
        covered = sum(o.area() for o in inside)
        if len(inside) >= 2 and cand.area() > 0 and covered / cand.area() >= 0.8:
            continue
        keep.append(cand)
    return keep


def _connected_components(boxes: list[Box]) -> list[list[Box]]:
    """Group boxes as connected components of the _compatible graph (order-free)."""
    parent = list(range(len(boxes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if _compatible(boxes[i], boxes[j]):
                parent[find(i)] = find(j)
    comps: dict[int, list[Box]] = {}
    for i, b in enumerate(boxes):
        comps.setdefault(find(i), []).append(b)
    return list(comps.values())


def _row_is_rtl(row: list[Region]) -> bool:
    """A row reads right-to-left when most of its detected letters are Arabic."""
    text = " ".join(m.text for r in row for m in r.members if m.text)
    return classify(text) == "ar"


def _reading_order(regions: list[Region]) -> list[Region]:
    """Cluster regions into rows by vertical overlap; rows top-down. Within a
    row, regions run right-to-left when the row is mainly Arabic (so consecutive
    LINE blocks of one Arabic value join in reading order), else left-to-right.
    """
    rows: list[list[Region]] = []
    row_extent: Box | None = None
    for r in sorted(regions, key=lambda r: (r.extent.y0, r.extent.x0)):
        if (
            row_extent is not None
            and vertical_overlap_ratio(r.extent, row_extent) >= 0.5
        ):
            rows[-1].append(r)
            row_extent = union([row_extent, r.extent], "row")
        else:
            rows.append([r])
            row_extent = r.extent
    ordered: list[Region] = []
    for row in rows:
        if _row_is_rtl(row):
            ordered.extend(sorted(row, key=lambda r: -r.extent.x1))
        else:
            ordered.extend(sorted(row, key=lambda r: r.extent.x0))
    return ordered


def build_regions(boxes: list[Box]) -> list[Region]:
    boxes = [b for b in boxes if b.area() >= 4]
    boxes = _drop_redundant_containers(boxes)
    regions = []
    for g in _connected_components(boxes):
        per_source: dict[str, list[Box]] = {}
        for m in g:
            per_source.setdefault(m.source, []).append(m)
        per_source_union = {s: union(v, s) for s, v in per_source.items()}
        regions.append(Region(0, per_source_union, union(g, "region"), g))
    regions = _reading_order(regions)
    for i, r in enumerate(regions, 1):
        r.id = i
    return regions
