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
It is generated, not written. ``fields`` is the confidence columns that appear as a
quoted string in the tab components below, less ``not_for_patients``; those tabs are
on screen whatever the disease. ``disease_fields`` is the same scan of the disease
tab, one entry per disease type: that tab shows one section for the patient's own
disease, which the page decides from the disease text (``disease_rules``, first
keyword found wins, read by ``provisional.ts`` and ``disease_type`` below), so the
fields on screen are ``fields`` plus the entry for that type; ``disease_sections`` names
the functions of the disease tab each type renders, so the frontend check can redo that
scan. ``columns`` is every
scalar confidence column, so the frontend check can redo the scan without the models.
``tests/test_patient_visible_fields.py`` and a vitest check fail, in both directions,
when a tab changes and the file does not follow; regenerate it with::

    python -m omop_core.services.patient_visible_fields --write

A control that does not render through ``Field`` (the treating institution picker, the
GELF and FLIPI checklists) is wrapped in ``MarkedControl`` with the record field it
shows quoted at the call site, which is how the scan finds it and how it shows the
same indicator.

``not_for_patients`` names the fields the scanned tabs render only for staff (the
General tab's Clinician Validation section, hidden in patient mode and refused on a
patient's PATCH): a mark on one could never be cleared by a patient.

Left out on purpose: the treatment tab (therapy lines are edited in their own
dialog, and its planned-therapies picker is not a column that holds a proposal) and
the read-only summary.
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
DISEASE_TAB = 'DiseaseTab.tsx'
LIST_FILE = pathlib.Path('frontend/src/components/PatientInfo/patientVisibleFields.json')

# Rendered only outside patient mode (GeneralTab's Clinician Validation section).
NOT_FOR_PATIENTS = ('validated', 'validated_by', 'validation_date')

# The functions of DiseaseTab.tsx that render for each disease type the page can
# pick (``DiseaseTab``'s switch). A new ``*Section`` function must be added here or
# the drift test fails.
DISEASE_SECTIONS = {
    'breast': ('BreastCancerSection', 'StagingBiomarkersSection'),
    'lymphoma': ('LymphomaSection',),
    'myeloma': ('MyelomaSection',),
    'cll': ('CLLSection',),
    'mcl': ('MCLSection',),
}

# How the page names a record's disease type from its disease text
# (``getDiseaseType`` in PatientDetail.tsx): the first keyword found in the
# lowercased text wins, and no match is ``other``, which has no disease tab.
DISEASE_RULES = (
    ('breast', 'breast'),
    ('mantle', 'mcl'),
    ('lymphoma', 'lymphoma'),
    ('myeloma', 'myeloma'),
    ('cll', 'cll'),
    ('chronic lymphocytic', 'cll'),
)

# Functions of DiseaseTab.tsx that render no disease fields of their own.
NOT_SECTIONS = ('DatePrecisionPicker', 'DiseaseTab')

_QUOTED = re.compile(r"""['"]([a-z][a-z0-9_]*)['"]""")
_FUNCTION_START = re.compile(r'\n(?=(?:export default )?function \w+)')
_FUNCTION_NAME = re.compile(r'(?:export default )?function (\w+)')


def scalar_confidence_columns() -> frozenset[str]:
    """The scalar fields that can carry a proposal (a list field never blocks)."""
    from omop_core.derived_fields import PROVISIONAL_JSON_FIELDS
    from omop_core.models import PatientRecordConfidence
    return frozenset(
        field.name for field in PatientRecordConfidence._meta.concrete_fields
        if field.name != 'person' and field.name not in PROVISIONAL_JSON_FIELDS
    )


def function_blocks(source: str) -> dict[str, str]:
    """A TSX file's top-level functions by name, each with the text up to the next."""
    blocks: dict[str, str] = {}
    for part in _FUNCTION_START.split(source):
        match = _FUNCTION_NAME.match(part)
        if match:
            blocks[match.group(1)] = part
    return blocks


def _named(text: str, columns: frozenset[str]) -> set[str]:
    return {token for token in _QUOTED.findall(text) if token in columns}


def scan(root: pathlib.Path = ROOT) -> list[str]:
    """The scalar confidence columns the always-shown patient tabs name, sorted."""
    columns = scalar_confidence_columns()
    names: set[str] = set()
    for tab in SCANNED_TABS:
        names |= _named((root / TAB_DIR / tab).read_text(), columns)
    return sorted(names - set(NOT_FOR_PATIENTS))


def scan_disease(root: pathlib.Path = ROOT) -> dict[str, list[str]]:
    """The scalar confidence columns the disease tab names, per disease type, sorted."""
    columns = scalar_confidence_columns()
    blocks = function_blocks((root / TAB_DIR / DISEASE_TAB).read_text())
    return {
        disease: sorted(set().union(*(_named(blocks[name], columns) for name in sections))
                        - set(NOT_FOR_PATIENTS))
        for disease, sections in DISEASE_SECTIONS.items()
    }


def render(names: list[str], disease: dict[str, list[str]]) -> str:
    return json.dumps({
        'columns': sorted(scalar_confidence_columns()),
        'not_for_patients': list(NOT_FOR_PATIENTS),
        'fields': names,
        'disease_rules': [list(rule) for rule in DISEASE_RULES],
        'disease_sections': {kind: list(names) for kind, names in DISEASE_SECTIONS.items()},
        'disease_fields': disease,
    }, indent=2) + '\n'


@functools.lru_cache(maxsize=1)
def _stored() -> dict:
    return json.loads((ROOT / LIST_FILE).read_text())


def names() -> frozenset[str]:
    """The stored list of fields on screen whatever the disease."""
    return frozenset(_stored()['fields'])


def disease_type(disease: object) -> str:
    """The page's disease type for a disease text (``other`` when it has none)."""
    text = disease.lower() if isinstance(disease, str) else ''
    for keyword, kind in _stored()['disease_rules']:
        if keyword in text:
            return kind
    return 'other'


def on_screen(disease: object) -> frozenset[str]:
    """The stored fields a patient sees for a record with this disease text: what blocks."""
    stored = _stored()
    return names() | frozenset(stored['disease_fields'].get(disease_type(disease), ()))


def write(root: pathlib.Path = ROOT) -> list[str]:
    found = scan(root)
    (root / LIST_FILE).write_text(render(found, scan_disease(root)))
    return found


if __name__ == '__main__':
    if '--write' not in sys.argv:
        sys.exit('Pass --write to regenerate patientVisibleFields.json.')
    import os
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'promop.settings')
    import django
    django.setup()
    print(f'{len(write())} fields written.')
