"""Seed the reviewed concept suggestions as proposed mappings.

These are suggestions, not decisions: every row lands as ``proposed``, which the
descriptor ignores, so none of them makes a field writable. A reviewer approves
or rejects each one in the mapping interface, and that approval is what turns a
read-only box into a typeable one.

Seeded by migration so every environment starts from the same shortlist rather
than whichever developer happened to run the command.

The choices themselves live in ``omop_core/suggested_mappings.py`` with the
reasoning for each, including why the top lexical match was rejected in the cases
where it was wrong.
"""
from django.db import migrations


def seed(apps, schema_editor):
    from omop_core.suggested_mappings import REVIEWED_SUGGESTIONS

    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    Concept = apps.get_model('omop_core', 'Concept')

    for field_name, choice in REVIEWED_SUGGESTIONS.items():
        concept = Concept.objects.filter(
            vocabulary_id=choice['vocabulary_id'],
            concept_code=choice['concept_code'],
        ).first()
        if concept is None:
            # Vocabularies load separately. A suggestion pointing at a concept
            # this database does not have would look reviewable and resolve to
            # nothing, so it is skipped until the load happens.
            continue

        # A reviewer's decision outranks a suggestion. Only rows nobody has
        # judged are (re)written.
        existing = FieldConceptMapping.objects.filter(field_name=field_name).first()
        if existing is not None and existing.status != 'proposed':
            continue

        FieldConceptMapping.objects.update_or_create(
            field_name=field_name,
            defaults={
                'concept': concept,
                'vocabulary_id': concept.vocabulary_id,
                'concept_code': concept.concept_code,
                'omop_table': choice.get('omop_table', 'observation'),
                'value_kind': choice.get('value_kind', ''),
                # Left blank on purpose: derivation matches on source_value, and
                # guessing it is how a mapping becomes a write into a void. The
                # reviewer sets it against whatever the extractor reads.
                'source_value': '',
                'status': 'proposed',
                'notes': (
                    'SUGGESTED, NOT REVIEWED BY A CLINICIAN. Chosen for meaning '
                    f'rather than spelling: {choice["rationale"]} '
                    'Set source_value before approving; derivation matches on it.'
                ),
            },
        )


def unseed(apps, schema_editor):
    from omop_core.suggested_mappings import REVIEWED_SUGGESTIONS

    # Only the ones still untouched — an approved or rejected row records a
    # decision this migration has no business undoing.
    apps.get_model('omop_core', 'FieldConceptMapping').objects.filter(
        field_name__in=list(REVIEWED_SUGGESTIONS), status='proposed',
    ).delete()


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0157_seed_employment_status_mapping')]
    operations = [migrations.RunPython(seed, unseed)]
