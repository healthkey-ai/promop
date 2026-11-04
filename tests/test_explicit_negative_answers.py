"""Explicit confirmations for the two derived negative states (#5666)."""
import json
from io import StringIO

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from django.db import IntegrityError

from omop_core.models import ExplicitNegativeAnswer, FieldFormula, GroupAccess
from omop_core.services.patient_record_service import _compute_derived_fields
from omop_core.services.write_descriptor import build_writable_field_descriptor
from patient_portal.models import Identity, PatientUser
from tests.factories import PatientRecordFactory

pytestmark = pytest.mark.django_db

INFECTION = 'no_active_infection_status'
MALIGNANCIES = 'no_other_active_malignancies'


@pytest.fixture
def editor():
    record = PatientRecordFactory(
        active_infection_status=None,
        active_malignancies=None,
        no_active_infection_status=None,
        no_other_active_malignancies=None,
    )
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='negative-answer@example.test', is_staff=True))
    return record, client


def source_of(record, field):
    return (record.active_infection_status if field == INFECTION
            else record.active_malignancies)


def answer(editor, field, action, **extra):
    record, client = editor
    record.refresh_from_db()
    return client.post(
        f'/api/v1/patient-records/{record.person_id}/negative-answer/',
        {
            'field': field,
            'action': action,
            'expected_value': getattr(record, field),
            'expected_source': source_of(record, field),
            **extra,
        },
        format='json',
    )


def snapshot(editor, field):
    record, client = editor
    return client.get(
        f'/api/v1/patient-records/{record.person_id}/negative-answer/',
        {'field': field},
    )


def test_descriptor_exposes_both_negative_answers():
    descriptor = build_writable_field_descriptor()
    for field, label in ((INFECTION, 'Confirm no active infection'),
                         (MALIGNANCIES, 'Confirm no other active malignancies')):
        entry = descriptor[field]
        assert entry['kind'] == 'explicit-answer'
        assert entry['writable'] is False
        assert entry['answerable'] is True
        assert entry['endpoint'] == 'negative-answer'
        assert entry['actions'] == ['confirm_negative', 'withdraw']
        assert entry['action_labels']['confirm_negative'] == label


def test_confirm_no_active_infection_persists_across_recomputation(editor):
    record, _ = editor
    response = answer(editor, INFECTION, 'confirm_negative')
    assert response.status_code == 200, response.data
    assert response.data == {
        'field': INFECTION, 'value': True, 'confirmed': True, 'source': None,
    }
    assert ExplicitNegativeAnswer.objects.filter(
        person=record.person, field=INFECTION, answer=True,
    ).exists()
    record.refresh_from_db()
    assert record.no_active_infection_status is True

    # A later source change must not silently overwrite the attestation.
    record.active_infection_status = True
    _compute_derived_fields(record)
    assert record.no_active_infection_status is True


def test_withdraw_returns_to_the_source_value(editor):
    record, _ = editor
    assert answer(editor, INFECTION, 'confirm_negative').status_code == 200
    response = answer(editor, INFECTION, 'withdraw')
    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.no_active_infection_status is None
    assert not ExplicitNegativeAnswer.objects.exists()


def test_confirm_refuses_a_recorded_active_infection(editor):
    record, _ = editor
    record.active_infection_status = True
    record.save(update_fields=['active_infection_status'])
    response = answer(editor, INFECTION, 'confirm_negative')
    assert response.status_code == 400
    assert 'active infection' in response.data['detail']
    assert not ExplicitNegativeAnswer.objects.exists()


def test_confirm_refuses_more_than_one_active_malignancy(editor):
    record, _ = editor
    record.active_malignancies = [{'name': 'primary'}, {'name': 'second'}]
    record.save(update_fields=['active_malignancies'])
    response = answer(editor, MALIGNANCIES, 'confirm_negative')
    assert response.status_code == 400
    assert 'active malignancy' in response.data['detail']
    assert not ExplicitNegativeAnswer.objects.exists()


def test_one_active_malignancy_is_consistent_with_no_other(editor):
    record, _ = editor
    record.active_malignancies = [{'name': 'primary'}]
    record.save(update_fields=['active_malignancies'])
    response = answer(editor, MALIGNANCIES, 'confirm_negative')
    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.no_other_active_malignancies is True


