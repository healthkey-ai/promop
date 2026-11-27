"""Split the backend test suites across parallel CI jobs (#2939).

Both suites already use every core of one runner (pytest-xdist, Django's
``--parallel``), so the only way to go faster is to run part of each suite on
another runner. A *shard* is a stable subset of the tests; the same partition is
computed independently by every job from the same inputs, so no job needs to
know what another one picked, and every test lands in exactly one shard.

The unit that is partitioned is a whole test file (pytest) or a whole test class
(Django runner): that keeps xdist's ``loadfile`` semantics and module/class
scoped fixtures intact inside a shard.

Items are placed greedily, heaviest first, on the currently lightest shard (the
LPT rule, within 4/3 of the best possible split). Weights are measured seconds
where known; an item with no recorded weight is given the average one, so a new
test file or class is spread rather than piled on one shard. Ties are broken by
name, so the result never depends on dict or collection order.

Environment, read by both entry points: ``TEST_SHARD_COUNT`` (default 1, which
means no sharding) and ``TEST_SHARD_INDEX`` (1-based).
"""

import json
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

DEFAULT_WEIGHT = 1.0


def shard_settings(environ=None) -> Optional[tuple]:
    """``(index, count)`` from the environment, or None when not sharding."""
    environ = os.environ if environ is None else environ
    count = int(environ.get('TEST_SHARD_COUNT') or 1)
    if count <= 1:
        return None
    index = int(environ.get('TEST_SHARD_INDEX') or 0)
    if not 1 <= index <= count:
        raise ValueError(
            f'TEST_SHARD_INDEX must be between 1 and TEST_SHARD_COUNT ({count}), got {index}'
        )
    return index, count


def load_weights(path) -> Dict[str, float]:
    """Read a ``{name: seconds}`` file; a missing or unreadable one is no weights."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): float(v) for k, v in data.items() if float(v) >= 0}


def partition(
    names: Iterable[str], weights: Dict[str, float], count: int
) -> List[Set[str]]:
    """Split ``names`` into ``count`` shards of about equal total weight."""
    names = sorted(set(names))
    known = [weights[n] for n in names if n in weights]
    fallback = (sum(known) / len(known)) if known else DEFAULT_WEIGHT
    ordered = sorted(names, key=lambda n: (-weights.get(n, fallback), n))
    shards: List[Set[str]] = [set() for _ in range(count)]
    loads = [0.0] * count
    for name in ordered:
        lightest = min(range(count), key=lambda i: (loads[i], i))
        shards[lightest].add(name)
        loads[lightest] += weights.get(name, fallback)
    return shards


def shard_of(
    names: Iterable[str], weights: Dict[str, float], index: int, count: int
) -> Set[str]:
    """The names that belong to shard ``index`` (1-based) of ``count``."""
    return partition(names, weights, count)[index - 1]
