"""One disease's fields and their concepts, as a list and as a CSV (#2331).

Two sets exist for a Disease Category: its **extension** (the
disease-specific fields) and its **profile** (the full record: the base
record plus the extension). Both are cut from the field descriptors, which
carry ``scope`` and ``diseases`` per field from one read of the membership
rows, so neither costs a query per field or per disease.

The CSV is a review copy for a curator working offline. It is reference data:
no patient value, and provenance as the curation fixture carries it, with a
reviewer's name replaced by ``curator``. Nothing loads it back.
"""
from __future__ import annotations

import csv
import io
from collections.abc import Iterable

from django.utils.timezone import localdate

from omop_core.services import disease_profiles

SET_PROFILE = 'profile'
SET_EXTENSION = 'extension'
SETS = (SET_PROFILE, SET_EXTENSION)

COLUMNS = (
    'field_name', 'label', 'scope', 'diseases', 'tab', 'field_type', 'omop_table',
    'concept_id', 'concept_name', 'vocabulary_id', 'concept_code', 'unit',
    'mapping_status', 'provenance',
)


class SetError(ValueError):
    """A disease or a set the caller named that does not exist."""


def _codes(diseases: Iterable[str], which: str) -> set[str]:
    if which not in SETS:
        raise SetError(f'Unknown set {which!r}. Choose {" or ".join(SETS)}.')
    try:
        return disease_profiles.profile_codes(diseases)
    except disease_profiles.ScopeError as error:
        raise SetError(str(error)) from error


def disease_set(descriptors: list[dict], diseases: Iterable[str], which: str = SET_PROFILE) -> list[dict]:
    """The descriptors in the named diseases' extension, or in their profile.

    ``Other`` names no extension, so its profile is the base record and its
    extension is empty. Order is the descriptors' own.
    """
    return _cut(descriptors, _codes(diseases, which), which)


def _cut(descriptors: list[dict], codes: set[str], which: str) -> list[dict]:
    if which == SET_EXTENSION:
        return [d for d in descriptors if codes & set(d['diseases'])]
    return [d for d in descriptors if disease_profiles.in_profile(d['diseases'], codes)]


def _labels() -> dict[str, str]:
    """What a person calls each field: a created field's display name, else the column's."""
    from omop_core.models import CustomPatientField, PatientRecord

    labels = {
        field.name: str(field.verbose_name).capitalize()
        for field in PatientRecord._meta.get_fields() if getattr(field, 'concrete', False)
    }
    labels.update(CustomPatientField.objects.values_list('field_name', 'display_name'))
    return labels


def _cell(value) -> str:
    """One value as CSV text; text a spreadsheet would run as a formula is quoted.

    Field names and concept names are curated, but a display name or a
    HealthKey-minted concept name is whatever a curator typed.
    """
    if value is None:
        return ''
    text = str(value)
    return "'" + text if text[:1] in ('=', '+', '-', '@', '\t', '\r') else text


def rows(descriptors: list[dict]) -> list[dict[str, str]]:
    """One CSV row per field; a field with no concept keeps blank concept columns.

    Disease-specific fields come first, then the base record, each by tab.
    """
    from omop_core.services.field_mapping_provenance import portable_provenance

    labels = _labels()
    result = []
    for d in sorted(descriptors, key=lambda d: (
            d['scope'] != disease_profiles.SCOPE_DISEASE, d['tab'] or '', d['field_name'])):
        mapping = d.get('mapping') or {}
        result.append({name: _cell(value) for name, value in {
            'field_name': d['field_name'],
            'label': labels.get(d['field_name'], ''),
            'scope': d['scope'],
            'diseases': ' '.join(d['diseases']),
            'tab': d['tab'],
            'field_type': d['field_type'],
            'omop_table': mapping.get('omop_table') or '',
            'concept_id': mapping.get('concept_id') or '',
            'concept_name': mapping.get('concept_name') or '',
            'vocabulary_id': mapping.get('vocabulary_id') or '',
            'concept_code': mapping.get('concept_code') or '',
            'unit': mapping.get('unit') or '',
            'mapping_status': mapping.get('status') or '',
            'provenance': portable_provenance(mapping.get('provenance') or ''),
        }.items()})
    return result


def csv_export(diseases: Iterable[str], which: str = SET_PROFILE) -> tuple[str, str]:
    """``(filename, csv text)`` for the named diseases' set; no disease is the base record."""
    from omop_core.services.field_descriptor import get_all_field_descriptors

    codes = _codes([d for d in diseases if str(d or '').strip()], which)
    chosen = _cut(get_all_field_descriptors(), codes, which)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows(chosen))
    name = '-'.join(sorted(codes)) or 'base'
    return f'field-mappings-{name}-{which}-{localdate().isoformat()}.csv', buffer.getvalue()
