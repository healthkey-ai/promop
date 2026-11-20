"""The scalar fields a patient sees on their page and can edit there (#2761).

A patient's "Confirm my record" is refused while the page shows a field marked
Provisional that the patient can clear. Only a field a patient tab renders can
be cleared from the page, so the list of such fields decides what blocks: a
field no tab renders (``concomitant_medications``, the diagnosis date, a lab
alias) is marked in the record read but never on screen, and blocking on it
would be a block no patient could lift.

The list is ``frontend/src/components/PatientInfo/patientVisibleFields.json``: one
file, imported by the frontend (``PatientInfo/provisional.ts``, whose image is built
from ``frontend/`` alone) and read here (every backend image carries ``frontend/``).
It is generated, not written: ``fields`` is the confidence columns that appear as a
quoted string in the tab components below, less ``not_for_patients``; ``columns``
is every scalar confidence column, so the frontend check can redo the scan without
the models. ``tests/test_patient_visible_fields.py`` and a vitest check fail, in
both directions, when a tab changes and the file does not follow; regenerate it with::

    python -m omop_core.services.patient_visible_fields --write

``not_for_patients`` names the fields the scanned tabs render only for staff (the
General tab's Clinician Validation section, hidden in patient mode and refused on a
patient's PATCH): a mark on one could never be cleared by a patient.

Left out on purpose: the disease tab (its fields show only for the patient's own
disease, which the page decides from the disease text, so a static list cannot
say a field is on screen), the treatment tab (therapy lines are edited in their
own dialog), the read-only summary, and the controls that bypass ``Field`` (GELF,
FLIPI, treating institution), which show no mark. A proposal on one of those is
therefore shown but does not block; the plan lists them as open.
"""
from __future__ import annotations

import functools
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
TAB_DIR = pathlib.Path('frontend/src/components/PatientInfo/tabs')
SCANNED_TABS = ('GeneralTab.tsx', 'LabsTab.tsx', 'BehaviorTab.tsx', 'WearableTab.tsx')
LIST_FILE = pathlib.Path('frontend/src/components/PatientInfo/patientVisibleFields.json')

# Rendered only outside patient mode (GeneralTab's Clinician Validation section).
NOT_FOR_PATIENTS = ('validated', 'validated_by', 'validation_date')

_QUOTED = re.compile(r"""['"]([a-z][a-z0-9_]*)['"]""")


def scalar_confidence_columns() -> frozenset[str]:
    """The scalar fields that can carry a proposal (a list field never blocks)."""
    from omop_core.derived_fields import PROVISIONAL_JSON_FIELDS
    from omop_core.models import PatientRecordConfidence
    return frozenset(
        field.name for field in PatientRecordConfidence._meta.concrete_fields
        if field.name != 'person' and field.name not in PROVISIONAL_JSON_FIELDS
    )


def scan(root: pathlib.Path = ROOT) -> list[str]:
    """The scalar confidence columns the patient tabs name, sorted."""
    columns = scalar_confidence_columns()
    names: set[str] = set()
    for tab in SCANNED_TABS:
        source = (root / TAB_DIR / tab).read_text()
        names.update(token for token in _QUOTED.findall(source) if token in columns)
    return sorted(names - set(NOT_FOR_PATIENTS))


def render(names: list[str]) -> str:
    return json.dumps({
        'columns': sorted(scalar_confidence_columns()),
        'not_for_patients': list(NOT_FOR_PATIENTS),
        'fields': names,
    }, indent=2) + '\n'


@functools.lru_cache(maxsize=1)
def names() -> frozenset[str]:
    """The stored list: what blocks a patient's confirmation."""
    return frozenset(json.loads((ROOT / LIST_FILE).read_text())['fields'])


def write(root: pathlib.Path = ROOT) -> list[str]:
    found = scan(root)
    (root / LIST_FILE).write_text(render(found))
    return found


if __name__ == '__main__':
    if '--write' not in sys.argv:
        sys.exit('Pass --write to regenerate patientVisibleFields.json.')
    import os
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'promop.settings')
    import django
    django.setup()
    print(f'{len(write())} fields written.')
