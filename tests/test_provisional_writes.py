"""Generate OMOP writes, promotes and reverts provisional rows (phase 1b, #2530).

A provisional row is a clinical row whose concept came from a *proposed*
mapping: it carries the proposal's confidence as a whole percentage. It follows
its proposal: approval makes it real, rejection or a moved proposal takes it
back to unresolved (concept 0, no confidence), and a real row for the same
event always wins over it.
"""
import pytest
from rest_framework.test import APIClient

from omop_core.mapping.code_resolution import (
    NO_MATCHING_CONCEPT_ID, _create_missing_clinical_rows, promote_provisional_rows,
    provisional_proposal, repoint_clinical_rows, resolve_person_source_codes,
    revert_provisional_rows,
)
from omop_core.models import (
    ConditionOccurrence, Measurement, PatientRecord, PatientSourceCode,
    SourceCodeConceptMapping,
)
from omop_core.services import repoint_jobs
from tests.factories import (
    ConceptClassFactory, ConceptFactory, ConditionOccurrenceFactory, DomainFactory,
    MeasurementFactory, OrganizationFactory, PatientRecordFactory, PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db

CODE = 'GLU-1'


@pytest.fixture(autouse=True)
def provisional_on(settings):
    settings.PROVISIONAL_DATA_ENABLED = True
    settings.PROVISIONAL_CONFIDENCE_FLOOR = 70
    settings.CELERY_BROKER_URL = ''


@pytest.fixture()
def world():
    """Concept 0, the EHR type concept, a person with a record, and two standard
    Measurement concepts: P (the proposal) and C (what a curator may choose)."""
    ConceptFactory(concept_id=NO_MATCHING_CONCEPT_ID, concept_code='0',
                   concept_name='No matching concept', standard_concept=None)
    ConceptFactory(concept_id=32817, concept_code='32817', concept_name='EHR')
    person = PersonFactory()
    PatientRecordFactory(person=person)
    return {
        'person': person,
        'proposed': ConceptFactory(concept_name='Proposed glucose'),
        'chosen': ConceptFactory(concept_name='Chosen glucose'),
    }


def proposal(target, code=CODE, confidence=0.85, **overrides):
    return SourceCodeConceptMapping.objects.create(**{
        'source_code': code, 'source_vocabulary_id': 'Local',
        'source_code_description': code, 'domain_id': 'Measurement',
        'omop_table': 'measurement', 'target_concept': target, 'status': 'proposed',
        'suggestion_confidence': confidence, **overrides,
    })


def approved(target, code=CODE, **overrides):
    return proposal(target, code, confidence=None, status='approved', **overrides)


def row(person, concept, confidence=None, code=CODE, **overrides):
    return MeasurementFactory(**{
        'person': person, 'measurement_concept_id': concept, 'measurement_source_value': code,
        'measurement_date': '2024-03-01', 'value_as_number': 5, 'confidence': confidence,
        **overrides,
    })


def state(pk):
    stored = Measurement.objects.get(pk=pk)
    return stored.measurement_concept_id, stored.confidence


def source_code(person, value=CODE):
    return PatientSourceCode.objects.create(
        person=person, source_value=value, omop_table='measurement',
        source_vocabulary_id='Local')


# --- which proposals qualify ------------------------------------------------------

@pytest.mark.parametrize(('confidence', 'percent'), [(0.85, 85), (0.7, 70), (1.0, 100)])
def test_a_ranked_proposal_at_or_above_the_floor_qualifies(world, confidence, percent):
    proposal(world['proposed'], confidence=confidence)
    found = provisional_proposal(CODE, 'measurement')
    assert found == (world['proposed'], percent)


@pytest.mark.parametrize('why', ['unranked', 'below the floor', 'rejected', 'placeholder',
                                 'wrong domain', 'organization scoped', 'conflicting'])
def test_a_proposal_that_does_not_qualify_writes_nothing(world, why):
    person = world['person']
    target = world['proposed']
    kwargs = {}
    if why == 'unranked':
        kwargs['confidence'] = None
    elif why == 'below the floor':
        kwargs['confidence'] = 0.69
    elif why == 'rejected':
        kwargs['status'] = 'rejected'
    elif why == 'placeholder':
        target = ConceptFactory(standard_concept=None)   # an HK-* placeholder is not standard
    elif why == 'wrong domain':
        domain = DomainFactory(domain_id='Condition', domain_name='Condition')
        target = ConceptFactory(domain=domain, concept_class=ConceptClassFactory(
            concept_class_id='Clinical Finding'))
    elif why == 'organization scoped':
        kwargs['organization'] = OrganizationFactory()
    proposal(target, **kwargs)
    if why == 'conflicting':
        proposal(world['chosen'], source_vocabulary_id='Other')
    source_code(person)

    created, details = _create_missing_clinical_rows(person)

    assert created == 0 and details == []
    assert not Measurement.objects.filter(person=person).exists()


def test_the_switch_off_writes_nothing(world, settings):
    settings.PROVISIONAL_DATA_ENABLED = False
    proposal(world['proposed'])
    source_code(world['person'])
    assert _create_missing_clinical_rows(world['person']) == (0, [])


# --- Generate OMOP creates rows ----------------------------------------------------

def test_generate_omop_creates_a_provisional_row_for_a_proposal(world):
    person = world['person']
    proposal(world['proposed'], confidence=0.82)
    source_code(person)

    result = resolve_person_source_codes(person)

    stored = Measurement.objects.get(person=person)
    assert (stored.measurement_concept_id, stored.confidence) == (world['proposed'].pk, 82)
    assert result['created'] == 1 and result['provisional'] == 1
    detail = result['code_details'][0]
    assert detail['outcome'] == 'provisional' and detail['confidence'] == 82


def test_an_approved_mapping_still_creates_a_real_row(world):
    person = world['person']
    approved(world['chosen'])
    proposal(world['proposed'], source_vocabulary_id='Other')
    source_code(person)

    result = resolve_person_source_codes(person)

    stored = Measurement.objects.get(person=person)
    assert (stored.measurement_concept_id, stored.confidence) == (world['chosen'].pk, None)
    assert result['provisional'] == 0


# --- re-resolving rows already stored ----------------------------------------------

def test_resolve_makes_an_unresolved_row_provisional_and_leaves_real_ones(world):
    person = world['person']
    proposal(world['proposed'], confidence=0.9)
    unresolved = row(person, NO_MATCHING_CONCEPT_ID)
    real = row(person, world['chosen'].pk, measurement_date='2024-04-01')

    result = resolve_person_source_codes(person)

    assert state(unresolved.pk) == (world['proposed'].pk, 90)
    assert state(real.pk) == (world['chosen'].pk, None)
    assert result['provisional'] == 1 and result['resolved'] == 0


def test_resolve_follows_a_proposal_that_moved_and_updates_its_confidence(world):
    person = world['person']
    mapping = proposal(world['proposed'], confidence=0.9)
    stored = row(person, world['proposed'].pk, confidence=90)

    mapping.suggestion_confidence = 0.75
    mapping.save()
    resolve_person_source_codes(person)
    assert state(stored.pk) == (world['proposed'].pk, 75)

    mapping.target_concept = world['chosen']
    mapping.save()
    resolve_person_source_codes(person)
    assert state(stored.pk) == (world['chosen'].pk, 75)


@pytest.mark.parametrize('why', ['rejected', 'below the floor', 'switch off', 'deleted'])
def test_resolve_takes_back_rows_whose_proposal_no_longer_qualifies(world, settings, why):
    person = world['person']
    mapping = proposal(world['proposed'], confidence=0.9)
    stored = row(person, world['proposed'].pk, confidence=90)
    real = row(person, world['proposed'].pk, measurement_date='2024-04-01')
    if why == 'rejected':
        mapping.status = 'rejected'
        mapping.save()
    elif why == 'below the floor':
        mapping.suggestion_confidence = 0.4
        mapping.save()
    elif why == 'switch off':
        settings.PROVISIONAL_DATA_ENABLED = False
    else:
        mapping.delete()

    result = resolve_person_source_codes(person)

    assert state(stored.pk) == (NO_MATCHING_CONCEPT_ID, None)
    assert state(real.pk) == (world['proposed'].pk, None), 'a real row at the same concept stays'
    assert result['reverted'] == 1


@pytest.mark.parametrize('chosen', ['same', 'other'])
def test_resolve_promotes_provisional_rows_on_approval(world, chosen):
    person = world['person']
    mapping = proposal(world['proposed'], confidence=0.9)
    stored = row(person, world['proposed'].pk, confidence=90)
    target = world['proposed'] if chosen == 'same' else world['chosen']
    mapping.target_concept = target
    mapping.status = 'approved'
    mapping.deferred_repoint_concept_ids = [world['proposed'].pk] if chosen == 'other' else []
    mapping.save()

    result = resolve_person_source_codes(person)

    assert state(stored.pk) == (target.pk, None)
    assert result['resolved'] == 1


@pytest.mark.parametrize('promoted_first', [True, False])
def test_a_real_row_beats_its_provisional_twin_when_resolve_promotes(world, promoted_first):
    person = world['person']
    if promoted_first:
        twin = row(person, world['proposed'].pk, confidence=90)
        real = row(person, world['chosen'].pk)
    else:
        real = row(person, world['chosen'].pk)
        twin = row(person, world['proposed'].pk, confidence=90)
    mapping = approved(world['chosen'])
    mapping.deferred_repoint_concept_ids = [world['proposed'].pk]
    mapping.save()

    resolve_person_source_codes(person)

    assert list(Measurement.objects.filter(person=person).values_list('pk', flat=True)) == [real.pk]
    assert state(real.pk) == (world['chosen'].pk, None)
    assert not Measurement.objects.filter(pk=twin.pk).exists()


# --- units -------------------------------------------------------------------------

def test_a_provisional_row_is_converted_when_promoted_not_when_written(world):
    from decimal import Decimal
    from patient_portal.api.views import _normalize_bulk_measurement_rows
    person = world['person']
    ucum = VocabularyFactory(vocabulary_id='UCUM')
    mmol = ConceptFactory(vocabulary=ucum, concept_code='mmol/L', concept_name='mmol/L')
    mapping = approved(world['proposed'], destination_unit_concept=mmol)
    provisional = row(person, world['proposed'].pk, confidence=90,
                      value_as_number=100, unit_source_value='umol/L')
    real = row(person, world['proposed'].pk, measurement_date='2024-04-01',
               value_as_number=100, unit_source_value='umol/L')

    _normalize_bulk_measurement_rows(Measurement, [provisional.pk, real.pk])

    assert Measurement.objects.get(pk=real.pk).value_as_number == Decimal('0.1')
    assert Measurement.objects.get(pk=provisional.pk).value_as_number == 100

    promote_provisional_rows(mapping=mapping)

    assert Measurement.objects.get(pk=provisional.pk).value_as_number == Decimal('0.1')
    assert state(provisional.pk) == (world['proposed'].pk, None)


# --- approval, rejection, a moved proposal ----------------------------------------

@pytest.mark.parametrize('promoted_first', [True, False])
def test_repoint_promotes_and_keeps_the_real_row(world, promoted_first):
    person = world['person']
    if promoted_first:
        twin = row(person, world['proposed'].pk, confidence=90)
        real = row(person, world['chosen'].pk)
    else:
        real = row(person, world['chosen'].pk)
        twin = row(person, world['proposed'].pk, confidence=90)
    alone = row(person, world['proposed'].pk, confidence=60, measurement_date='2024-05-01')
    mapping = approved(world['chosen'])

    result = repoint_clinical_rows(
        mapping=mapping, old_concept_id=world['proposed'].pk,
        new_concept_id=world['chosen'].pk)

    assert result['rows_collapsed'] == 1 and result['rows_promoted'] == 1
    assert not Measurement.objects.filter(pk=twin.pk).exists()
    assert state(real.pk) == (world['chosen'].pk, None)
    assert state(alone.pk) == (world['chosen'].pk, None)
    assert PatientRecord.objects.get(person=person).derivation_version == 0


def test_a_repoint_to_concept_0_leaves_no_confidence_on_the_row(world):
    stored = row(world['person'], world['proposed'].pk, confidence=90)
    mapping = proposal(world['proposed'])

    repoint_clinical_rows(
        mapping=mapping, old_concept_id=world['proposed'].pk,
        new_concept_id=NO_MATCHING_CONCEPT_ID)

    assert state(stored.pk) == (NO_MATCHING_CONCEPT_ID, None)


def test_approval_at_the_proposed_concept_promotes_in_place(world):
    person = world['person']
    stored = row(person, world['proposed'].pk, confidence=90)
    real = row(person, world['proposed'].pk, measurement_date='2024-04-01')
    mapping = approved(world['proposed'])

    result = promote_provisional_rows(mapping=mapping)

    assert result['rows_promoted'] == 1 and result['person_ids'] == {person.person_id}
    assert state(stored.pk) == (world['proposed'].pk, None)
    assert state(real.pk) == (world['proposed'].pk, None)
    assert PatientRecord.objects.get(person=person).derivation_version == 0


@pytest.mark.parametrize('real_has_lower_pk', [True, False])
def test_promotion_in_place_folds_a_duplicate_into_the_real_row(world, real_has_lower_pk):
    person = world['person']
    if real_has_lower_pk:
        real = row(person, world['proposed'].pk)
        twin = row(person, world['proposed'].pk, confidence=90)
    else:
        twin = row(person, world['proposed'].pk, confidence=90)
        real = row(person, world['proposed'].pk)
    mapping = approved(world['proposed'])

    result = promote_provisional_rows(mapping=mapping)

    assert result['rows_collapsed'] == 1
    assert list(Measurement.objects.filter(person=person).values_list('pk', flat=True)) == [real.pk]
    assert not Measurement.objects.filter(pk=twin.pk).exists()


def test_revert_takes_provisional_rows_to_unresolved_and_spares_real_ones(world):
    person = world['person']
    mapping = proposal(world['proposed'])
    provisional = row(person, world['proposed'].pk, confidence=90)
    real = row(person, world['proposed'].pk, measurement_date='2024-04-01')
    other_code = row(person, world['proposed'].pk, confidence=90, code='OTHER')

    result = revert_provisional_rows(mapping=mapping, concept_id=world['proposed'].pk)

    assert result['rows_reverted'] == 1
    assert state(provisional.pk) == (NO_MATCHING_CONCEPT_ID, None)
    assert state(real.pk) == (world['proposed'].pk, None)
    assert state(other_code.pk) == (world['proposed'].pk, 90)
    assert PatientRecord.objects.get(person=person).derivation_version == 0


def test_revert_for_one_patient_leaves_the_others(world):
    other = PersonFactory()
    mapping = proposal(world['proposed'])
    mine = row(world['person'], world['proposed'].pk, confidence=90)
    theirs = row(other, world['proposed'].pk, confidence=90)

    revert_provisional_rows(
        mapping=mapping, concept_id=world['proposed'].pk, person_id=world['person'].person_id)

    assert state(mine.pk) == (NO_MATCHING_CONCEPT_ID, None)
    assert state(theirs.pk) == (world['proposed'].pk, 90)


def test_a_hospital_scoped_revert_moves_only_that_hospitals_rows(world):
    organization = OrganizationFactory()
    mapping = proposal(world['proposed'], organization=organization)
    unattributed = row(world['person'], world['proposed'].pk, confidence=90)

    result = revert_provisional_rows(mapping=mapping, concept_id=world['proposed'].pk)

    assert result['rows_reverted'] == 0
    assert state(unattributed.pk) == (world['proposed'].pk, 90)


# --- the curator's save and the repoint job ----------------------------------------

@pytest.fixture
def client(django_user_model):
    instance = APIClient()
    instance.force_authenticate(
        django_user_model.objects.create_user(email='provisional@test.com', is_staff=True))
    return instance


def patch(client, mapping, body):
    response = client.patch(f'/api/v1/code-mappings/{mapping.pk}/', body, format='json')
    assert response.status_code == 200, response.data
    return response


def test_rejecting_a_proposal_takes_its_rows_back(client, world):
    mapping = proposal(world['proposed'])
    stored = row(world['person'], world['proposed'].pk, confidence=90)

    patch(client, mapping, {'status': 'rejected'})

    assert state(stored.pk) == (NO_MATCHING_CONCEPT_ID, None)


def test_pointing_a_proposal_elsewhere_takes_its_rows_back_and_writes_none(client, world):
    mapping = proposal(world['proposed'])
    stored = row(world['person'], world['proposed'].pk, confidence=90)

    patch(client, mapping, {'status': 'proposed', 'destination_concept_id': world['chosen'].pk})

    assert state(stored.pk) == (NO_MATCHING_CONCEPT_ID, None)
    assert not Measurement.objects.filter(measurement_concept=world['chosen']).exists()


def test_a_patient_page_rejection_reverts_that_patient_only(client, world):
    other = PersonFactory()
    mapping = proposal(world['proposed'])
    mine = row(world['person'], world['proposed'].pk, confidence=90)
    theirs = row(other, world['proposed'].pk, confidence=90)

    patch(client, mapping, {'status': 'rejected', 'patient_id': world['person'].person_id})

    assert state(mine.pk) == (NO_MATCHING_CONCEPT_ID, None)
    assert state(theirs.pk) == (world['proposed'].pk, 90)


@pytest.mark.parametrize('chosen', ['same', 'other'])
def test_approval_makes_the_rows_real_once_the_job_runs(
        client, world, django_capture_on_commit_callbacks, chosen):
    mapping = proposal(world['proposed'])
    stored = row(world['person'], world['proposed'].pk, confidence=90)
    unresolved = row(world['person'], NO_MATCHING_CONCEPT_ID, measurement_date='2024-02-01')
    target = world['proposed'] if chosen == 'same' else world['chosen']

    with django_capture_on_commit_callbacks() as callbacks:
        patch(client, mapping, {'status': 'approved', 'destination_concept_id': target.pk})
    assert state(stored.pk) == (world['proposed'].pk, 90), 'the save only queues the move'
    for callback in callbacks:
        callback()

    assert state(stored.pk) == (target.pk, None)
    assert state(unresolved.pk) == (target.pk, None)


def test_the_job_promotes_rows_when_no_concept_has_to_move(world):
    stored = row(world['person'], world['proposed'].pk, confidence=90)
    mapping = approved(world['proposed'], deferred_repoint_concept_ids=[NO_MATCHING_CONCEPT_ID])

    repoint_jobs.run_repoint(mapping.pk)

    assert state(stored.pk) == (world['proposed'].pk, None)


# --- the other writers that move a proposal -----------------------------------------

def test_a_csv_upload_that_moves_a_proposal_takes_its_rows_back(client, world):
    from django.core.files.uploadedfile import SimpleUploadedFile
    proposal(world['proposed'], source_vocabulary_id='VendorLab')
    stored = row(world['person'], world['proposed'].pk, confidence=90)

    response = client.post('/api/v1/code-mappings/upload/', {
        'file': SimpleUploadedFile(
            'codes.csv',
            f'source code,source description,destination concept ID,state\n'
            f'{CODE},Glucose,{world["chosen"].pk},Proposed\n'.encode(),
            content_type='text/csv'),
        'source_vocabulary_id': 'VendorLab', 'provenance': 'catalogue',
    }, format='multipart')
    assert response.status_code == 201, response.data

    assert state(stored.pk) == (NO_MATCHING_CONCEPT_ID, None)


def test_suggest_replacing_a_proposal_takes_the_old_rows_back(monkeypatch, world):
    from omop_core.mapping import suggestions
    domain = DomainFactory(domain_id='Condition', domain_name='Condition')
    vocabulary = VocabularyFactory(vocabulary_id='SNOMED', vocabulary_name='SNOMED')
    klass = ConceptClassFactory(concept_class_id='Clinical Finding')
    old, new = (ConceptFactory(
        concept_name=name, concept_code=code, vocabulary=vocabulary, domain=domain,
        concept_class=klass) for name, code in (('Old pick', '1'), ('New pick', '2')))
    SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='ICD10', source_code='C90.0', domain_id='Condition',
        omop_table='condition', status='proposed', origin='import',
        origin_system='suggest v0.4', suggestion_model_version='v0.4',
        target_concept=old, suggested_target_concept=old, occurrence_count=5,
        suggestion_confidence=0.9)
    stored = ConditionOccurrenceFactory(
        person=world['person'], condition_concept=old, condition_source_value='C90.0',
        confidence=90)
    candidate = {
        'concept_id': new.concept_id, 'concept_name': new.concept_name,
        'concept_code': new.concept_code, 'vocabulary_id': new.vocabulary_id,
        'concept_class_id': new.concept_class_id, 'domain_id': new.domain_id,
        'umls_score': 1.0, 'retrieval': 'umls',
    }
    monkeypatch.setattr(suggestions, 'umls_candidates', lambda *a: ([candidate], 'C1'))
    monkeypatch.setattr(suggestions, 'rank_candidates',
                        lambda value, candidates, *a, **k: (candidates[0], 'ranked', []))

    suggestions.suggest_mappings('condition', strategies=['umls'], min_occurrences=1, resuggest=True)

    stored = ConditionOccurrence.objects.get(pk=stored.pk)
    assert (stored.condition_concept_id, stored.confidence) == (NO_MATCHING_CONCEPT_ID, None)


