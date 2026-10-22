"""Remove the fabricated LANGUAGE vocabulary and its mints (#812).

manage_language_skills used to create ten concepts at concept_id
40000001-40000010 in a 'LANGUAGE' vocabulary, flagged standard_concept='S'.
Every part of that was wrong: the ids sit inside the range OHDSI assigns rather
than the local block at >= 2_000_000_000, only OHDSI assigns 'S', and SNOMED
already carries 878 Language-domain concepts so nothing needed minting. The
command no longer creates them.

No database has any: staging holds 0, and production is loaded from a staging
dump. This exists so that a database which did run --create-sample-concepts is
cleaned rather than left carrying rows that look standard to every consumer.

Deliberately conservative. Concepts still referenced by a person's language
skills are left in place and logged: PersonLanguageSkill.language_concept is
PROTECT, so deleting one would raise mid-migration, and a half-applied cleanup
is worse than a logged leftover. Since #810 the Language-domain trigger rejects
new rows against these concepts, so the referenced set can only shrink.
"""
import logging

from django.db import migrations

logger = logging.getLogger(__name__)

_VOCABULARY_ID = 'LANGUAGE'


def drop_fabricated_vocabulary(apps, schema_editor):
    Concept = apps.get_model('omop_core', 'Concept')
    Vocabulary = apps.get_model('omop_core', 'Vocabulary')
    PersonLanguageSkill = apps.get_model('omop_core', 'PersonLanguageSkill')

    minted = Concept.objects.filter(vocabulary_id=_VOCABULARY_ID)
    if not minted.exists():
        return

    referenced = set(
        PersonLanguageSkill.objects
        .filter(language_concept__vocabulary_id=_VOCABULARY_ID)
        .values_list('language_concept_id', flat=True)
    )
    if referenced:
        logger.warning(
            'Leaving %d LANGUAGE concepts in place: still referenced by '
            'person_language_skill rows. Repoint those rows at SNOMED '
            'Language-domain concepts, then re-run this cleanup.',
            len(referenced))

    removable = minted.exclude(concept_id__in=referenced)
    count = removable.count()
    removable.delete()
    logger.warning('Removed %d fabricated LANGUAGE concepts.', count)

    if not Concept.objects.filter(vocabulary_id=_VOCABULARY_ID).exists():
        Vocabulary.objects.filter(vocabulary_id=_VOCABULARY_ID).delete()


def noop_reverse(apps, schema_editor):
    """Not recreated on reverse.

    These concepts should never have existed; re-minting them on a rollback
    would reintroduce ids inside OHDSI's range claiming to be standard.
    """


class Migration(migrations.Migration):

    dependencies = [('omop_core', '0189_source_code_mapping_review_status')]

    operations = [
        migrations.RunPython(drop_fabricated_vocabulary, noop_reverse),
    ]
