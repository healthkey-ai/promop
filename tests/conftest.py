"""Fixtures shared by the pytest suite."""
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

FIELD_CURATION_FIXTURE = (
    Path(__file__).parents[1] / 'omop_core' / 'data' / 'field_curation_v1.json'
)


@pytest.fixture
def lab_field_mappings(db):
    """Seed the lab-field curation rows a migrated database carries.

    pytest runs ``--no-migrations``, so 0240, 0285 and 0326 never fire here and
    the lab routing falls back to ``_LOINC_LAB_FIELDS``. This loads the
    fixture's lab rows through the apply logic 0240 uses, with a LOINC Concept
    for each so the rows resolve, then runs 0285's alias approval, so a test
    derives through the mapping tables as a deployed instance does.

    Returns the resolved LOINC code → (field, cast) routing.
    """
    from django.apps import apps
    from django.db import connection

    from omop_core.services.field_curation_transfer import apply_payload
    from omop_core.services.patient_record_service import (
        LAB_FIELDS, resolve_lab_code_fields,
    )
    from tests.factories import ConceptFactory, VocabularyFactory

    data = json.loads(FIELD_CURATION_FIXTURE.read_text())
    rows = [row for row in data['mappings'] if row['field_name'] in LAB_FIELDS]
    vocabulary = VocabularyFactory(vocabulary_id='LOINC')
    for row in rows:
        ConceptFactory(
            concept_id=row['concept_id'], concept_code=row['concept_code'],
            vocabulary=vocabulary, concept_name=f"LOINC {row['concept_code']}",
        )
    apply_payload({'mappings': rows}, tables=('mappings',))
    aliases = importlib.import_module(
        'omop_core.migrations.0285_approve_lab_extractor_aliases',
    )
    aliases.approve_aliases(apps, SimpleNamespace(connection=connection))
    return resolve_lab_code_fields()
