"""Preservation and multi-patient regressions from the #1267 review."""
import json
from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from omop_core.models import Measurement, Observation, PatientRecord
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.sample_disease_profiles import complete_demo_fhir_bundle
from omop_core.signals import suppress_patient_record_refresh
from omop_core.test_utils import ensure_test_concept_zero
from patient_portal.models import Identity
from tests.factories import (
    ConceptFactory, MeasurementFactory, ObservationFactory,
    OrganizationFactory, PatientRecordFactory,
)

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('score', [0, 3])
def test_unrelated_api_edit_preserves_historical_flipi(score):
    record = PatientRecordFactory(flipi_score=score, flipi_score_options=None)
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='review@example.test', is_staff=True))
    response = client.patch(f'/api/patient-info/{record.person_id}/',
                            {'ecog_performance_status': 1}, format='json')
    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.flipi_score == score
    assert record.flipi_score_options is None


def test_omop_numeric_score_survives_refresh_but_explicit_clear_wins():
    with suppress_patient_record_refresh():
        record = PatientRecordFactory(flipi_score=None, flipi_score_options=None)
        ObservationFactory(person=record.person,
                           observation_concept=ConceptFactory(concept_name='FLIPI score'),
                           value_as_number=3, value_as_string=None)
    record = refresh_patient_record(record.person)
    assert record.flipi_score == 3
    assert record.flipi_score_options is None
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='clear@example.test', is_staff=True))
    for value, expected in [('', 0), (None, None)]:
        response = client.patch(f'/api/patient-info/{record.person_id}/',
                                {'flipi_score_options': value}, format='json')
        assert response.status_code == 200, response.data
        assert response.data['flipi_score'] == expected
        for _ in range(2):
            record = refresh_patient_record(record.person)
            assert record.flipi_score == expected
            assert record.flipi_score_options == value


@pytest.mark.parametrize('score', [0, 3])
def test_backfill_does_not_invent_factors_for_a_historical_score(score):
    ensure_test_concept_zero()
    record = PatientRecordFactory(
        organization=OrganizationFactory(slug='synthea-fl'),
        disease='Follicular Lymphoma', stage='II',
        flipi_score=score, flipi_score_options=None,
    )
    call_command('backfill_sample_disease_profiles', confirm=True, stdout=StringIO())
    record.refresh_from_db()
    assert record.flipi_score == score
    assert record.flipi_score_options is None
    assert not Observation.objects.filter(person=record.person,
                                           observation_source_value='demo:flipi_score_options').exists()


@pytest.mark.parametrize('selection,expected_score,expected_gelf', [
    ('', 0, 'Not Met'), ('hemoglobin', 1, 'Met'), (None, 3, 'Met'),
])
def test_backfill_recovers_only_unassessed_checklists(selection, expected_score, expected_gelf):
    ensure_test_concept_zero()
    gelf = 'b_symptoms' if selection else selection
    with suppress_patient_record_refresh():
        record = PatientRecordFactory(
            organization=OrganizationFactory(slug='synthea-fl'),
            disease='Follicular Lymphoma', stage='II',
            flipi_score_options=selection, gelf_criteria_options=gelf,
            user_edited_fields=[],
        )
        for field, value in [('flipi_score_options', 'age,stage,ldh'),
                             ('gelf_criteria_options', 'large_mass')]:
            MeasurementFactory(person=record.person, measurement_concept_id=0,
                               measurement_source_value=f'demo:{field}',
                               measurement_date=date(2020, 1, 1),
                               value_as_string=value, value_as_number=None)
    counts = None
    for _ in range(2):
        call_command('backfill_sample_disease_profiles', confirm=True, stdout=StringIO())
        record.refresh_from_db()
        assert record.flipi_score == expected_score
        assert record.gelf_criteria_status == expected_gelf
        assert record.flipi_score_options == ('age,stage,ldh' if selection is None else selection)
        assert record.gelf_criteria_options == ('large_mass' if selection is None else gelf)
        current_counts = (Measurement.objects.count(), Observation.objects.count())
        if counts is not None:
            assert current_counts == counts
        counts = current_counts


def _patient(pid):
    return {'resourceType': 'Patient', 'id': pid,
            'name': [{'given': [pid], 'family': 'Review'}],
            'gender': 'female', 'birthDate': '1950-01-01'}


def _observation(pid, code, value):
    return {'resourceType': 'Observation', 'status': 'final',
            'subject': {'reference': f'Patient/{pid}'},
            'code': {'coding': [{'system': 'http://loinc.org', 'code': code}]},
            'effectiveDateTime': '2024-01-01', 'valueString': value}


@pytest.mark.parametrize('nodes,her2,expected', [
    ('N0', 'Negative', True), ('N1', 'Negative', True),
    ('N2', 'Negative', False), ('N0', 'Positive', False),
])
def test_generated_bc_bundle_uses_nodal_stage_for_oncotype(nodes, her2, expected):
    resources = [_patient('bc')]
    resources.extend(_observation('bc', code, value) for code, value in [
        ('21908-9', 'Stage II'), ('21906-3', nodes),
        ('16112-5', 'Positive'), ('16113-3', 'Positive'), ('48676-1', her2),
    ])
    bundle = {'resourceType': 'Bundle', 'entry': [{'resource': r} for r in resources]}
    complete_demo_fhir_bundle(bundle, 'BC')
    complete_demo_fhir_bundle(bundle, 'BC')
    oncotype = [entry for entry in bundle['entry']
                if entry['resource'].get('code', {}).get('coding', [{}])[0].get('code') == 'demo:oncotype_dx_score']
    assert len(oncotype) == int(expected)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('disease', ['FL', 'MM', 'BC'])
def test_completed_multi_patient_bundle_survives_single_patient_import_batches(tmp_path, disease, monkeypatch):
    from patient_portal.tests import _make_vocab_fixtures
    from omop_core.management.commands import import_fhir_bundle

    _make_vocab_fixtures()
    # Remote-connection tuning is unrelated to this local round trip and must
    # not change global socket/database settings for later tests.
    monkeypatch.setattr(import_fhir_bundle, '_patch_db_timeouts', lambda: None)
    Identity.objects.create_user(email='batch-import@example.test', is_staff=True)
    resources = [_patient('first'), _patient('second')]
    bundle = {'resourceType': 'Bundle', 'type': 'collection',
              'entry': [{'resource': r} for r in resources]}
    complete_demo_fhir_bundle(bundle, disease)
    expected = {}
    for entry in bundle['entry']:
        resource = entry['resource']
        if resource.get('code', {}).get('coding', [{}])[0].get('code') == 'demo:ecog_performance_status':
            expected[resource['subject']['reference'].split('/')[-1]] = resource['valueQuantity']['value']
    path = tmp_path / 'patients.json'
    path.write_text(json.dumps(bundle))
    output = StringIO()
    call_command('import_fhir_bundle', file=str(path), org_slug='synthea-' + disease.lower(),
                 batch_size=1, stdout=output, stderr=output)
    assert 'created=2 updated=0 errors=0' in output.getvalue(), output.getvalue()
    records = list(PatientRecord.objects.select_related('person'))
    assert len(records) == 2
    assert {r.person.given_name: r.ecog_performance_status for r in records} == expected
    for record in records:
        assert Measurement.objects.filter(person=record.person,
                                          measurement_source_value='demo:ecog_performance_status').count() == 1
