"""Mint local concepts for the four language capabilities (#813).

speak, read, write and understand have no coded equivalent to borrow. SNOMED's
nearest concepts are speech-pathology findings -- 4118707 "Able to speak",
4115799 "Unable to speak" -- which assert something clinically different, since
a person who speaks a language but does not write it is not "unable to write".
37162770 "Language written" is an Observable Entity, a question rather than an
answer, and nothing codes "understand" as a language skill at all. So they are
minted locally rather than aliased onto concepts that mean something else.

Quarantine rules, the same ones HK-Wearable and HK-Observation follow and
_assert_local_mint enforces: an HK-* vocabulary, source='HealthKey',
standard_concept=None, and a concept_id in OHDSI's reserved local range
(>= 2_000_000_000). "HK-Language" is carried by vocabulary_id; source is the
separate provenance column saying the row was authored here rather than loaded
from Athena. Both are required together -- neither alone identifies a local
mint.

Domain is 'Meas Value' because these are answers, not attributes: they are what
a person's skill in a language IS, so they belong where OMOP puts value
concepts. The language itself remains a 'Language'-domain SNOMED concept. The
two are different axes, and collapsing them is precisely the mistake
manage_language_skills made when it filed languages themselves under
'Meas Value' (#812).

Duplicated in omop_core/concept_fixtures.py so a test database has them without
running migrations; a drift test asserts the two definitions stay identical.
"""
from datetime import date

from django.db import migrations


_CONCEPTS = (
    (2_100_007_853, 'hkl:speak', 'Speaks language'),
    (2_100_007_854, 'hkl:read', 'Reads language'),
    (2_100_007_855, 'hkl:write', 'Writes language'),
    (2_100_007_856, 'hkl:understand', 'Understands language'),
)


def seed_capability_concepts(apps, schema_editor):
    Vocabulary = apps.get_model('omop_core', 'Vocabulary')
    Domain = apps.get_model('omop_core', 'Domain')
    ConceptClass = apps.get_model('omop_core', 'ConceptClass')
    Concept = apps.get_model('omop_core', 'Concept')

    Vocabulary.objects.get_or_create(
        vocabulary_id='HK-Language',
        defaults={
            'vocabulary_name': 'HealthKey language capability concepts',
            'vocabulary_reference': 'https://healthkey.ai',
            'vocabulary_version': '1.0',
            'vocabulary_concept_id': 0,
        },
    )
    Domain.objects.get_or_create(
        domain_id='Meas Value',
        defaults={'domain_name': 'Measurement Value', 'domain_concept_id': 21},
    )
    ConceptClass.objects.get_or_create(
        concept_class_id='Qualifier Value',
        defaults={
            'concept_class_name': 'Qualifier Value',
            'concept_class_concept_id': 0,
        },
    )

    for concept_id, concept_code, concept_name in _CONCEPTS:
        Concept.objects.get_or_create(
            vocabulary_id='HK-Language',
            concept_code=concept_code,
            defaults={
                'concept_id': concept_id,
                'concept_name': concept_name,
                'domain_id': 'Meas Value',
                'concept_class_id': 'Qualifier Value',
                'standard_concept': None,
                'source': 'HealthKey',
                'valid_start_date': date(1970, 1, 1),
                'valid_end_date': date(2099, 12, 31),
                'invalid_reason': None,
            },
        )


def unseed_capability_concepts(apps, schema_editor):
    """Remove the concepts, and the vocabulary if nothing else uses it.

    PersonLanguageSkill.skill_concept is PROTECT, so this raises rather than
    silently orphaning rows if any person still references a capability. That
    is the correct outcome: the reverse of a mint cannot run while the mint is
    in use.
    """
    Concept = apps.get_model('omop_core', 'Concept')
    Vocabulary = apps.get_model('omop_core', 'Vocabulary')

    Concept.objects.filter(
        vocabulary_id='HK-Language',
        concept_code__in=[code for _cid, code, _name in _CONCEPTS],
    ).delete()
    if not Concept.objects.filter(vocabulary_id='HK-Language').exists():
        Vocabulary.objects.filter(vocabulary_id='HK-Language').delete()


class Migration(migrations.Migration):

    dependencies = [('omop_core', '0186_seed_language_and_condition_value_concepts')]

    operations = [
        migrations.RunPython(seed_capability_concepts, unseed_capability_concepts),
    ]
