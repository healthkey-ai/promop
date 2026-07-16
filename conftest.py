"""
Session-level test-DB setup that pytest.ini's `--no-migrations` skips.

`--no-migrations` builds the test database by reflecting current Django
model state, not by replaying migration files. That's fast, but it silently
skips any migration whose effect isn't representable in model state —
specifically:

- `pg_trgm` extension (added via a `TrigramExtension()` migration op, needed
  for the `concept.concept_name` GIN trigram index)
- The Postgres sequences created in `omop_core/migrations/0074_add_pk_sequences.py`
  and `omop_oncology/migrations/0006_add_episode_pk_sequence.py` for OMOP
  tables that use a plain `BigIntegerField`/`IntegerField` primary key rather
  than `BigAutoField` — required by `omop_core.services.pk.next_pk`/`next_pk_batch`.

Without this, any code path that calls `next_pk`/`next_pk_batch` fails
(`relation "<table>_<pk>_seq" does not exist`) — confirmed against a freshly
created local test DB.

Also seeds concept 32817 ('EHR', a Type Concept used as
`measurement_type_concept_id`/`observation_type_concept_id` by several write
paths, including `enrich_breast_cancer_omop_data`) directly rather than via
`seed_omop_concepts` — that command has its own pre-existing bug (references
`vocabulary_id='None'` for concept 3000963 but never seeds a `'None'`
Vocabulary row, so calling it against an empty DB raises an IntegrityError).
`test_populate_patient_info.py`'s 45 tests already pass without any of this —
their factories create whatever concepts they need on demand — so this only
adds the one concept this branch's new commands actually depend on.

`pg_trgm` is handled separately (not here): the GIN trigram index on
`concept.concept_name` is declared in the model's `Meta.indexes`, so
`--no-migrations` schema-reflection recreates it as part of the initial
`CREATE TABLE` step inside the base `django_db_setup` fixture — before any
override of this fixture gets to run. Enable `pg_trgm` on Postgres's
`template1` database instead (`CREATE EXTENSION IF NOT EXISTS pg_trgm` while
connected to `template1`) — new databases clone it by default, so every
future test DB (and any other new local DB) has it with no ordering problem.
This is a one-time local Postgres setup step, not something a fixture here
can fix.
"""
import pytest

# Mirrors omop_core/migrations/0074_add_pk_sequences.py +
# omop_oncology/migrations/0006_add_episode_pk_sequence.py. Keep in sync if
# either migration's table list changes.
_PK_SEQUENCES = [
    ('measurement', 'measurement_id'),
    ('visit_occurrence', 'visit_occurrence_id'),
    ('care_site', 'care_site_id'),
    ('location', 'location_id'),
    ('visit_detail', 'visit_detail_id'),
    ('concept', 'concept_id'),
    ('condition_occurrence', 'condition_occurrence_id'),
    ('drug_exposure', 'drug_exposure_id'),
    ('observation', 'observation_id'),
    ('procedure_occurrence', 'procedure_occurrence_id'),
    ('person', 'person_id'),
    ('episode', 'episode_id'),
]


@pytest.fixture(scope='session')
def django_db_setup(django_db_setup, django_db_blocker):
    """Extend pytest-django's test DB setup with the schema side-effects
    `--no-migrations` doesn't replay. Depends on the built-in fixture of the
    same name so this runs strictly after the test DB/tables exist."""
    with django_db_blocker.unblock():
        from django.db import connection
        from omop_core.models import (
            Concept,
            ConceptClass,
            Domain,
            SctEligibility,
            StemCellTransplant,
            Vocabulary,
        )

        with connection.cursor() as cursor:
            for table, pk_field in _PK_SEQUENCES:
                seq_name = f'{table}_{pk_field}_seq'
                cursor.execute(f'CREATE SEQUENCE IF NOT EXISTS "{seq_name}"')
                cursor.execute(
                    f'SELECT setval(%s, COALESCE(MAX("{pk_field}"), 0) + 1, false) '
                    f'FROM "{table}"',
                    [seq_name],
                )

        Vocabulary.objects.get_or_create(
            vocabulary_id='Type Concept',
            defaults={'vocabulary_name': 'OMOP Type Concept',
                      'vocabulary_reference': 'OMOP generated',
                      'vocabulary_version': 'v5', 'vocabulary_concept_id': 0},
        )
        Domain.objects.get_or_create(
            domain_id='Type Concept',
            defaults={'domain_name': 'Type Concept', 'domain_concept_id': 0},
        )
        ConceptClass.objects.get_or_create(
            concept_class_id='Type Concept',
            defaults={'concept_class_name': 'Type Concept', 'concept_class_concept_id': 0},
        )
        Vocabulary.objects.get_or_create(
            vocabulary_id='None',
            defaults={'vocabulary_name': 'None',
                      'vocabulary_reference': '', 'vocabulary_version': '',
                      'vocabulary_concept_id': 0},
        )
        Domain.objects.get_or_create(
            domain_id='Metadata',
            defaults={'domain_name': 'Metadata', 'domain_concept_id': 0},
        )
        ConceptClass.objects.get_or_create(
            concept_class_id='Undefined',
            defaults={'concept_class_name': 'Undefined', 'concept_class_concept_id': 0},
        )
        Concept.objects.get_or_create(
            concept_id=0,
            defaults={
                'concept_name': 'No matching concept', 'domain_id': 'Metadata',
                'vocabulary_id': 'None', 'concept_class_id': 'Undefined',
                'concept_code': 'No matching concept',
                'valid_start_date': '1970-01-01', 'valid_end_date': '2099-12-31',
            },
        )
        Concept.objects.get_or_create(
            concept_id=32817,
            defaults={
                'concept_name': 'EHR', 'domain_id': 'Type Concept',
                'vocabulary_id': 'Type Concept', 'concept_class_id': 'Type Concept',
                'standard_concept': 'S', 'concept_code': 'OMOP4976890',
                'valid_start_date': '1970-01-01', 'valid_end_date': '2099-12-31',
            },
        )
        for code, title in [
            ('eligibleAuto', 'eligible for autologous SCT'),
            ('eligibleAllo', 'eligible for allogeneic SCT'),
            ('ineligibleAuto', 'ineligible for autologous SCT'),
            ('ineligibleAllo', 'ineligible for allogeneic SCT'),
        ]:
            SctEligibility.objects.get_or_create(code=code, defaults={'title': title})

        for code, title in [
            ('autologousSCT', 'autologous SCT'),
            ('allogeneicSCT', 'allogeneic SCT'),
            ('tandemSCT', 'tandem SCT'),
        ]:
            StemCellTransplant.objects.get_or_create(code=code, defaults={'title': title})
