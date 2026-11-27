#!/usr/bin/env python3
"""Rebuild tests/shard_weights.json from a pytest log run with ``--durations=0`` (#2939).

The pytest CI legs split the suite by whole test file, weighted by how long each
file takes (promop/test_sharding.py). Weights drift as tests are added; a file
with no weight is given the average one, so drift costs balance, never
correctness. To refresh them, run the suite once with every duration reported
(``pytest -n auto --dist loadfile -q --durations=0 --durations-min=0``, e.g. in a
throwaway CI run), save the job log, and:

    python scripts/update_test_shard_weights.py job.log

It sums setup, call and teardown seconds per file.
"""
import collections
import json
import re
import sys
from pathlib import Path

LINE = re.compile(r'(\d+\.\d+)s (?:call|setup|teardown)\s+(tests/[^:\s]+\.py)::')
OUT = Path(__file__).resolve().parent.parent / 'tests' / 'shard_weights.json'


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    totals = collections.defaultdict(float)
    for line in Path(argv[1]).read_text(errors='replace').splitlines():
        match = LINE.search(line)
        if match:
            totals[match.group(2)] += float(match.group(1))
    if not totals:
        print('No duration lines found; was the run made with --durations=0?', file=sys.stderr)
        return 1
    OUT.write_text(json.dumps({k: round(v, 2) for k, v in sorted(totals.items())}, indent=1) + '\n')
    print(f'{len(totals)} files, {sum(totals.values()):.0f} s of work -> {OUT}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
