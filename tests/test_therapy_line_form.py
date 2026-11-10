"""The shared form authors a therapy line, not the column that projects it.

`first_line_*`, `second_line_*` and `later_*` are read back from a persisted
Episode, so a direct column write is accepted and then replaced by the next
derivation (#2106). These tests pin the contract that replaced it: the
descriptor offers the flat line fields, and a write routes to the same service
the therapy-lines editor uses.
"""
from datetime import date

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from omop_core.models import Observation, PatientRecord, TherapyRegimen
from omop_core.services.mappings import (
    CONCEPT_DRUG_EXPOSURE_FIELD,
    CONCEPT_EHR_TYPE,
    CONCEPT_TREATMENT_REGIMEN,
)
from omop_core.services.patient_form import apply_patient_form_patch
from omop_core.services.write_descriptor import build_writable_field_descriptor
from omop_oncology.models import Episode
from tests.factories import (
    ConceptClassFactory, ConceptFactory, DomainFactory, PatientRecordFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db

MYELOMA = 'Multiple myeloma'
REGIMEN = 35806260


def _vocabulary():
    omop = VocabularyFactory(vocabulary_id='OMOP', vocabulary_name='OMOP')
    ConceptFactory(concept_id=0, concept_name='No matching concept', concept_code='0',
                   vocabulary=omop)
    for concept_id, name in ((CONCEPT_TREATMENT_REGIMEN, 'Treatment Regimen'),
                             (CONCEPT_EHR_TYPE, 'EHR'),
                             (CONCEPT_DRUG_EXPOSURE_FIELD, 'drug_exposure_id')):
        ConceptFactory(concept_id=concept_id, concept_name=name,
                       concept_code=str(concept_id), vocabulary=omop)
    hemonc = VocabularyFactory(vocabulary_id='HemOnc', vocabulary_name='HemOnc')
    regimen = ConceptFactory(
        concept_id=REGIMEN, concept_name='VRd', concept_code='1642', vocabulary=hemonc,
        concept_class=ConceptClassFactory(concept_class_id='Regimen'),
        domain=DomainFactory(domain_id='Drug'),
        standard_concept='S',
    )
    TherapyRegimen.objects.create(code='vrd', title='VRd', concept=regimen)
    return regimen


@pytest.fixture
def patient(db):
    _vocabulary()
    return PatientRecordFactory(disease=MYELOMA).person


def _record(person):
    return PatientRecord.objects.get(person=person)


def _lines(person, line_number):
    return Episode.objects.filter(
        person=person, episode_number=line_number,
        episode_concept_id=CONCEPT_TREATMENT_REGIMEN,
    )


def test_descriptor_offers_the_line_fields_as_writable(patient):
    descriptor = build_writable_field_descriptor()

    for field in ('first_line_therapy', 'first_line_date', 'first_line_outcome',
                  'second_line_therapy', 'later_therapy', 'later_date', 'later_outcome'):
        entry = descriptor[field]
        assert entry['writable'] is True, field
        assert entry['target'] == 'patient_record', field
        assert entry['authored_episode']['line_number'] == (
            3 if field.startswith('later') else 2 if field.startswith('second') else 1)
    assert {'value': 'VRd', 'code': 'vrd', 'label': 'VRd'} in descriptor['later_therapy']['options']
    assert descriptor['later_date']['value_kind'] == 'date'
    assert descriptor['later_outcome']['options']
    # The projection of the line stays unwritable: a client cannot write the
    # per-line read model back.
    assert descriptor['later_therapies']['writable'] is False


def test_the_shared_form_authors_a_later_line(patient):
    record, result = apply_patient_form_patch(patient, {
        'later_therapy': 'vrd',
        'later_date': date(2024, 5, 1),
        'later_outcome': 'Partial Response',
    })

    assert set(result.applied) == {'later_therapy', 'later_date', 'later_outcome'}
    assert not result.rejected
    line = _lines(patient, 3).get()
    assert line.episode_start_date == date(2024, 5, 1)
    record.refresh_from_db()
    assert record.later_therapy == 'VRd'
    assert record.later_date == date(2024, 5, 1)
    assert record.later_outcome == 'Partial Response'
    # Only the authored line exists, so the count is of lines present (1) and
    # the line number is what says it is the third.
    assert record.therapy_lines_count == 1
    assert record.line_of_therapy == '3'
    # The answer is derivation's, not a pinned column: nothing claims the field
    # as a hand edit that must survive a refresh.
    assert 'later_therapy' not in (record.user_edited_fields or [])


def test_an_outcome_only_edit_keeps_the_line_and_its_therapy(patient):
    apply_patient_form_patch(patient, {
        'later_therapy': 'vrd', 'later_date': date(2024, 5, 1),
        'later_outcome': 'Partial Response',
    })

    _, result = apply_patient_form_patch(patient, {'later_outcome': 'Complete Response'})

    assert result.applied == ['later_outcome']
    assert _lines(patient, 3).count() == 1
    record = _record(patient)
    assert record.later_therapy == 'VRd'
    assert record.later_date == date(2024, 5, 1)
    assert record.later_outcome == 'Complete Response'


def test_clearing_the_outcome_removes_the_answered_outcome(patient):
    apply_patient_form_patch(patient, {
        'later_therapy': 'vrd', 'later_date': date(2024, 5, 1),
        'later_outcome': 'Partial Response',
    })

    _, result = apply_patient_form_patch(patient, {'later_outcome': ''})

    assert result.applied == ['later_outcome']
    record = _record(patient)
    assert record.later_therapy == 'VRd'
    assert record.later_outcome is None
    assert not Observation.objects.filter(
        person=patient, observation_source_value='LOT-3-outcome').exists()


def test_unknown_therapy_is_rejected_and_writes_nothing(patient):
    _, result = apply_patient_form_patch(patient, {
        'later_therapy': 'not_a_regimen', 'later_date': date(2024, 5, 1),
    })

    assert 'later_therapy' in result.rejected
    assert 'not_a_regimen' in result.rejected['later_therapy']
    assert 'later_date' in result.rejected
    assert not result.applied
    assert not _lines(patient, 3).exists()


def test_a_new_line_requires_its_start_date(patient):
    _, result = apply_patient_form_patch(patient, {'later_therapy': 'vrd'})

    assert 'A therapy line requires a known start_date' in result.rejected['later_therapy']
    assert not _lines(patient, 3).exists()


def test_a_future_line_is_refused(patient):
    _, result = apply_patient_form_patch(patient, {
        'later_therapy': 'vrd', 'later_date': date(2999, 1, 1),
    })

    assert 'planned, not received' in result.rejected['later_therapy']
    assert not _lines(patient, 3).exists()


def test_the_native_record_patch_authors_the_line(patient):
    user = get_user_model().objects.create_superuser(
        email='provider@example.com', password='x')
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.patch(
        f'/api/v1/patient-records/{patient.pk}/',
        {'second_line_therapy': 'vrd', 'second_line_date': '2023-02-01',
         'second_line_outcome': 'CR'},
        format='json',
    )

    assert response.status_code == 200, response.content
    line = _lines(patient, 2).get()
    assert line.episode_start_date == date(2023, 2, 1)
    record = _record(patient)
    assert record.second_line_therapy == 'VRd'
    assert record.second_line_outcome == 'Complete Response'


def test_a_regimen_without_a_concept_is_not_offered(patient):
    """A choice the writer can only refuse is worse than no choice."""
    TherapyRegimen.objects.create(code='no_concept', title='No Concept')

    options = build_writable_field_descriptor()['later_therapy']['options']

    assert all(option['code'] != 'no_concept' for option in options)


def test_echoing_the_record_back_writes_nothing(patient):
    """The editor PATCHes the whole GET body: a line field echoed as its derived
    display name is not an instruction to author the line."""
    apply_patient_form_patch(patient, {
        'later_therapy': 'vrd', 'later_date': date(2024, 5, 1),
        'later_outcome': 'Partial Response',
    })
    record = _record(patient)
    echoed = {field: getattr(record, field) for field in (
        'later_therapy', 'later_date', 'later_outcome', 'first_line_therapy')}

    _, result = apply_patient_form_patch(patient, echoed)

    assert not result.applied
    assert not result.rejected
    record.refresh_from_db()
    assert record.later_therapy == 'VRd'
    assert record.later_date == date(2024, 5, 1)
    assert record.later_outcome == 'Partial Response'


def test_changing_the_therapy_of_a_line_with_a_composition_is_refused(patient):
    """The editor owns the composition; asserting another regimen here would
    leave the stored drug exposures describing the old one."""
    rxnorm = VocabularyFactory(vocabulary_id='RxNorm', vocabulary_name='RxNorm')
    drug = ConceptFactory(concept_id=1301025, concept_name='bortezomib',
                          concept_code='6360', vocabulary=rxnorm)
    from omop_core.services.episode_service import author_therapy_line
    author_therapy_line(patient, line_number=3, drugs=[drug.concept_id],
                        start_date=date(2024, 4, 1))

    _, result = apply_patient_form_patch(patient, {'later_therapy': 'vrd'})

    assert 'therapy-lines editor' in result.rejected['later_therapy']
    record = _record(patient)
    assert record.later_therapy != 'VRd'


def test_a_stale_pending_marker_does_not_hide_the_authored_line(patient):
    """An older direct write pinned the column; the line owns it now."""
    record = _record(patient)
    record.user_edited_fields = ['later_therapy']
    record.later_therapy = 'Old pinned value'
    record.save(update_fields=['user_edited_fields', 'later_therapy'])

    _, result = apply_patient_form_patch(patient, {
        'later_therapy': 'vrd', 'later_date': date(2024, 5, 1),
    })

    assert 'later_therapy' in result.applied
    record.refresh_from_db()
    assert record.later_therapy == 'VRd'
    assert 'later_therapy' not in (record.user_edited_fields or [])


def test_an_outcome_alias_is_stored_as_the_catalog_value(patient):
    apply_patient_form_patch(patient, {
        'later_therapy': 'vrd', 'later_date': date(2024, 5, 1),
    })

    _, result = apply_patient_form_patch(patient, {'later_outcome': 'CR'})

    assert result.applied == ['later_outcome']
    assert _record(patient).later_outcome == 'Complete Response'


def test_a_refused_line_rolls_back_the_rest_of_the_native_patch(patient):
    user = get_user_model().objects.create_superuser(
        email='provider2@example.com', password='x')
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.patch(
        f'/api/v1/patient-records/{patient.pk}/',
        {'later_therapy': 'not_a_regimen', 'ecog_performance_status': 2},
        format='json',
    )

    assert response.status_code == 400
    assert 'later_therapy' in response.json()
    record = _record(patient)
    assert record.ecog_performance_status != 2
    assert not _lines(patient, 3).exists()


def test_echo_with_a_catalog_code_or_concept_id_is_a_no_op(patient):
    """The record holds the concept name, but a client may echo the code or the
    concept id: a spelling it did not change is not an instruction."""
    apply_patient_form_patch(patient, {
        'later_therapy': 'vrd', 'later_date': date(2024, 5, 1),
        'later_outcome': 'Partial Response',
    })

    for value in ('vrd', 'VRd', REGIMEN, str(REGIMEN)):
        _, result = apply_patient_form_patch(patient, {'later_therapy': value})
        assert not result.applied, value
        assert not result.rejected, value

    # The same answer in its catalog code spelling is the record's value.
    _, result = apply_patient_form_patch(patient, {'later_outcome': 'PR'})
    assert not result.applied
    assert not result.rejected
    assert _record(patient).later_outcome == 'Partial Response'


def test_the_composition_guard_follows_the_line_being_written(patient):
    """A composed line elsewhere does not block a different line's therapy."""
    rxnorm = VocabularyFactory(vocabulary_id='RxNorm', vocabulary_name='RxNorm')
    drug = ConceptFactory(concept_id=1301026, concept_name='lenalidomide',
                          concept_code='6360', vocabulary=rxnorm)
    from omop_core.services.episode_service import author_therapy_line
    author_therapy_line(patient, line_number=2, drugs=[drug.concept_id],
                        start_date=date(2024, 4, 1))

    _, result = apply_patient_form_patch(patient, {
        'later_therapy': 'vrd', 'later_date': date(2024, 5, 1),
    })

    assert result.applied and not result.rejected
    assert _record(patient).later_therapy == 'VRd'


def test_options_offer_the_value_the_column_holds(patient):
    """The select preselects the record's derived name, so the option value has
    to be the concept's own name, not the catalog code."""
    options = build_writable_field_descriptor()['later_therapy']['options']

    assert {'value': 'VRd', 'code': 'vrd', 'label': 'VRd'} in options


@pytest.mark.parametrize('bad_date', ['2026-02-30', 'not-a-date'])
def test_an_invalid_line_date_is_a_field_error_not_a_500(patient, bad_date):
    """An impossible date made `parse_date` raise inside the echo comparison
    (500), and a malformed one read as an unchanged echo of a null date."""
    user = get_user_model().objects.create_superuser(
        email=f'provider-{bad_date}@example.com', password='x')
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.patch(
        f'/api/v1/patient-records/{patient.pk}/',
        {'second_line_therapy': 'vrd', 'second_line_date': bad_date},
        format='json',
    )

    assert response.status_code == 400, response.content
    assert not _lines(patient, 2).exists()
    _, result = apply_patient_form_patch(patient, {'later_therapy': 'vrd', 'later_date': bad_date})
    assert 'not a valid date' in result.rejected['later_date']
    assert not _lines(patient, 3).exists()
