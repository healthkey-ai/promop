"""Record the SCT fields as approved concept mappings.

The three stem-cell-transplant fields have always derived from dated
Observations keyed on ``mm-sct-date``, ``mm-sct-history`` and
``mm-sct-eligibility`` — the FHIR upload has written them that way since PR
#115. What never existed was a mapping row saying so, and without one the
writable-field descriptor had nothing to act on: the editor fell back to
patching the projection, which refuses OMOP-mapped columns. Three documented,
validated fields could not be saved from the UI at all.

Seeding them here rather than hardcoding the recipe keeps one source of truth.
A curator can now see these alongside every other mapping, and correct them
without a code change.

The concept is the EHR type concept rather than a clinical one, matching what
the upload path writes: these facts carry their meaning in
``observation_source_value`` and ``value_as_string``. Matching it is what makes
an edit and an import indistinguishable to derivation.
"""
from django.db import migrations

# field_name, source_value, value_kind, value_vocabulary, multiple
_SCT_MAPPINGS = [
    ('sct_date', 'mm-sct-date', 'date', '', False),
    ('stem_cell_transplant_history', 'mm-sct-history', 'string',
     'StemCellTransplant', True),
    ('sct_eligibility', 'mm-sct-eligibility', 'string', 'SctEligibility', True),
]

_EHR_TYPE_CONCEPT_ID = 32817


def seed(apps, schema_editor):
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    Concept = apps.get_model('omop_core', 'Concept')

    concept = Concept.objects.filter(concept_id=_EHR_TYPE_CONCEPT_ID).first()
    if concept is None:
        # Vocabularies are loaded separately, and a database without them is a
        # valid state — the descriptor simply reports these as unmapped until
        # the load happens. Seeding a mapping with no concept would be worse:
        # it would look curated and write nothing.
        return

    for field_name, source_value, value_kind, vocabulary, multiple in _SCT_MAPPINGS:
        FieldConceptMapping.objects.update_or_create(
            field_name=field_name,
            defaults={
                'concept': concept,
                # Deliberately no code. The unique constraint on
                # (vocabulary_id, concept_code) exists because a code should
                # identify one field, and it is right to refuse three fields
                # claiming the same one. These facts are not identified by a
                # code at all -- they are identified by source_value, and the
                # concept here is a type placeholder, the same one the upload
                # writes. Leaving the code blank says that rather than working
                # around the constraint.
                'vocabulary_id': '',
                'concept_code': '',
                'omop_table': 'observation',
                'source_value': source_value,
                'value_kind': value_kind,
                'type_concept_id': _EHR_TYPE_CONCEPT_ID,
                'value_vocabulary': vocabulary,
                'multiple': multiple,
                'status': 'approved',
                'notes': (
                    'Seeded by migration 0156. These fields already derived from '
                    'dated Observations on this source value; the mapping is what '
                    'lets an editor write them.'
                ),
            },
        )


def unseed(apps, schema_editor):
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    FieldConceptMapping.objects.filter(
        field_name__in=[m[0] for m in _SCT_MAPPINGS],
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0155_field_concept_mapping_write_details'),
    ]

    operations = [migrations.RunPython(seed, unseed)]
