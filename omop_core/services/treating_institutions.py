"""Public NCI treating-center snapshot and deterministic sample assignments."""
import hashlib
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def institution_directory():
    return json.loads((Path(__file__).resolve().parents[1] / 'data' / 'treating_institutions.json').read_text())


def sample_institution(person_id, region=''):
    # Sample cancer cohorts are adults; do not assign a pediatric-only center.
    centers = [c for c in institution_directory()['institutions'] if not c['pediatric_only']]
    # PatientRecord may carry either the state name or its postal abbreviation.
    state = region.strip().casefold()
    local = [c for c in centers if state in {c['state'].casefold(), c['state_code'].casefold()}]
    choices = local or centers
    index = int.from_bytes(hashlib.sha256(f'institution:{person_id}'.encode()).digest()[:8], 'big')
    return choices[index % len(choices)]['label']