def test_a_relocated_provisional_row_stays_provisional(world):
    """Re-filing a code under another table moves its rows with their concept and
    confidence; the proposal is the same one, so the rows still follow it."""
    from omop_core.mapping.table_relocation import relocate_clinical_rows
    from omop_core.models import Observation
    mapping = proposal(world['proposed'], omop_table='observation', domain_id='Observation')
    stored = row(world['person'], world['proposed'].pk, confidence=90)

    relocate_clinical_rows(
        mapping=mapping, from_table='measurement', to_table='observation',
        person_id=world['person'].person_id)

    assert not Measurement.objects.filter(pk=stored.pk).exists()
    moved = Observation.objects.get(person=world['person'])
    assert (moved.observation_concept_id, moved.confidence) == (world['proposed'].pk, 90)


# --- review findings (#2587) -------------------------------------------------------

def test_promotion_never_deletes_a_real_row_even_a_keyed_duplicate(world):
    """Two real results that share an event identity but carry their own source
    record keys are two facts; settling a provisional twin must not merge them."""
    person = world['person']
    first = row(person, world['proposed'].pk, source_record_key='epic:a/Observation/1')
    second = row(person, world['proposed'].pk, source_record_key='epic:a/Observation/2')
    twin = row(person, world['proposed'].pk, confidence=90)
    mapping = approved(world['proposed'])

    result = promote_provisional_rows(mapping=mapping)

    assert result['rows_collapsed'] == 1
    assert set(Measurement.objects.filter(person=person).values_list('pk', flat=True)) == {first.pk, second.pk}
    assert not Measurement.objects.filter(pk=twin.pk).exists()


