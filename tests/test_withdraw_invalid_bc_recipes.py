from importlib import import_module
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import connection
from django.utils import timezone

from omop_core.models import FieldConceptMapping, SourceCodeConceptMapping
from tests.factories import ConceptFactory

pytestmark = pytest.mark.django_db
forward = import_module('omop_core.migrations.0233_withdraw_invalid_bc_field_recipes').forward


def test_withdrawal_uses_namespaced_codes_and_preserves_other_curator_decisions():
    question = ConceptFactory(vocabulary__vocabulary_id='LOCAL', concept_code='85337-4')
    mapping, _ = FieldConceptMapping.objects.update_or_create(field_name='ki67_proliferation_index', defaults={
        'concept': question, 'vocabulary_id': 'LOCAL', 'concept_code': '85337-4',
        'status': 'approved', 'notes': 'Different namespace: retain for review.'})
    forward(apps, SimpleNamespace(connection=connection))
    mapping.refresh_from_db()
    assert mapping.status == 'approved'
    assert mapping.concept_id == question.pk


def test_untouched_wrong_ki67_seed_is_withdrawn_idempotently_but_reviewed_row_survives():
    er = ConceptFactory(vocabulary__vocabulary_id='LOINC', concept_code='85337-4',
                        concept_name='Estrogen receptor Ag [Presence] in Breast cancer specimen by Immune stain')
    common = {'source_vocabulary_id': '', 'origin_system': 'hk-labs-seed',
              'status': 'approved', 'target_concept': er, 'domain_id': 'Measurement'}
    untouched = SourceCodeConceptMapping.objects.create(source_code='ki67', **common)
    reviewed = SourceCodeConceptMapping.objects.create(source_code='ki 67 proliferation index',
        reviewed_at=timezone.now(), notes='Preserve explicit review for separate reconciliation.', **common)
    forward(apps, SimpleNamespace(connection=connection))
    untouched.refresh_from_db()
    reviewed.refresh_from_db()
    assert untouched.status == 'proposed'
    assert untouched.target_concept_id is None
    assert str(er.pk) in untouched.notes
    assert reviewed.status == 'approved'
    assert reviewed.target_concept_id == er.pk
    notes = untouched.notes
    forward(apps, SimpleNamespace(connection=connection))
    untouched.refresh_from_db()
    assert untouched.notes == notes
