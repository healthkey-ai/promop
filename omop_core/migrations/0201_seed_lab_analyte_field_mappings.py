"""Make eight Blood/Labs analytes writable (#957).

The tabs already render input boxes for troponin, BNP, glucose, HbA1c, LDH,
CEA, CA 19-9 and PSA, and ``LAB_FIELD_TO_LOINC`` already carries the code, unit
and display name that derivation reads them back by. What was missing is the
``FieldConceptMapping`` row, and without it ``write_descriptor`` emits no
editable entry — so every one of those boxes was read-only.

That is the state ``FieldConceptMapping``'s own docstring warns about: a field
whose OMOP meaning is known everywhere except the one table that makes it
typeable. Re-organising which tab they sit on (#955, #956) does not help while
they cannot be written at all.

Each row carries all four parts a write needs — concept, ``omop_table``,
``source_value`` and ``value_kind``. ``source_value`` is the LOINC code, matching
how the already-writable lab fields key theirs (``smoking_status`` uses
``72166-2``); without it derivation cannot find the row it just wrote and the
mapping stays advisory.
"""
import logging

from django.db import migrations

logger = logging.getLogger(__name__)

_VOCABULARY_ID = 'LOINC'
_LAB_TYPE_CONCEPT_ID = 32856     # "Lab" measurement type

# (field_name, LOINC code, unit). Codes and units are copied from
# LAB_FIELD_TO_LOINC rather than re-derived, so the write key and the read key
# cannot drift apart.
_ANALYTES = (
    ('troponin_ng_ml', '10839-9', 'ng/mL'),
    ('bnp_pg_ml', '42637-9', 'pg/mL'),
    ('glucose_mg_dl', '2345-7', 'mg/dL'),
    ('hba1c_percent', '4548-4', '%'),
    ('ldh_u_l', '2532-0', 'U/L'),
    ('cea_ng_ml', '2039-6', 'ng/mL'),
    ('ca19_9_u_ml', '25390-6', 'U/mL'),
    ('psa_ng_ml', '2857-1', 'ng/mL'),
)


def seed_lab_analyte_mappings(apps, schema_editor):
    Concept = apps.get_model('omop_core', 'Concept')
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')

    for field_name, concept_code, unit in _ANALYTES:
        concept = Concept.objects.filter(
            vocabulary_id=_VOCABULARY_ID, concept_code=concept_code,
        ).first()
        if concept is None:
            # A deployment without the LOINC vocabulary loaded. Skip rather than
            # write a mapping with no concept: write_descriptor would ignore it
            # anyway, and a dangling row is worse than none. Re-run this
            # migration's logic via the admin once the vocabulary lands.
            logger.warning(
                'Concept %s:%s is not loaded; %s stays read-only.',
                _VOCABULARY_ID, concept_code, field_name,
            )
            continue

        # The approved-row uniqueness constraint is on (omop_table,
        # source_value), so a code already claimed by another approved field
        # would make this one collide. Report it rather than fail the deploy.
        clash = (
            FieldConceptMapping.objects
            .filter(omop_table='measurement', source_value=concept_code,
                    status='approved')
            .exclude(field_name=field_name)
            .first()
        )
        if clash is not None:
            logger.warning(
                'measurement/%s is already claimed by %s; %s stays read-only.',
                concept_code, clash.field_name, field_name,
            )
            continue

        FieldConceptMapping.objects.update_or_create(
            field_name=field_name,
            defaults=dict(
                concept=concept,
                vocabulary_id=_VOCABULARY_ID,
                concept_code=concept_code,
                unit=unit,
                omop_table='measurement',
                source_value=concept_code,
                value_kind='number',
                type_concept_id=_LAB_TYPE_CONCEPT_ID,
                status='approved',
                notes='Seeded from LAB_FIELD_TO_LOINC (#957): derivation '
                      'already read these back; only the write path was missing.',
            ),
        )


def unseed_lab_analyte_mappings(apps, schema_editor):
    """Remove the mapping rows, never the concepts.

    The LOINC concepts are licensed vocabulary content this migration did not
    create. Measurements already written through these mappings are left alone:
    they are patient data, and derivation reads them via LAB_FIELD_TO_LOINC
    regardless of whether the mapping row exists.
    """
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    FieldConceptMapping.objects.filter(
        field_name__in=[name for name, _code, _unit in _ANALYTES],
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0200_merge_20260901_0000'),
    ]

    operations = [
        migrations.RunPython(seed_lab_analyte_mappings, unseed_lab_analyte_mappings),
    ]
