"""Lines of therapy per cancer (#1739).

1. ``episode_parent_id`` becomes a BigInteger, like ``episode_id``: the ids it
   holds no longer fit in 32 bits.
2. Every existing line of therapy (a Treatment Regimen Episode with no parent)
   is attached to a Disease Episode (32528) for its person's current primary
   cancer, and its LOT-{n}-outcome/intent/discontinuation Observations are
   linked to it. People without a primary cancer, or deployments without the
   Episode vocabulary yet, are left as they are: unparented lines keep meaning
   "the primary cancer", and ``manage.py attach_lines_to_diseases`` repeats
   this once the vocabulary is loaded.
"""
import re

from django.db import migrations, models

DISEASE_FIRST_OCCURRENCE = 32528
EHR_TYPE = 32817


def _slug(name):
    return re.sub(r'[^a-z0-9]+', '-', (name or '').lower()).strip('-')[:100]


def attach_lines(apps, schema_editor):
    from omop_core.services.pk import next_pk

    Concept = apps.get_model('omop_core', 'Concept')
    PatientRecord = apps.get_model('omop_core', 'PatientRecord')
    Observation = apps.get_model('omop_core', 'Observation')
    Episode = apps.get_model('omop_oncology', 'Episode')

    disease_concept = Concept.objects.filter(concept_id=DISEASE_FIRST_OCCURRENCE).first()
    if disease_concept is None:
        return
    object_concept = (Concept.objects.filter(concept_id=0).first()
                      or Concept.objects.filter(concept_id=EHR_TYPE).first() or disease_concept)
    type_concept = Concept.objects.filter(concept_id=EHR_TYPE).first() or disease_concept

    # A line is any Episode that is not a Disease Episode (as lines have always been read).
    orphans = Episode.objects.exclude(episode_concept_id=DISEASE_FIRST_OCCURRENCE).filter(episode_parent_id__isnull=True)
    for person_id in orphans.values_list('person_id', flat=True).distinct():
        record = PatientRecord.objects.filter(person_id=person_id).first()
        slug = (record.disease_slug or _slug(record.disease)) if record else ''
        if not slug:
            continue
        lines = list(orphans.filter(person_id=person_id))
        source = f'disease:{slug}'[:50]
        parent = Episode.objects.filter(
            person_id=person_id, episode_concept_id=DISEASE_FIRST_OCCURRENCE, episode_source_value=source,
        ).first()
        if parent is None:
            parent = Episode.objects.create(
                episode_id=next_pk(Episode, 'episode_id'),
                person_id=person_id,
                episode_concept=disease_concept,
                episode_object_concept=object_concept,
                episode_type_concept=type_concept,
                episode_start_date=min(line.episode_start_date for line in lines),
                episode_source_value=source,
            )
        for line in lines:
            line.episode_parent_id = parent.episode_id
            line.save(update_fields=['episode_parent_id'])
            if line.episode_number is not None:
                Observation.objects.filter(
                    person_id=person_id, observation_event_id__isnull=True,
                    observation_source_value__in=[
                        f'LOT-{line.episode_number}-{suffix}' for suffix in ('outcome', 'intent', 'discontinuation')
                    ],
                ).update(observation_event_id=line.episode_id)


class Migration(migrations.Migration):

    dependencies = [
        ('omop_oncology', '0006_add_episode_pk_sequence'),
        ('omop_core', '0281_merge_corrected_units_and_source_code_resolve'),
    ]

    operations = [
        migrations.AlterField(
            model_name='episode',
            name='episode_parent_id',
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(attach_lines, migrations.RunPython.noop),
    ]
