"""PatientRecord.stage keeps a patient's stage, not a SNOMED semantic tag (#2618).

The derivation used to copy a staging concept's display verbatim, so 102 rows in
the merge dump said "Stage 4 (qualifier value)" and the record page, trial
matching and the download report showed it. The derivation now normalises
(omop_core/services/stage_labels.py); this clears what is already stored.

The mapping is repeated here rather than imported: a migration must keep
producing the same result after the live module changes.
"""

import logging
import re

from django.db import migrations

logger = logging.getLogger(__name__)

_MAPPING = {'0': '0', '1': 'I', '2': 'II', '3': 'III', '4': 'IV',
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
_STAGE_PREFIX = re.compile(
    r'^\s*(?:american joint committee on cancer\s+)?stage\s*'
    r'(?P<number>iv|iii|ii|i|0|[1-4])\s*(?P<suffix>[abc])?$',
    re.IGNORECASE,
)


def _normalise(value):
    if not isinstance(value, str):
        return value
    text = value.strip()
    while True:
        match = _TRAILING_PAREN.search(text)
        if not match or match.group(1).strip().lower() not in _TAGS:
            break
        text = text[:match.start()].strip()
    text = re.sub(r'\s*\([^)]*$', '', text).strip()
    stage = _STAGE_PREFIX.match(text)
    if stage:
        suffix = (stage.group('suffix') or '').upper()
        return f"Stage {_MAPPING[stage.group('number').lower()]}{suffix}"
    return text


def normalise_stages(apps, schema_editor):
    PatientRecord = apps.get_model('omop_core', 'PatientRecord')
    changed = []
    untouched = []
    for record in PatientRecord.objects.exclude(stage__isnull=True).exclude(stage='').only('pk', 'stage'):
        normalised = _normalise(record.stage)
        if normalised != record.stage:
            record.stage = normalised
            changed.append(record)
        elif '(' in record.stage:
            # A value this migration could not read is logged, never silently
            # kept: the repository's data-migration rule.
            untouched.append(record.stage)
    if changed:
        PatientRecord.objects.bulk_update(changed, ['stage'], batch_size=500)
    # WARNING, not INFO: the deploy log only surfaces WARNING for this module,
    # so an INFO line would make a bulk rewrite invisible.
    logger.warning('0345_normalise_stage_labels: normalised %s stage values', len(changed))
    if untouched:
        logger.warning(
            '0345_normalise_stage_labels: left %s stage values unchanged: %s',
            len(untouched), sorted(set(untouched))[:20],
        )


class Migration(migrations.Migration):

    dependencies = [
        # Two migrations were merged with the number 0344; this one chains
        # after the unit-marker cleanup so the graph has a single leaf.
        ('omop_core', '0344_drop_placeholder_units'),
    ]

    operations = [
        # The original display is a vocabulary artefact, not a clinical fact, so
        # restoring it on rollback would put the tag back on 102 records.
        migrations.RunPython(normalise_stages, migrations.RunPython.noop),
    ]
