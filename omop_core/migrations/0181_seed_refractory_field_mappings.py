"""Make the refractoriness source concepts actually curatable (#785).

Migration 0180 minted the concepts. A concept on its own changes nothing the
curator or the editor can see:

  - ``FieldMappingPage`` buckets a field as "Mapped" on
    ``mapping?.status === 'approved'``, i.e. on a FieldConceptMapping row.
  - ``write_descriptor._curated_writes`` needs ``status='approved'`` AND a
    non-empty ``source_value`` AND a non-empty ``omop_table`` before the field
    becomes writable. Approved alone shows "Mapped" while the box stays
    read-only, which is the half-finished state FieldConceptMapping's docstring
    warns about.

So this seeds the mapping rows with all three, for the two fields where a
curated answer is a fact in its own right.

``ecog_assessment_date`` is deliberately left unmapped even though 0180 minted a
concept for it. It already carries LOINC 89247-1 — the same code as
``ecog_performance_status`` — and ``patient_record_service`` sets it from the
date of that same row, never from a distinct fact. ``mappings.py`` calls the two
"parts of one fact", and ``suggested_mappings.py`` excludes the identical
``pregnancy_test_date`` case because "a mapping would invite writing it
independently of the result it is supposed to date". Migration 0160 excluded
companion dates on the same grounds. The concept is harmless while nothing maps
to it; a mapping would produce a second row for one fact.

Concepts are resolved by ``(vocabulary_id, concept_code)`` rather than by the
literal ids 0180 declares. 0180 creates them with
``get_or_create(vocabulary_id=..., concept_code=...)``, so on a database that
already held one of these codes — ``regimen_resolution`` mints ``hko:`` codes at
runtime from the sequence — the pre-existing row wins and the declared id was
never applied. Keying on the code here means the mapping is correct either way;
a divergence is logged rather than silently mis-pointed.
"""
import logging

from django.db import migrations

logger = logging.getLogger(__name__)

_VOCABULARY_ID = 'HK-Observation'

# Type concept for a curated, human-entered clinical assertion.
_EHR_TYPE_CONCEPT_ID = 32817

# field_name, concept_code, concept_id as declared by migration 0180
_REFRACTORY_MAPPINGS = [
    ('btk_inhibitor_refractory', 'hko:btk-inhibitor-refractory', 2_100_007_852),
    ('bcl2_inhibitor_refractory', 'hko:bcl2-inhibitor-refractory', 2_100_007_851),
]


def seed_refractory_field_mappings(apps, schema_editor):
    Concept = apps.get_model('omop_core', 'Concept')
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')

    for field_name, concept_code, expected_id in _REFRACTORY_MAPPINGS:
        concept = Concept.objects.filter(
            vocabulary_id=_VOCABULARY_ID, concept_code=concept_code,
        ).first()
        if concept is None:
            # 0180 seeds these immediately before this migration, so absence
            # means someone has removed them. Skip rather than write a mapping
            # with no concept: write_descriptor would ignore it anyway, and a
            # dangling row is worse than none.
            logger.warning(
                'Concept %s:%s is missing; skipping its field mapping.',
                _VOCABULARY_ID, concept_code,
            )
            continue
        if concept.concept_id != expected_id:
            logger.warning(
                'Concept %s:%s has concept_id %s, not the %s migration 0180 '
                'declares. Mapping the row that exists.',
                _VOCABULARY_ID, concept_code, concept.concept_id, expected_id,
            )

        FieldConceptMapping.objects.update_or_create(
            field_name=field_name,
            defaults=dict(
                concept=concept,
                vocabulary_id=_VOCABULARY_ID,
                concept_code=concept_code,
                omop_table='observation',
                source_value=concept_code,
                value_kind='boolean',
                type_concept_id=_EHR_TYPE_CONCEPT_ID,
                status='approved',
                notes='Local mint (#785): no Athena code distinguishes which '
                      'drug class the disease is refractory to.',
            ),
        )


def unseed_refractory_field_mappings(apps, schema_editor):
    """Remove only the mapping rows, never the concepts.

    The concepts may already be referenced by observation rows written through
    these mappings; 0180's own reverse is a noop for the same reason.
    """
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    FieldConceptMapping.objects.filter(
        field_name__in=[f for f, _c, _i in _REFRACTORY_MAPPINGS],
        vocabulary_id=_VOCABULARY_ID,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [('omop_core', '0180_seed_clinical_field_source_concepts')]

    operations = [
        migrations.RunPython(
            seed_refractory_field_mappings, unseed_refractory_field_mappings),
    ]
