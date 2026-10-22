"""Seed approved FieldConceptMappings for PR and HER2 status fields (#847).

Companion to 0196 (estrogen_receptor_status). The same _receptor_status()
derivation function was updated to return LOINC answer display names for all
three receptor fields, but only ER had its vocabulary and mapping migrated.
This migration completes PR and HER2.

1. Seeds approved FieldConceptMappings:
   - progesterone_receptor_status → LOINC 16113-3
   - her2_status → LOINC 48676-1
2. Updates ProgesteroneReceptorStatus and Her2Status vocabulary tables from
   custom slugs to LOINC answer list LL2160-7 codes.
3. Remaps existing PatientRecord values to canonical LOINC answer display names.
"""
import logging

from django.db import migrations

logger = logging.getLogger(__name__)

_LAB_TYPE_CONCEPT_ID = 32856

# LOINC answer list LL2160-7 — shared by ER, PR, HER2 interpretation fields.
_LOINC_ANSWERS = [
    ('LA6576-8', 'Positive'),
    ('LA6577-6', 'Negative'),
    ('LA11884-6', 'Equivocal'),
]

_PR_LOINC = ('16113-3', 'LOINC')
_HER2_LOINC = ('48676-1', 'LOINC')

# Old PR values → canonical
_PR_VALUE_REMAP = {
    'POSITIVE': 'Positive',
    'NEGATIVE': 'Negative',
    'EQUIVOCAL': 'Equivocal',
    'PR+': 'Positive',
    'PR-': 'Negative',
    'PR+ with low expression': 'Positive',
    'PR+ with high expression': 'Positive',
    'Positive': 'Positive',
    'Negative': 'Negative',
    'Equivocal': 'Equivocal',
}

# Old HER2 values → canonical
_HER2_VALUE_REMAP = {
    'POSITIVE': 'Positive',
    'NEGATIVE': 'Negative',
    'EQUIVOCAL': 'Equivocal',
    'HER2+': 'Positive',
    'HER2-': 'Negative',
    'HER2 low': 'Equivocal',
    'INDETERMINATE': 'Equivocal',
    'Positive': 'Positive',
    'Negative': 'Negative',
    'Equivocal': 'Equivocal',
    'Indeterminate': 'Equivocal',
}

_OLD_PR_VOCAB = [
    ('pr_minus', 'PR-'),
    ('pr_plus', 'PR+'),
    ('pr_plus_with_low_exp', 'PR+ with low expression'),
    ('pr_plus_with_hi_exp', 'PR+ with high expression'),
]

_OLD_HER2_VOCAB = [
    ('her2_plus', 'HER2+'),
    ('her2_minus', 'HER2-'),
    ('her2_low', 'HER2 low'),
]


def _seed_mapping(apps, field_name, concept_code, vocab_id, value_vocabulary, notes):
    Concept = apps.get_model('omop_core', 'Concept')
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')

    concept = Concept.objects.filter(
        vocabulary_id=vocab_id, concept_code=concept_code,
    ).first()
    if concept is None:
        logger.warning(
            'LOINC concept %s not found; skipping FieldConceptMapping for %s.',
            concept_code, field_name,
        )
        return

    FieldConceptMapping.objects.update_or_create(
        field_name=field_name,
        defaults=dict(
            concept=concept,
            vocabulary_id=vocab_id,
            concept_code=concept_code,
            omop_table='measurement',
            source_value=concept_code,
            value_kind='string',
            type_concept_id=_LAB_TYPE_CONCEPT_ID,
            value_vocabulary=value_vocabulary,
            multiple=False,
            status='approved',
            notes=notes,
        ),
    )


def seed_pr_her2_mappings(apps, schema_editor):
    _seed_mapping(
        apps, 'progesterone_receptor_status', *_PR_LOINC,
        'ProgesteroneReceptorStatus',
        'LOINC 16113-3 "Progesterone receptor [Interpretation] in Tissue '
        'by Immune stain". Seeded by migration 0197 (#847).',
    )
    _seed_mapping(
        apps, 'her2_status', *_HER2_LOINC,
        'Her2Status',
        'LOINC 48676-1 "HER2 [Interpretation] in Tissue by Immune stain". '
        'Seeded by migration 0197 (#847).',
    )


def unseed_pr_her2_mappings(apps, schema_editor):
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    FieldConceptMapping.objects.filter(
        field_name__in=['progesterone_receptor_status', 'her2_status'],
    ).delete()


def _remap_field(apps, field_name, value_map):
    PatientRecord = apps.get_model('omop_core', 'PatientRecord')
    records = PatientRecord.objects.exclude(
        **{f'{field_name}__isnull': True},
    ).exclude(**{field_name: ''})

    to_update = []
    for record in records.iterator():
        old_val = getattr(record, field_name)
        new_val = value_map.get(old_val)
        if new_val is None:
            logger.warning(
                'PatientRecord %s has unrecognized %s "%s"; preserving as-is.',
                record.pk, field_name, old_val,
            )
            continue
        if old_val != new_val:
            setattr(record, field_name, new_val)
            to_update.append(record)

    if to_update:
        PatientRecord.objects.bulk_update(to_update, [field_name])
        logger.info(
            'Remapped %d PatientRecord.%s values to LOINC answer display names.',
            len(to_update), field_name,
        )


def remap_pr_her2_values(apps, schema_editor):
    _remap_field(apps, 'progesterone_receptor_status', _PR_VALUE_REMAP)
    _remap_field(apps, 'her2_status', _HER2_VALUE_REMAP)


def reverse_remap(apps, schema_editor):
    # Original values cannot be recovered after remapping.
    pass


def _update_vocab(apps, model_name, new_entries):
    Model = apps.get_model('omop_core', model_name)
    new_codes = {code for code, _ in new_entries}
    old_rows = Model.objects.exclude(code__in=new_codes)
    if old_rows.exists():
        logger.info(
            'Removing %d old %s entries: %s',
            old_rows.count(), model_name,
            list(old_rows.values_list('code', flat=True)),
        )
        old_rows.delete()
    for code, title in new_entries:
        Model.objects.update_or_create(code=code, defaults={'title': title})


def update_pr_her2_vocabularies(apps, schema_editor):
    _update_vocab(apps, 'ProgesteroneReceptorStatus', _LOINC_ANSWERS)
    _update_vocab(apps, 'Her2Status', _LOINC_ANSWERS)


def restore_pr_her2_vocabularies(apps, schema_editor):
    # Remove LOINC entries and restore old ones.
    _update_vocab(apps, 'ProgesteroneReceptorStatus', _OLD_PR_VOCAB)
    _update_vocab(apps, 'Her2Status', _OLD_HER2_VOCAB)


class Migration(migrations.Migration):

    dependencies = [('omop_core', '0196_seed_er_status_loinc_mapping')]

    operations = [
        # 1. Seed FieldConceptMappings.
        migrations.RunPython(seed_pr_her2_mappings, unseed_pr_her2_mappings),
        # 2. Remap existing PatientRecord values.
        migrations.RunPython(remap_pr_her2_values, reverse_remap),
        # 3. Update vocabulary tables.
        migrations.RunPython(update_pr_her2_vocabularies, restore_pr_her2_vocabularies),
    ]
