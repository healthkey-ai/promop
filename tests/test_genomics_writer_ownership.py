"""The genomic writer refuses changes outside the caller's authoring scope (#1918).

CB pre-checks finding ownership before calling the shared writer; these tests
cover the writer's own contract, independent of that pre-check.
"""
import pytest
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.test import APIClient

from omop_core.models import Measurement
from omop_core.services.genomics import delete_variant, save_variant
from patient_portal.models import Identity, PatientUser
from tests.factories import ConceptFactory, PatientRecordFactory, PersonFactory
from tests.test_genomics_crud import setup  # noqa: F401

pytestmark = pytest.mark.django_db

PATIENT_SELF_REPORT = 32865
CLINICAL = 32817


def make_finding(person, type_concept_id, variant='Source finding'):
    return save_variant(
        person, {'gene': 'TP53', 'variant': variant}, type_concept_id=type_concept_id,
    )


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def test_patient_scope_cannot_update_an_ehr_authored_finding(setup):
    person, _, _ = setup
    ConceptFactory(concept_id=CLINICAL)
    ConceptFactory(concept_id=PATIENT_SELF_REPORT)
    ehr = make_finding(person, CLINICAL)
    before = Measurement.objects.get(pk=ehr['id'])

    with pytest.raises(ValidationError) as refused:
        save_variant(person, {'gene': 'TP53', 'variant': 'Patient rewrite'},
                     ehr['id'], type_concept_id=PATIENT_SELF_REPORT)

    assert 'clinical source' in str(refused.value.detail)
    after = Measurement.objects.get(pk=ehr['id'])
    assert after.value_as_string == before.value_as_string
    assert after.measurement_type_concept_id == CLINICAL
    assert after.is_erroneous is False


def test_patient_scope_cannot_delete_an_ehr_authored_finding(setup):
    person, _, _ = setup
    ConceptFactory(concept_id=CLINICAL)
    ConceptFactory(concept_id=PATIENT_SELF_REPORT)
    ehr = make_finding(person, CLINICAL)

    with pytest.raises(ValidationError):
        delete_variant(person, ehr['id'], type_concept_id=PATIENT_SELF_REPORT)

    assert Measurement.objects.get(pk=ehr['id']).is_erroneous is False


def test_patient_scope_still_updates_and_deletes_its_own_finding(setup):
    person, _, _ = setup
    ConceptFactory(concept_id=CLINICAL)
    ConceptFactory(concept_id=PATIENT_SELF_REPORT)
    mine = make_finding(person, PATIENT_SELF_REPORT)

    edited = save_variant(person, {'gene': 'TP53', 'variant': 'Patient correction'},
                          mine['id'], type_concept_id=PATIENT_SELF_REPORT)
    assert edited['variant'] == 'Patient correction'
    assert Measurement.objects.get(pk=mine['id']).measurement_type_concept_id == PATIENT_SELF_REPORT

    delete_variant(person, mine['id'], type_concept_id=PATIENT_SELF_REPORT)
    assert Measurement.objects.get(pk=mine['id']).is_erroneous is True


def test_a_finding_from_another_person_is_not_mutable(setup):
    person, _, _ = setup
    ConceptFactory(concept_id=CLINICAL)
    ConceptFactory(concept_id=PATIENT_SELF_REPORT)
    other = PersonFactory()
    PatientRecordFactory(person=other)
    theirs = make_finding(other, CLINICAL)

    with pytest.raises(NotFound):
        save_variant(person, {'gene': 'TP53', 'variant': 'Someone else'},
                     theirs['id'], type_concept_id=CLINICAL)
    with pytest.raises(NotFound):
        delete_variant(person, theirs['id'], type_concept_id=CLINICAL)

    assert Measurement.objects.get(pk=theirs['id']).is_erroneous is False


def test_clinical_scope_may_adopt_a_patient_authored_finding(setup):
    """The privileged path: a clinician correcting a patient-entered value."""
    person, _, _ = setup
    ConceptFactory(concept_id=CLINICAL)
    ConceptFactory(concept_id=PATIENT_SELF_REPORT)
    mine = make_finding(person, PATIENT_SELF_REPORT)

    corrected = save_variant(person, {'gene': 'TP53', 'variant': 'Clinician correction'},
                             mine['id'], type_concept_id=CLINICAL)

    assert corrected['variant'] == 'Clinician correction'
    assert Measurement.objects.get(pk=mine['id']).measurement_type_concept_id == CLINICAL


def test_the_api_returns_a_typed_refusal_for_a_patient_editing_an_ehr_finding(setup):
    person, _, _ = setup
    ConceptFactory(concept_id=CLINICAL)
    ConceptFactory(concept_id=PATIENT_SELF_REPORT)
    ehr = make_finding(person, CLINICAL)
    user = Identity.objects.create_user(email='self@example.test', password='pw')
    PatientUser.objects.create(identity=user, person=person)
    client = client_for(user)
    url = f'/api/v1/patient-records/{person.pk}/genomics/{ehr["id"]}/'

    patched = client.patch(url, {'gene': 'TP53', 'variant': 'Patient rewrite'}, format='json')
    assert patched.status_code == 400, patched.data
    deleted = client.delete(url)
    assert deleted.status_code == 400, deleted.data
    assert Measurement.objects.get(pk=ehr['id']).is_erroneous is False


def test_replace_rolls_back_a_patient_edit_before_a_clinical_refusal(setup):
    """A mixed PATCH is one transaction: the refusal undoes the earlier save."""
    from omop_core.services.genomics import list_variants, replace_variants

    person, record, _ = setup
    ConceptFactory(concept_id=CLINICAL)
    ConceptFactory(concept_id=PATIENT_SELF_REPORT)
    mine = make_finding(person, PATIENT_SELF_REPORT, variant='Patient original')
    ehr = make_finding(person, CLINICAL, variant='Imported original')
    before = {(v['id']): v['variant'] for v in list_variants(person)}

    with pytest.raises(ValidationError):
        replace_variants(person, [
            {**mine, 'variant': 'Patient edit'},
            {**ehr, 'variant': 'Patient rewrite of an import'},
        ], type_concept_id=PATIENT_SELF_REPORT)

    record.refresh_from_db()
    assert {v['id']: v['variant'] for v in list_variants(person)} == before
    assert {v['variant'] for v in record.genetic_mutations} == {
        'Patient original', 'Imported original',
    }
