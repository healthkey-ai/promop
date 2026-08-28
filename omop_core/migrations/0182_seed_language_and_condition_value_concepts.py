"""Value concepts for languages_skills and preexisting_conditions (#774, #723).

Both issues asked for one concept each — SNOMED 4267143 "Language" and 4010833
"Pre-existing condition". Both are the right *question* concept: they say what
the field is asking. Neither carries an answer. 4267143 does not say which
language, and 4010833 does not say which condition, so on their own they leave
the stored values uncoded, which is the thing that made these fields unusable
for matching.

So each field gets both halves:

  - a FieldConceptMapping on the question concept, which is what the issues
    asked for and what the curation page keys on, and
  - FieldChoice/FieldChoiceCode rows carrying the actual answers, following
    migration 0162's pattern.

Every concept here is SNOMED and standard, so unlike the MeSH case in #803 these
can be written to a *_concept_id column without violating the standardness rule
in docs/vocabularies.md.

Two of the eleven pre-existing-condition categories deliberately have no code.
"Performance Status" and "Prior Therapies" are not conditions — they are other
eligibility axes that happen to live in the same category list, already modelled
by ecog_performance_status and the therapy-line fields. Coding them as
conditions would assert something clinically untrue. They stay as choices
without codes, which the schema allows.

"Other" under languages_skills is likewise uncoded: it is the absence of a
specific language, not a language.
"""
from django.db import migrations


_QUESTION_CONCEPTS = [
    # field_name, vocabulary, concept_code, note
    ('languages_skills', 'SNOMED', '61909002',
     'Language (#774) — the question this field answers.'),
    ('preexisting_conditions', 'SNOMED', '102478008',
     'Pre-existing condition (#723) — the question this field answers.'),
]

# field_name → [(display, [(code, vocabulary_id, display, is_primary), ...])]
_VALUE_CHOICES = {
    'languages_skills': [
        ('English', [('297487008', 'SNOMED', 'English language', True)]),
        ('Spanish', [('297510001', 'SNOMED', 'Spanish language', True)]),
        # Deliberately uncoded — see module docstring.
        ('Other', []),
    ],
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
            # target, exactly as migration 0160 does.
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

    FieldChoice.objects.filter(field_name__in=_VALUE_CHOICES).delete()
    FieldConceptMapping.objects.filter(
        field_name__in=[f for f, _v, _c, _n in _QUESTION_CONCEPTS],
    ).delete()


class Migration(migrations.Migration):

    dependencies = [('omop_core', '0181_seed_refractory_field_mappings')]

    operations = [migrations.RunPython(seed, unseed)]