def test_a_code_held_under_a_system_without_a_proposal_gets_no_provisional_row(world):
    """An Epic proposal for flowsheet "6" says nothing about a patient whose "6"
    came from Cerner."""
    person = world['person']
    proposal(world['proposed'], source_vocabulary_id='EpicA')
    PatientSourceCode.objects.create(
        person=person, source_value=CODE, omop_table='measurement', source_vocabulary_id='CernerB')
    unresolved = row(person, NO_MATCHING_CONCEPT_ID)

    resolve_person_source_codes(person)

    assert state(unresolved.pk) == (NO_MATCHING_CONCEPT_ID, None)
    assert Measurement.objects.filter(person=person).count() == 1


def test_a_code_held_under_the_proposing_system_gets_one(world):
    person = world['person']
    proposal(world['proposed'], source_vocabulary_id='EpicA')
    PatientSourceCode.objects.create(
        person=person, source_value=CODE, omop_table='measurement', source_vocabulary_id='EpicA')

    resolve_person_source_codes(person)

    assert Measurement.objects.get(person=person).confidence == 85


def test_every_system_a_code_arrived_under_must_have_a_qualifying_proposal(world):
    person = world['person']
    proposal(world['proposed'], source_vocabulary_id='EpicA')
    proposal(world['proposed'], source_vocabulary_id='CernerB', confidence=0.75)
    for vocabulary in ('EpicA', 'CernerB'):
        PatientSourceCode.objects.create(
            person=person, source_value=CODE, omop_table='measurement',
            source_vocabulary_id=vocabulary)

    resolve_person_source_codes(person)

    assert Measurement.objects.get(person=person).confidence == 75, 'the lowest of the two'


