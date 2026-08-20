"""
Data migration: convert smoking status observations from old SNOMED format
(observation_concept_id = SNOMED code) to OMOP question/answer format
(observation_concept_id = LOINC 72166-2, value_as_concept_id = LOINC answer).

See issue #451.
"""
import logging

from django.db import migrations

logger = logging.getLogger(__name__)

# Old SNOMED concept_code -> new LOINC answer concept_code
_SNOMED_TO_LOINC_ANSWER = {
    '266919005': 'LA18978-9',   # Never smoked tobacco -> Never smoker
    '8517006':   'LA15920-4',   # Ex-smoker -> Former smoker
    '77176002':  'LA18976-3',   # Smoker -> Current every day smoker
}


def _convert_tobacco_observations(apps, schema_editor):
    Concept = apps.get_model('omop_core', 'Concept')
    Observation = apps.get_model('omop_core', 'Observation')

    # Look up the question concept: LOINC 72166-2
    question_concept = Concept.objects.filter(
        vocabulary_id='LOINC', concept_code='72166-2'
    ).order_by('concept_id').first()
    if question_concept is None:
        logger.warning(
            'LOINC 72166-2 concept not found — skipping tobacco migration. '
            'Run seed_omop_concepts first.'
        )
        return

    # Look up the answer concepts
    answer_concepts = {}
    for snomed_code, loinc_answer_code in _SNOMED_TO_LOINC_ANSWER.items():
        answer = Concept.objects.filter(
            vocabulary_id='LOINC', concept_code=loinc_answer_code
        ).order_by('concept_id').first()
        if answer is None:
            logger.warning(
                'LOINC answer concept %s not found — rows with SNOMED %s '
                'will not be converted. Run seed_omop_concepts first.',
                loinc_answer_code, snomed_code,
            )
        else:
            answer_concepts[snomed_code] = answer

    if not answer_concepts:
        logger.warning('No answer concepts found — nothing to migrate.')
        return

    # Find old-format rows: observation_concept is one of the three SNOMED codes
    old_rows = Observation.objects.filter(
        observation_concept__concept_code__in=list(_SNOMED_TO_LOINC_ANSWER.keys()),
        observation_concept__vocabulary_id='SNOMED',
    ).select_related('observation_concept')

    updated = []
    for obs in old_rows:
        snomed_code = obs.observation_concept.concept_code
        answer = answer_concepts.get(snomed_code)
        if answer is None:
            continue
        obs.observation_concept_id = question_concept.concept_id
        obs.value_as_concept_id = answer.concept_id
        updated.append(obs)

    if updated:
        Observation.objects.bulk_update(
            updated,
            ['observation_concept_id', 'value_as_concept_id'],
            batch_size=500,
        )
        logger.info('Converted %d tobacco observation(s) to question/answer format.', len(updated))
    else:
        logger.info('No old-format tobacco observations found to convert.')


def _reverse_noop(apps, schema_editor):
    # The original SNOMED concept_ids cannot be recovered per-row after
    # remapping (we don't know which answer mapped from which SNOMED code
    # without storing that). Rolling back leaves rows in question/answer
    # format. Manual correction is required if reversal is needed.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0146_patientrecord_patientrecord_lat_lon_both_or_neither_and_more'),
    ]

    operations = [
        migrations.RunPython(
            _convert_tobacco_observations,
            _reverse_noop,
        ),
    ]
