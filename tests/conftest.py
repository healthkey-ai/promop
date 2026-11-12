"""Fixtures shared by the pytest suite."""
import json
from pathlib import Path

import pytest

FIELD_CURATION_FIXTURE = (
    Path(__file__).parents[1] / 'omop_core' / 'data' / 'field_curation_v1.json'
)


@pytest.fixture
def lab_field_mappings(db):
    """Seed the lab-field curation rows a migrated database carries.

    pytest runs ``--no-migrations``, so 0240, 0285 and 0327 never fire here and
    the lab routing falls back to ``_LOINC_LAB_FIELDS``. This loads the
    fixture's lab rows through the apply logic 0240 uses, with a LOINC Concept
    for each so the rows resolve, then runs 0285's alias approval, so a test
    derives through the mapping tables as a deployed instance does.

    Returns the resolved LOINC code → (field, cast) routing.
    """
    from omop_core.services.field_curation_transfer import apply_payload
    from omop_core.services.patient_record_service import (
        LAB_FIELDS, resolve_lab_code_fields,
    )
    from tests.factories import ConceptFactory, VocabularyFactory
    from tests.migration_utils import run_data_migration

    data = json.loads(FIELD_CURATION_FIXTURE.read_text())
    rows = [row for row in data['mappings'] if row['field_name'] in LAB_FIELDS]
    vocabulary = VocabularyFactory(vocabulary_id='LOINC')
    for row in rows:
        ConceptFactory(
            concept_id=row['concept_id'], concept_code=row['concept_code'],
            vocabulary=vocabulary, concept_name=f"LOINC {row['concept_code']}",
        )
    apply_payload({'mappings': rows}, tables=('mappings',))
    run_data_migration('0285_approve_lab_extractor_aliases', 'approve_aliases')
    return resolve_lab_code_fields()


@pytest.fixture
def stale_answer_formula_allowed(db):
    """A database whose CHECK does not yet refuse an active answer formula.

    0341 extends ``field_formula_explicit_answer_inactive`` to every
    authored-answer field (#2489). Tests of what the evaluator, the serializer
    and the repair command do with a row left active by an older schema need
    that row; dropping the constraint inside the test's transaction recreates
    the older schema and is rolled back with it.
    """
    from django.db import connection

    drop_explicit_answer_check(connection)


def drop_explicit_answer_check(connection):
    """Drop the explicit-answer CHECK, only where the drop will roll back.

    A transactional test (``transactional_db``) commits, so the DDL would
    outlive the test and silently weaken every later one; refuse it there.
    """
    assert connection.in_atomic_block, (
        'stale_answer_formula_allowed needs a test transaction to roll the '
        'constraint drop back; it cannot be used with transactional_db.')
    with connection.cursor() as cursor:
        cursor.execute(
            'ALTER TABLE field_formula '
            'DROP CONSTRAINT field_formula_explicit_answer_inactive')