def test_approval_does_not_make_real_the_rows_of_a_person_coded_elsewhere(world):
    person = world['person']
    stored = row(person, world['proposed'].pk, confidence=90)
    for vocabulary in ('EpicA', 'CernerB'):
        PatientSourceCode.objects.create(
            person=person, source_value=CODE, omop_table='measurement',
            source_vocabulary_id=vocabulary)
    mapping = approved(world['proposed'], source_vocabulary_id='EpicA')

    result = promote_provisional_rows(mapping=mapping)

    assert result['rows_promoted'] == 0
    assert state(stored.pk) == (world['proposed'].pk, 90)


def test_approval_and_rejection_leave_another_mappings_rows_alone(world):
    """An import mapping's description ("Glucose") can be another mapping's code."""
    person = world['person']
    stored = row(person, world['proposed'].pk, confidence=90, code='Glucose')
    mapping = proposal(world['proposed'], origin='import', source_code_description='Glucose')

    revert_provisional_rows(mapping=mapping, concept_id=world['proposed'].pk)
    assert state(stored.pk) == (world['proposed'].pk, 90)

    mapping.status = 'approved'
    mapping.save()
    promote_provisional_rows(mapping=mapping)
    assert state(stored.pk) == (world['proposed'].pk, 90)


def test_rejecting_while_the_table_moves_still_reverts_the_rows_where_they_are(client, world):
    mapping = proposal(world['proposed'])
    stored = row(world['person'], world['proposed'].pk, confidence=90)

    patch(client, mapping, {'status': 'rejected', 'omop_table': 'observation',
                            'domain_id': 'Observation'})

    assert state(stored.pk) == (NO_MATCHING_CONCEPT_ID, None)


def test_rejecting_while_the_table_moves_between_tables_that_cannot_relocate(client, world):
    mapping = proposal(world['proposed'])
    stored = row(world['person'], world['proposed'].pk, confidence=90)

    patch(client, mapping, {'status': 'rejected', 'omop_table': 'condition',
                            'domain_id': 'Condition'})

    assert state(stored.pk) == (NO_MATCHING_CONCEPT_ID, None)


def test_resolve_issues_no_writes_for_unmapped_codes_when_nothing_is_provisional(
        world, settings, django_assert_num_queries):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    settings.PROVISIONAL_DATA_ENABLED = False
    person = world['person']
    for index in range(5):
        row(person, NO_MATCHING_CONCEPT_ID, code=f'UNMAPPED-{index}')
    with CaptureQueriesContext(connection) as queries:
        resolve_person_source_codes(person)
    writes = [q['sql'] for q in queries if q['sql'].lstrip().upper().startswith('UPDATE')
              and 'measurement' in q['sql'].lower()]
    assert writes == []
