"""Value concepts for languages_skills and preexisting_conditions (#774, #723).

Both issues asked for one concept each — SNOMED 4267143 "Language" and 4010833
"Pre-existing condition". Both are the right *question* concept: they say what
the field is asking. Neither carries an answer. 4267143 does not say which
language, and 4010833 does not say which condition, so on their own they leave
the stored values uncoded, which is the thing that made these fields unusable
for matching.

So each field gets a FieldConceptMapping on the question concept, and
preexisting_conditions additionally gets FieldChoice/FieldChoiceCode rows
carrying the actual answers, following migration 0162's pattern.

The mappings are seeded 'proposed', so they appear in the curation page as
proposals for a reviewer rather than as finished mappings. They deliberately
carry no omop_table/source_value/value_kind: _curated_writes needs all three
before a field becomes writable, and deciding where a field's fact lives is the
reviewer's call, not a migration's. Nothing here makes either field editable —
that follows approval.

languages_skills gets NO choices, though #774 reads as though it should.
patient_record_service derives it as a composite over two vocabularies —
"English language: speak, Spanish language: write" — so bare language names
could never match a stored value, and offering them as options would present a
reviewer with values no record can hold. The language value concepts that issue
wants (4180186 "English language", 4182511 "Spanish language") are already what
PersonLanguageSkill.language_concept holds and what derivation reads back. What
is missing is a write path creating those rows at all, which is a separate
change from coding a value set.

Every concept here is SNOMED and standard, so unlike the MeSH case in #803 these
can be written to a *_concept_id column without violating the standardness rule
in docs/vocabularies.md.

One category spans two domains. Ten of the coded categories resolve to
Condition-domain concepts, but 413712001 "Breastfeeding (mother)" is an
Observation: breastfeeding is a state, not a disorder, and SNOMED has no
Condition-domain concept for it. It is kept because dropping it would leave
half of "Pregnancy or Breastfeeding" uncoded, and marked is_primary=False so
the Condition-domain 77386006 "Pregnancy" is what a single-code consumer
writing to condition_occurrence picks up. A test pins that ordering — flipping
it would route the row to the wrong OMOP table.

The two question concepts are themselves Observation-domain, which is what a
concept naming a question rather than a finding should be.

Two of the eleven pre-existing-condition categories deliberately have no code.
"Performance Status" and "Prior Therapies" are not conditions — they are other
eligibility axes that happen to live in the same category list, already modelled
by ecog_performance_status and the therapy-line fields. Coding them as
conditions would assert something clinically untrue. They stay as choices
without codes, which the schema allows.

"Other" under languages_skills is likewise uncoded: it is the absence of a
specific language, not a language.
"""
import logging

from django.db import migrations

logger = logging.getLogger(__name__)


_QUESTION_CONCEPTS = [
    # field_name, vocabulary, concept_code, note
    ('languages_skills', 'SNOMED', '61909002',
     'Language (#774) — the question this field answers.'),
    ('preexisting_conditions', 'SNOMED', '102478008',
     'Pre-existing condition (#723) — the question this field answers.'),
]

# field_name → [(display, [(code, vocabulary_id, display, is_primary), ...])]
_VALUE_CHOICES = {
    'preexisting_conditions': [
        ('Cardiac Issues', [('56265001', 'SNOMED', 'Heart disease', True)]),
        ('Pulmonary Disease', [('19829001', 'SNOMED', 'Disorder of lung', True)]),
        ('Renal Impairment', [('236423003', 'SNOMED', 'Renal impairment', True)]),
        ('Hepatic Impairment', [('235856003', 'SNOMED', 'Disease of liver', True)]),
        ('Infections', [('40733004', 'SNOMED', 'Infectious disease', True)]),
        ('Other Active Malignancies',
         [('363346000', 'SNOMED', 'Malignant neoplastic disease', True)]),
        # Two axes in one category, so two codes; the schema allows several per
        # choice and is_primary picks the one a single-code consumer should use.
        ('Neurological and Psychiatric Conditions',
         [('118940003', 'SNOMED', 'Disorder of nervous system', True),
          ('74732009', 'SNOMED', 'Mental disorder', False)]),
        ('Autoimmune and Inflammatory Disorders',
         [('85828009', 'SNOMED', 'Autoimmune disease', True),
          ('128139000', 'SNOMED', 'Inflammatory disorder', False)]),
        # Mixed domains: Pregnancy is a Condition, Breastfeeding an Observation.
        # Primary must stay on the Condition — see the module docstring.
        ('Pregnancy or Breastfeeding',
         [('77386006', 'SNOMED', 'Pregnancy', True),
          ('413712001', 'SNOMED', 'Breastfeeding (mother)', False)]),
        # Deliberately uncoded — see module docstring.
        ('Performance Status', []),
        ('Prior Therapies', []),
    ],
}


def seed(apps, schema_editor):
    Concept = apps.get_model('omop_core', 'Concept')
    FieldChoice = apps.get_model('omop_core', 'FieldChoice')
    FieldChoiceCode = apps.get_model('omop_core', 'FieldChoiceCode')
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')

    for field_name, vocabulary_id, concept_code, note in _QUESTION_CONCEPTS:
        concept = Concept.objects.filter(
            vocabulary_id=vocabulary_id, concept_code=concept_code,
        ).first()
        if concept is None:
            # SNOMED loads separately. Without the concept the descriptor must
            # keep the field read-only rather than emit a write recipe with no
            # target, exactly as migration 0160 does. Logged rather than passed
            # over, so a half-applied migration — choices seeded, mappings not —
            # is diagnosable from the deploy log, as 0181 does.
            logger.warning(
                'Concept %s:%s is missing; %s keeps no proposed mapping.',
                vocabulary_id, concept_code, field_name,
            )
            continue
        FieldConceptMapping.objects.update_or_create(
            field_name=field_name,
            defaults=dict(
                concept=concept,
                vocabulary_id=vocabulary_id,
                concept_code=concept_code,
                status='proposed',
                notes=note,
            ),
        )

    for field_name, choices in _VALUE_CHOICES.items():
        for sort_order, (display, codes) in enumerate(choices):
            choice, _ = FieldChoice.objects.get_or_create(
                field_name=field_name, display=display,
                defaults={'sort_order': sort_order},
            )
            for code, vocabulary_id, code_display, is_primary in codes:
                FieldChoiceCode.objects.update_or_create(
                    choice=choice, vocabulary_id=vocabulary_id, code=code,
                    defaults={'display': code_display, 'is_primary': is_primary},
                )


def unseed(apps, schema_editor):
    """Remove what this migration added, leaving concepts alone.

    The concepts are Athena-loaded SNOMED rows shared with everything else; only
    the choices and mappings here are ours to remove.
    """
    FieldChoice = apps.get_model('omop_core', 'FieldChoice')
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')

    # Scoped to the displays seeded here. FieldChoice is curator-managed — it
    # carries created_by and is creatable through the API — so deleting every
    # choice for these fields would destroy rows this migration never made.
    # Migration 0162's reverse scopes by display for the same reason.
    for field_name, choices in _VALUE_CHOICES.items():
        FieldChoice.objects.filter(
            field_name=field_name,
            display__in=[display for display, _codes in choices],
        ).delete()
    FieldConceptMapping.objects.filter(
        field_name__in=[f for f, _v, _c, _n in _QUESTION_CONCEPTS],
    ).delete()


class Migration(migrations.Migration):

    dependencies = [('omop_core', '0181_seed_refractory_field_mappings')]

    operations = [migrations.RunPython(seed, unseed)]
