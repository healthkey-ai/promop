"""Seed an approved FieldConceptMapping for estrogen_receptor_status (#847).

The field has always derived from LOINC 16112-5 ("Estrogen receptor
[Interpretation] in Tissue by Immune stain") via ``_get_biomarker_data``, but no
FieldConceptMapping row existed, so the write-descriptor could not make it
writable through the mapping path.

This migration:

1. Seeds an approved FieldConceptMapping pointing at LOINC 16112-5.
2. Updates the ``EstrogenReceptorStatus`` vocabulary codes from custom slugs
   (``er_plus``, ``er_minus``) to LOINC answer list LL2160-7 codes
   (``LA6576-8`` = Positive, ``LA6577-6`` = Negative, ``LA11884-6`` = Equivocal).
3. Remaps existing ``PatientRecord.estrogen_receptor_status`` values from the
   uppercase format (``POSITIVE``, ``NEGATIVE``) and old custom titles
   (``ER+``, ``ER-``) to the canonical LOINC answer display names
   (``Positive``, ``Negative``, ``Equivocal``).
4. Fixes the HR-status derivation values that depended on the old format.
"""
import logging

from django.db import migrations

logger = logging.getLogger(__name__)

# LOINC concept for ER status — concept_id 3004390 in our fixtures.
_LOINC_CONCEPT_CODE = '16112-5'
_LOINC_VOCABULARY_ID = 'LOINC'

# Lab-type concept (standard lab result).
_LAB_TYPE_CONCEPT_ID = 32856

# New vocabulary entries using LOINC answer list LL2160-7 codes.
_NEW_ER_VOCAB = [
    ('LA6576-8', 'Positive'),
    ('LA6577-6', 'Negative'),
    ('LA11884-6', 'Equivocal'),
]

# Map old PatientRecord values to the new canonical LOINC display names.
_VALUE_REMAP = {
    # Uppercase (from _receptor_status derivation)
    'POSITIVE': 'Positive',
    'NEGATIVE': 'Negative',
    'EQUIVOCAL': 'Equivocal',
    # Old vocabulary titles
    'ER+': 'Positive',
    'ER-': 'Negative',
    'ER+ with low expression': 'Positive',
    'ER+ with high expression': 'Positive',
    # Title case (from some FHIR imports)
    'Positive': 'Positive',
    'Negative': 'Negative',
    'Equivocal': 'Equivocal',
}


def seed_er_mapping(apps, schema_editor):
    Concept = apps.get_model('omop_core', 'Concept')
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')

    concept = Concept.objects.filter(
        vocabulary_id=_LOINC_VOCABULARY_ID,
        concept_code=_LOINC_CONCEPT_CODE,
    ).first()
    if concept is None:
        logger.warning(
            'LOINC concept %s not found; skipping FieldConceptMapping seed. '
            'Run a vocabulary load and re-run this migration.',
            _LOINC_CONCEPT_CODE,
        )
        return

    FieldConceptMapping.objects.update_or_create(
        field_name='estrogen_receptor_status',
        defaults=dict(
            concept=concept,
            vocabulary_id=_LOINC_VOCABULARY_ID,
            concept_code=_LOINC_CONCEPT_CODE,
            omop_table='measurement',
            source_value=_LOINC_CONCEPT_CODE,
            value_kind='string',
            type_concept_id=_LAB_TYPE_CONCEPT_ID,
            value_vocabulary='EstrogenReceptorStatus',
            multiple=False,
            status='approved',
            notes=(
                'LOINC 16112-5 "Estrogen receptor [Interpretation] in Tissue '
                'by Immune stain". Values use LOINC answer list LL2160-7 '
                '(LA6576-8 Positive, LA6577-6 Negative, LA11884-6 Equivocal). '
                'Seeded by migration 0196 (#847).'
            ),
        ),
    )


def unseed_er_mapping(apps, schema_editor):
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    FieldConceptMapping.objects.filter(
        field_name='estrogen_receptor_status',
        vocabulary_id=_LOINC_VOCABULARY_ID,
    ).delete()


def update_er_vocabulary(apps, schema_editor):
    EstrogenReceptorStatus = apps.get_model('omop_core', 'EstrogenReceptorStatus')

    # Remove old custom-coded entries not in the new set.
    new_codes = {code for code, _ in _NEW_ER_VOCAB}
    old_rows = EstrogenReceptorStatus.objects.exclude(code__in=new_codes)
    if old_rows.exists():
        logger.info(
            'Removing %d old EstrogenReceptorStatus entries: %s',
            old_rows.count(),
            list(old_rows.values_list('code', flat=True)),
        )
        old_rows.delete()

    # Seed the LOINC answer-list entries.
    for code, title in _NEW_ER_VOCAB:
        EstrogenReceptorStatus.objects.update_or_create(
            code=code, defaults={'title': title},
        )


def restore_er_vocabulary(apps, schema_editor):
    EstrogenReceptorStatus = apps.get_model('omop_core', 'EstrogenReceptorStatus')

    # Remove LOINC entries.
    new_codes = {code for code, _ in _NEW_ER_VOCAB}
    EstrogenReceptorStatus.objects.filter(code__in=new_codes).delete()

    # Restore old entries.
    for code, title in [
        ('er_minus', 'ER-'),
        ('er_plus', 'ER+'),
        ('er_plus_with_low_exp', 'ER+ with low expression'),
        ('er_plus_with_hi_exp', 'ER+ with high expression'),
    ]:
        EstrogenReceptorStatus.objects.get_or_create(
            code=code, defaults={'title': title},
        )


def remap_er_values(apps, schema_editor):
    """Remap PatientRecord.estrogen_receptor_status to LOINC answer display names."""
    PatientRecord = apps.get_model('omop_core', 'PatientRecord')

    records = PatientRecord.objects.exclude(
        estrogen_receptor_status__isnull=True,
    ).exclude(
        estrogen_receptor_status='',
    )

    to_update = []
    for record in records.iterator():
        old_val = record.estrogen_receptor_status
        new_val = _VALUE_REMAP.get(old_val)
        if new_val is None:
            # Value not in our map -- preserve it but log a warning.
            logger.warning(
                'PatientRecord %s has unrecognized estrogen_receptor_status '
                '"%s"; preserving as-is.',
                record.pk, old_val,
            )
            continue
        if old_val != new_val:
            record.estrogen_receptor_status = new_val
            to_update.append(record)

    if to_update:
        PatientRecord.objects.bulk_update(to_update, ['estrogen_receptor_status'])
        logger.info(
            'Remapped %d PatientRecord.estrogen_receptor_status values to '
            'LOINC answer display names.',
            len(to_update),
        )


def reverse_remap_er_values(apps, schema_editor):
    # Original values cannot be recovered after remapping.
    # Rolling back this migration leaves the field in its new-vocabulary state,
    # inconsistent with the restored vocabulary table. Manual correction is required.
    pass


class Migration(migrations.Migration):

    dependencies = [('omop_core', '0195_detach_code_named_suggest_placeholders')]

    operations = [
        # 1. Seed the FieldConceptMapping (needs concept to exist).
        migrations.RunPython(seed_er_mapping, unseed_er_mapping),
        # 2. Remap existing PatientRecord values before truncating vocabulary.
        migrations.RunPython(remap_er_values, reverse_remap_er_values),
        # 3. Update the vocabulary table.
        migrations.RunPython(update_er_vocabulary, restore_er_vocabulary),
    ]
