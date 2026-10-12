from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkDiff:
    reused: tuple[tuple[int, int], ...]
    added: tuple[int, ...]
    removed: tuple[int, ...]


def plan_diff(old_spans, new_spans):
    """Match content identities by occurrence, never carry old locators forward."""
    positions = defaultdict(deque)
    for index, value in enumerate(old_spans):
        positions[value].append(index)
    reused, added = [], []
    for index, value in enumerate(new_spans):
        if positions[value]:
            reused.append((positions[value].popleft(), index))
        else:
            added.append(index)
    return ChunkDiff(tuple(reused), tuple(added), tuple(sorted(i for values in positions.values() for i in values)))