def test_stale_value_or_source_is_refused_without_writing(editor):
    record, _ = editor
    assert answer(editor, INFECTION, 'confirm_negative', expected_value=False).status_code == 409
    assert answer(editor, INFECTION, 'confirm_negative', expected_source=True).status_code == 409
    assert not ExplicitNegativeAnswer.objects.exists()


def test_snapshot_is_read_only(editor):
    record, _ = editor
    response = snapshot(editor, INFECTION)
    assert response.status_code == 200
    assert response.data == {
        'field': INFECTION, 'value': None, 'confirmed': False, 'source': None,
    }
    assert not ExplicitNegativeAnswer.objects.exists()


def test_without_an_attestation_the_source_still_derives(editor):
    record, _ = editor
    record.active_infection_status = True
    _compute_derived_fields(record)
    assert record.no_active_infection_status is False
    record.active_malignancies = []
    _compute_derived_fields(record)
    assert record.no_other_active_malignancies is True


def test_audit_reports_a_later_contradiction(editor):
    record, _ = editor
    assert answer(editor, INFECTION, 'confirm_negative').status_code == 200
    record.active_infection_status = True
    record.save(update_fields=['active_infection_status'])
    out = StringIO()
    call_command('audit_explicit_negative_answers', stdout=out)
    output = out.getvalue()
    assert 'active infection is recorded' in output
    assert f'person={record.person_id}' in output
    record.refresh_from_db()
    assert record.no_active_infection_status is True


def test_patient_can_answer_own_record_but_not_someone_elses(editor):
    record, client = editor
    patient = Identity.objects.create_user(email='negative-patient@example.test')
    PatientUser.objects.create(identity=patient, person=record.person)
    client.force_authenticate(patient)
    assert answer(editor, INFECTION, 'confirm_negative').status_code == 200
    other = PatientRecordFactory()
    response = client.post(
        f'/api/v1/patient-records/{other.person_id}/negative-answer/',
        {'field': INFECTION, 'action': 'confirm_negative',
         'expected_value': None, 'expected_source': None}, format='json')
    assert response.status_code in (403, 404)


def test_analyst_cannot_confirm(editor):
    record, client = editor
    analyst = Identity.objects.create_user(email='negative-analyst@example.test')
    GroupAccess.objects.create(identity=analyst, org=record.organization, role='analyst')
    client.force_authenticate(analyst)
    assert answer(editor, INFECTION, 'confirm_negative').status_code == 403
    assert not ExplicitNegativeAnswer.objects.exists()


def test_unknown_field_is_refused(editor):
    record, client = editor
    response = client.post(
        f'/api/v1/patient-records/{record.person_id}/negative-answer/',
        {'field': 'no_such_answer', 'action': 'confirm_negative'}, format='json')
    assert response.status_code == 400


def test_shipped_formulas_are_inactive():
    import pathlib
    fixture = pathlib.Path(__file__).resolve().parents[1] / 'omop_core/data/field_curation_v1.json'
    data = json.loads(fixture.read_text())
    shipped = {row['field_name']: row['is_active'] for row in data['formulas']}
    assert shipped[INFECTION] is False
    assert shipped[MALIGNANCIES] is False


def test_malformed_post_is_a_400_not_a_500(editor):
    record, client = editor
    url = f'/api/v1/patient-records/{record.person_id}/negative-answer/'
    for body in ({'field': [], 'action': 'confirm_negative'}, []):
        response = client.post(url, body, format='json')
        assert response.status_code == 400, (body, response.status_code)
    assert not ExplicitNegativeAnswer.objects.exists()


def test_full_recompute_preserves_the_attestation(editor):
    record, _ = editor
    assert answer(editor, INFECTION, 'confirm_negative').status_code == 200
    record.active_infection_status = True
    record.save(update_fields=['active_infection_status'])
    from omop_core.services.patient_record_service import recompute_patient_record_fields
    record = recompute_patient_record_fields(record)
    assert record.no_active_infection_status is True


def test_an_explicit_answer_formula_cannot_be_active():
    row = FieldFormula.objects.create(
        field_name=INFECTION, formula='@not(active_infection_status)', is_active=False,
    )
    row.is_active = True
    with pytest.raises(IntegrityError):
        row.save()


def test_formula_api_refuses_an_explicit_answer_field():
    from patient_portal.api.serializers import FieldFormulaSerializer
    serializer = FieldFormulaSerializer(data={
        'field_name': INFECTION,
        'formula': '@not(active_infection_status)',
        'is_active': False,
    })
    assert not serializer.is_valid()
