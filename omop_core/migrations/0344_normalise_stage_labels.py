"""PatientRecord.stage keeps a patient's stage, not a SNOMED semantic tag (#2618).

The derivation used to copy a staging concept's display verbatim, so 102 rows in
the merge dump said "Stage 4 (qualifier value)" and the record page, trial
matching and the download report showed it. The derivation now normalises
(omop_core/services/stage_labels.py); this clears what is already stored.

The mapping is repeated here rather than imported: a migration must keep
producing the same result after the live module changes.
"""

import re

from django.db import migrations

_MAPPING = {'1': 'I', '2': 'II', '3': 'III', '4': 'IV',
            'i': 'I', 'ii': 'II', 'iii': 'III', 'iv': 'IV'}
_TAGS = {
    'qualifier value', 'finding', 'disorder', 'situation', 'event',
    'observable entity', 'procedure', 'body structure',
    'morphologic abnormality', 'clinical finding', 'social context',
    'physical object', 'organism', 'substance', 'specimen',
    'pharmaceutical / biologic product', 'record artifact',
    'environment or geographical location', 'qualifier',
}
_TRAILING_PAREN = re.compile(r'\s*\(([^()]*)\)\s*$')
_FULL_STAGE = re.compile(r'stage\s*(1|2|3|4|i{1,3}|iv)\b', re.IGNORECASE)
_PARTIAL_STAGE = re.compile(r'stage\s*([1-4])\s*([abc])\b', re.IGNORECASE)


def _normalise(value):
    if not isinstance(value, str):
        return value
    text = value.strip()
    match = _TRAILING_PAREN.search(text)
    if match and match.group(1).strip().lower() in _TAGS:
        text = text[:match.start()].strip()
    full = _FULL_STAGE.fullmatch(text)
    if full:
        return f"Stage {_MAPPING[full.group(1).lower()]}"
    partial = _PARTIAL_STAGE.fullmatch(text)
    if partial:
        return f"Stage {_MAPPING[partial.group(1)]}{partial.group(2).upper()}"
    return text


def normalise_stages(apps, schema_editor):
    PatientRecord = apps.get_model('omop_core', 'PatientRecord')
    changed = []
    for record in PatientRecord.objects.exclude(stage__isnull=True).exclude(stage='').only('pk', 'stage'):
        normalised = _normalise(record.stage)
        if normalised != record.stage:
            record.stage = normalised
            changed.append(record)
    if changed:
        PatientRecord.objects.bulk_update(changed, ['stage'], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0343_merge_dev_sync_20261114'),
    ]

    operations = [
        # The original display is a vocabulary artefact, not a clinical fact, so
        # restoring it on rollback would put the tag back on 102 records.
        migrations.RunPython(normalise_stages, migrations.RunPython.noop),
    ]
