"""Turn each choice's primary coded association into a proposal (#1224).

``FieldChoiceCode.is_primary`` was the only place an answer's concept lived,
unreviewed, and neither unique nor guaranteed to resolve. It is carried over as
a **proposal**, never an approval: the code is looked up by exact vocabulary and
code, and what it found is recorded in ``source_resolution`` (``resolved``,
``unresolved`` when the concept is not loaded, ``invalid`` when it is retired,
``non_standard``). A proposal that did not resolve keeps its code as text and
has no target, so a curator sees the gap instead of a silent drop.

Only the first primary code of a choice is carried, by id; the codes
themselves stay where they are for source codings and interoperability, and
nothing that writes data reads the new rows yet. A choice that already has a
value mapping is left alone, so a replay changes nothing.
"""
from django.db import migrations

PROVENANCE = 'migration 0368'


def propose_from_primary_codes(apps, schema_editor):
    FieldChoiceCode = apps.get_model('omop_core', 'FieldChoiceCode')
    Mapping = apps.get_model('omop_core', 'FieldValueConceptMapping')
    Revision = apps.get_model('omop_core', 'FieldValueConceptMappingRevision')
    Concept = apps.get_model('omop_core', 'Concept')
    Vocabulary = apps.get_model('omop_core', 'Vocabulary')

    have = set(Mapping.objects.values_list('choice_id', flat=True))
    releases = dict(Vocabulary.objects.values_list('vocabulary_id', 'vocabulary_version'))
    seen = set()
    for code in FieldChoiceCode.objects.filter(is_primary=True).order_by('choice_id', 'id').iterator():
        if code.choice_id in have or code.choice_id in seen:
            continue
        seen.add(code.choice_id)
        concept = (Concept.objects
                   .filter(vocabulary_id=code.vocabulary_id, concept_code=code.code)
                   .order_by('concept_id').first())
        if concept is None:
            resolution, target = 'unresolved', None
        elif concept.invalid_reason:
            resolution, target = 'invalid', None
        elif concept.standard_concept != 'S':
            resolution, target = 'non_standard', None
        else:
            resolution, target = 'resolved', concept
        mapping = Mapping.objects.create(
            choice_id=code.choice_id, role='value_as_concept', outcome='mapped',
            status='proposed', target_concept=target,
            vocabulary_id=code.vocabulary_id, concept_code=code.code,
            source_resolution=resolution, provenance=PROVENANCE,
            vocabulary_release=(releases.get(code.vocabulary_id) or '') if target else '',
            rationale='Carried over from the choice\'s primary coded association; not reviewed.',
        )
        Revision.objects.create(
            mapping=mapping, revision=1, action='migrated', status='proposed',
            outcome='mapped', role='value_as_concept',
            concept_id=target.pk if target else None,
            vocabulary_id=code.vocabulary_id, concept_code=code.code,
            rationale=mapping.rationale, vocabulary_release=mapping.vocabulary_release,
            changed_by_label=PROVENANCE,
        )


def remove_migrated_proposals(apps, schema_editor):
    """Only what this migration wrote and no one has acted on since."""
    Mapping = apps.get_model('omop_core', 'FieldValueConceptMapping')
    Mapping.objects.filter(provenance=PROVENANCE, status='proposed', revision=1).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0367_field_value_concept_mapping'),
    ]

    operations = [
        migrations.RunPython(propose_from_primary_codes, remove_migrated_proposals),
    ]
