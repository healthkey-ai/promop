"""An ETL can write a person's curated lines of therapy in one request (#1969).

The OMOP row endpoints never create an Episode, so every imported patient had an
empty therapy tab. The lines are known upstream; this is how they get in.
"""
from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APIClient

from omop_core.models import (
    Concept, ConceptClass, ConceptSynonym, DrugExposure, Observation, PatientRecord,
    ProvenanceRecord, RegimenMappingGap, TherapyRegimen,
)
from omop_core.services.episode_service import author_therapy_line
from omop_core.services.therapy_transactions import CONCURRENT_EDIT_MESSAGE, TherapyLineWriteConflict
from omop_oncology.models import Episode, EpisodeEvent, TherapyLineAssertion
from tests.factories import ConceptFactory, PatientRecordFactory, VocabularyFactory
from tests.test_episode_service import _seed_episode_writer_concepts

pytestmark = pytest.mark.django_db

URL = '/api/v1/therapy-lines/bulk_upsert/'
ETL_TOKEN = 'etl-test-secret-000000000000000000000'
GRANTS = {
    # The scopes staging's ETL credential holds (render.yaml).
    'etl': {'token': ETL_TOKEN, 'scopes': 'patient/*.read system/etl.write'},
}
etl_settings = override_settings(SERVICE_AUTH_TOKENS=GRANTS, SERVICE_AUTH_TOKEN='')

# The three validated lines the ticket reports for person 16813.
TICKET_LINES = [
    {'line_number': 1, 'start_date': '2010-08-11', 'end_date': '2012-11-15', 'ongoing': False,
     'regimen': 'DVT-PACE', 'outcome': 'stringent complete response'},
    {'line_number': 2, 'start_date': '2021-01-01', 'end_date': '2021-04-14', 'ongoing': False,
     'regimen': 'R', 'outcome': 'stable disease'},
    {'line_number': 3, 'start_date': '2021-07-15', 'ongoing': True,
     'regimen': 'Dara-CyPd', 'outcome': 'stringent complete response'},
]


@pytest.fixture
def person():
    _seed_episode_writer_concepts()
    return PatientRecordFactory(disease='multiple myeloma').person


@pytest.fixture
def etl():
    with etl_settings:
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f'Bearer {ETL_TOKEN}')
        yield api


def _post(api, person, lines, **extra):
    return api.post(URL, {'person': person.person_id, 'lines': lines}, format='json', **extra)


def _lines(person):
    return {e.episode_number: e for e in Episode.objects.filter(person=person, episode_number__isnull=False)}


def _outcome(person, n):
    return Observation.objects.get(person=person, observation_source_value=f'LOT-{n}-outcome').value_as_string


def _hemonc_regimen(concept_id, name):
    vocab = VocabularyFactory(vocabulary_id='HemOnc', vocabulary_name='HemOnc')
    regimen_class, _ = ConceptClass.objects.get_or_create(
        concept_class_id='Regimen', defaults={'concept_class_name': 'Regimen', 'concept_class_concept_id': 0},
    )
    return ConceptFactory(concept_id=concept_id, concept_name=name, concept_code=str(concept_id),
                           vocabulary=vocab, concept_class=regimen_class, standard_concept='S')


def test_quarantine_hash_fallback_does_not_alias_a_natural_slug(person):
    import hashlib
    from omop_core.mapping.therapy import get_or_create_quarantine_regimen

    digest = hashlib.sha1(b'a b', usedforsecurity=False).hexdigest()[:8]
    labels = ['a+b', f'a b {digest}', 'a b']
    concepts = [get_or_create_quarantine_regimen(label) for label in labels]
    assert len({concept.pk for concept in concepts}) == 3
    assert [concept.concept_name for concept in concepts] == labels
    assert [get_or_create_quarantine_regimen(label).pk for label in labels] == [
        concept.pk for concept in concepts
    ]


def test_the_ticket_s_three_lines_land_and_reach_the_record(etl, person):
    response = _post(etl, person, TICKET_LINES)

    assert response.status_code == 200, response.data
    assert (response.data['created'], response.data['updated']) == (3, 0)
    lines = _lines(person)
    assert sorted(lines) == [1, 2, 3]
    assert (lines[1].episode_start_date, lines[1].episode_end_date) == (date(2010, 8, 11), date(2012, 11, 15))
    assert lines[3].episode_end_date is None
    assert [lines[n].episode_source_value for n in (1, 2, 3)] == ['DVT-PACE', 'R', 'Dara-CyPd']
    # Outcomes are stored in the catalog's spelling, whatever case the source used.
    assert _outcome(person, 1) == 'Stringent Complete Response'
    assert _outcome(person, 2) == 'Stable Disease'
    # An ETL import says so on the assertion, distinct from patient authoring.
    assert set(TherapyLineAssertion.objects.filter(episode__person=person)
               .values_list('source', flat=True)) == {'import'}
    # No drug the source never named is invented.
    assert not DrugExposure.objects.filter(person=person).exists()

    record = PatientRecord.objects.get(person=person)
    assert record.therapy_lines_count == 3
    assert record.first_line_therapy == 'DVT-PACE'
    assert record.first_line_outcome == 'Stringent Complete Response'
    assert record.second_line_therapy == 'R'
    assert record.second_line_end_date == date(2021, 4, 14)


def test_the_editor_endpoint_still_refuses_the_etl_credential(etl, person):
    """Why this endpoint exists: the per-line POST is a patient's write."""
    response = etl.post('/api/v1/therapy-lines/', {
        'person': person.person_id, 'line_number': 1, 'start_date': '2010-08-11',
        'composition_unknown': True,
    }, format='json')
    assert response.status_code == 403


def test_a_re_run_converges_instead_of_duplicating(etl, person):
    assert _post(etl, person, TICKET_LINES).status_code == 200
    again = _post(etl, person, TICKET_LINES)

    assert again.status_code == 200, again.data
    assert (again.data['created'], again.data['updated']) == (0, 3)
    assert Episode.objects.filter(person=person).count() == 3
    assert Observation.objects.filter(person=person, observation_source_value__endswith='-outcome').count() == 3
    assert [line['episode_id'] for line in again.data['lines']] == [_lines(person)[n].episode_id for n in (1, 2, 3)]


def test_a_correction_moves_dates_and_clears_what_the_source_cleared(etl, person):
    _post(etl, person, TICKET_LINES)

    response = _post(etl, person, [
        # Line 3 has now ended; line 2's outcome was withdrawn upstream.
        {'line_number': 3, 'start_date': '2021-07-15', 'end_date': '2024-02-01', 'regimen': 'Dara-CyPd'},
        {'line_number': 2, 'start_date': '2021-01-05', 'end_date': '2021-04-14', 'outcome': None},
    ])

    assert response.status_code == 200, response.data
    lines = _lines(person)
    assert lines[3].episode_end_date == date(2024, 2, 1)
    assert lines[2].episode_start_date == date(2021, 1, 5)
    assert not Observation.objects.filter(person=person, observation_source_value='LOT-2-outcome').exists()
    # Omitted fields are left as stored: line 3's outcome and line 2's name.
    assert _outcome(person, 3) == 'Stringent Complete Response'
    assert lines[2].episode_source_value == 'R'
    # A line the request does not name is untouched.
    assert lines[1].episode_end_date == date(2012, 11, 15)

    reopened = _post(etl, person, [{'line_number': 3, 'start_date': '2021-07-15', 'ongoing': True}])
    assert reopened.status_code == 200, reopened.data
    assert _lines(person)[3].episode_end_date is None


def test_a_curated_reference_regimen_resolves_to_its_concept(etl, person):
    dara = _hemonc_regimen(37_557_700, 'Daratumumab, Cyclophosphamide, Pomalidomide, Dexamethasone')
    TherapyRegimen.objects.create(code='dara_cypd', title='Dara-CyPd', concept=dara)

    response = _post(etl, person, [TICKET_LINES[2] | {'line_number': 1}])

    assert response.status_code == 200, response.data
    assert response.data['lines'][0]['regimen_match'] == 'reference'
    assert response.data['lines'][0]['regimen_concept_id'] == dara.concept_id
    record = PatientRecord.objects.get(person=person)
    assert record.first_line_therapy_id == dara.concept_id
    assert record.therapy_ids_provenance['first_line_therapy_id']['origin'] == 'asserted'
    assert not RegimenMappingGap.objects.filter(normalized_name='dara-cypd').exists()


def test_an_abbreviation_is_never_guessed_from_a_hemonc_synonym(etl, person):
    """HealthTree's myeloma "R" is lenalidomide; HemOnc's "R" is rituximab."""
    rituximab = _hemonc_regimen(35_803_432, 'Rituximab monotherapy')
    english = ConceptFactory(concept_name='English language', concept_code='4180186')
    ConceptSynonym.objects.create(concept=rituximab, concept_synonym_name='R', language_concept=english)

    response = _post(etl, person, [TICKET_LINES[1] | {'line_number': 1}])

    assert response.status_code == 200, response.data
    line = response.data['lines'][0]
    assert line['regimen_match'] == 'unmatched'
    assert line['regimen_concept_id'] != rituximab.concept_id
    assert Concept.objects.get(pk=line['regimen_concept_id']).vocabulary_id == 'HK-Regimen'
    gap = RegimenMappingGap.objects.get(source_system='therapy-lines-api', normalized_name='r')
    assert gap.quarantine_concept_id == line['regimen_concept_id']
    record = PatientRecord.objects.get(person=person)
    assert record.first_line_therapy == 'R'
    assert record.first_line_therapy_id is None


def test_an_exact_hemonc_name_and_an_explicit_concept_resolve(etl, person):
    vrd = _hemonc_regimen(35_806_260, 'VRd')
    kd = _hemonc_regimen(35_806_261, 'Carfilzomib and Dexamethasone')

    response = _post(etl, person, [
        {'line_number': 1, 'start_date': '2019-01-01', 'end_date': '2019-06-01', 'regimen': 'vrd'},
        {'line_number': 2, 'start_date': '2020-01-01', 'regimen': 'Kd', 'regimen_concept_id': kd.concept_id},
    ])

    assert response.status_code == 200, response.data
    assert [(line['regimen_match'], line['regimen_concept_id']) for line in response.data['lines']] == [
        ('hemonc', vrd.concept_id), ('concept_id', kd.concept_id)]
    assert _lines(person)[2].episode_source_value == 'Kd'


@pytest.mark.parametrize('lines,field', [
    ([TICKET_LINES[0] | {'outcome': 'cured'}], 'outcome'),
    ([TICKET_LINES[2] | {'end_date': '2024-01-01'}], 'end_date'),
    ([TICKET_LINES[0] | {'end_date': '2009-01-01'}], 'end_date'),
    ([TICKET_LINES[0] | {'start_date': str(date.today() + timedelta(days=1))}], 'start_date'),
    ([TICKET_LINES[0], TICKET_LINES[0] | {'regimen': 'VRd'}], 'line_number'),
    # Naming the primary cancer is the same line as naming none.
    ([TICKET_LINES[0], TICKET_LINES[0] | {'disease': 'Multiple Myeloma'}], 'line_number'),
    ([TICKET_LINES[0] | {'disease': 'multiple myeloma'}, TICKET_LINES[0]], 'line_number'),
    ([{'line_number': 1, 'regimen': 'VRd'}], 'start_date'),
])
def test_an_invalid_line_rejects_the_request_and_names_the_line(etl, person, lines, field):
    response = _post(etl, person, [TICKET_LINES[1] | {'line_number': 9}] + lines)

    assert response.status_code == 400
    errors = response.data['lines']
    assert not errors[0]                       # the valid line is not blamed
    assert field in errors[-1]
    assert not Episode.objects.filter(person=person).exists()


def test_an_explicit_concept_must_be_a_live_regimen(etl, person):
    diagnosis = ConceptFactory(concept_name='Multiple myeloma', concept_code='109989006')
    retired = _hemonc_regimen(35_806_262, 'Old regimen')
    Concept.objects.filter(pk=retired.pk).update(invalid_reason='D')

    for concept_id in (diagnosis.concept_id, retired.concept_id, 999_999_999):
        response = _post(etl, person, [TICKET_LINES[0] | {'regimen_concept_id': concept_id}])
        assert response.status_code == 400, response.data
        assert 'regimen_concept_id' in response.data['lines'][0]
    assert not Episode.objects.filter(person=person).exists()


def test_a_quarantined_regimen_id_the_api_returned_is_accepted_back(etl, person):
    first = _post(etl, person, [TICKET_LINES[2] | {'line_number': 1}])
    quarantined = first.data['lines'][0]['regimen_concept_id']

    again = _post(etl, person, [TICKET_LINES[2] | {'line_number': 1, 'regimen_concept_id': quarantined}])

    assert again.status_code == 200, again.data
    assert again.data['lines'][0]['regimen_concept_id'] == quarantined


def test_a_curated_regimen_with_a_retired_concept_is_not_asserted(etl, person):
    retired = _hemonc_regimen(37_557_701, 'Daratumumab, Cyclophosphamide, Pomalidomide, Dexamethasone')
    Concept.objects.filter(pk=retired.pk).update(invalid_reason='U')
    TherapyRegimen.objects.create(code='dara_cypd', title='Dara-CyPd', concept=retired)

    response = _post(etl, person, [TICKET_LINES[2] | {'line_number': 1}])

    assert response.status_code == 200, response.data
    line = response.data['lines'][0]
    assert line['regimen_match'] == 'unmatched'
    assert line['regimen_concept_id'] != retired.concept_id


def test_an_empty_batch_is_refused(etl, person):
    assert _post(etl, person, []).status_code == 400


def test_a_line_the_writer_refuses_rolls_back_the_whole_batch(etl, person):
    response = _post(etl, person, [
        TICKET_LINES[0],
        # No melanoma diagnosis on this record to file a line under.
        TICKET_LINES[1] | {'disease': 'Melanoma'},
    ])

    assert response.status_code == 400, response.data
    first, second = response.data['lines']
    assert not first
    assert 'no diagnosis' in second['non_field_errors'][0]
    assert not Episode.objects.filter(person=person).exists()
    assert not RegimenMappingGap.objects.exists()


def test_a_line_s_existing_drugs_are_kept(etl, person):
    drug = ConceptFactory(concept_name='lenalidomide', concept_code='len')
    authored = author_therapy_line(
        person, line_number=2, start_date=date(2021, 1, 1),
        drugs=[{'concept_id': drug.concept_id, 'source_value': 'lenalidomide'}],
    )

    response = _post(etl, person, [TICKET_LINES[1]])

    assert response.status_code == 200, response.data
    assert response.data['lines'][0]['created'] is False
    assert EpisodeEvent.objects.filter(episode_id=authored.episode.episode_id,
                                       event_id__in=authored.drug_exposure_ids).count() == 1
    assert not DrugExposure.objects.get(pk=authored.drug_exposure_ids[0]).is_erroneous


def test_provenance_is_recorded_for_created_lines_only(etl, person):
    _post(etl, person, TICKET_LINES[:1])  # No source header: nothing to attribute.
    response = _post(etl, person, TICKET_LINES[:2], HTTP_X_PROVENANCE_SOURCE='healthtree-etl')

    assert response.status_code == 200, response.data
    records = ProvenanceRecord.objects.filter(source='healthtree-etl')
    # Line 1 already existed, so this request created only line 2.
    assert [(r.object_id, r.target_patient_id) for r in records] == [
        (_lines(person)[2].episode_id, str(person.person_id))]
    assert not ProvenanceRecord.objects.filter(object_id=_lines(person)[1].episode_id).exists()


def test_skip_refresh_defers_the_derivation(etl, person):
    response = etl.post(f'{URL}?skip_refresh=true', {'person': person.person_id, 'lines': TICKET_LINES},
                        format='json')

    assert response.status_code == 200, response.data
    assert len(_lines(person)) == 3
    assert not PatientRecord.objects.get(person=person).therapy_lines_count


def test_without_the_regimen_vocabulary_nothing_is_written(etl, person):
    Concept.objects.filter(pk=32531).delete()

    response = _post(etl, person, TICKET_LINES)

    assert response.status_code == 409
    assert not Episode.objects.filter(person=person).exists()
    assert not Concept.objects.filter(vocabulary_id='HK-Regimen').exists()


def test_a_patient_session_cannot_import_lines(person):
    patient = get_user_model().objects.create_user(email='patient-1969@example.test', password='pw')
    api = APIClient()
    api.force_authenticate(patient)

    assert _post(api, person, TICKET_LINES).status_code == 403
    assert APIClient().post(URL, {}, format='json').status_code in (401, 403)
    assert not Episode.objects.filter(person=person).exists()


def test_staff_can_import_lines(person):
    staff = get_user_model().objects.create_user(email='staff-1969@example.test', password='pw', is_staff=True)
    api = APIClient()
    api.force_authenticate(staff)

    assert _post(api, person, TICKET_LINES).status_code == 200
    assert len(_lines(person)) == 3


def test_an_unexpected_error_is_not_returned_to_the_caller(etl, person, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError('internal detail')
    monkeypatch.setattr('patient_portal.api.therapy_line_import.author_therapy_lines', broken)
    etl.raise_request_exception = False

    response = _post(etl, person, TICKET_LINES)

    assert response.status_code == 500
    assert b'internal detail' not in response.content


def test_a_lock_conflict_returns_a_fixed_message(etl, person, monkeypatch):
    def conflict(*args, **kwargs):
        raise TherapyLineWriteConflict('internal detail')
    monkeypatch.setattr('patient_portal.api.therapy_line_import.author_therapy_lines', conflict)

    response = _post(etl, person, TICKET_LINES)

    assert response.status_code == 409
    assert response.data == {'detail': CONCURRENT_EDIT_MESSAGE}


def _source_headers(organization):
    return {'HTTP_X_PROVENANCE_SOURCE': 'safety-test-etl',
            'HTTP_X_PROVENANCE_ORGANIZATION_ID': str(organization.pk)}


def test_foreign_line_attribution_rejects_and_rolls_back_the_complete_import(etl, person):
    from omop_core.models import Organization

    source = Organization.objects.create(name='Source A', slug='etl-source-a')
    other = Organization.objects.create(name='Source B', slug='etl-source-b')
    assert _post(etl, person, TICKET_LINES[:1], **_source_headers(source)).status_code == 200
    line = _lines(person)[1]
    before_episode = Episode.objects.filter(pk=line.pk).values().get()
    before_outcomes = list(Observation.objects.filter(person=person).order_by('pk').values())
    before_record = PatientRecord.objects.filter(person=person).values().get()
    before_provenance = list(ProvenanceRecord.objects.order_by('pk').values())
    before_gaps = list(RegimenMappingGap.objects.order_by('pk').values())
    response = _post(etl, person, [
        TICKET_LINES[1] | {'regimen': 'new quarantined regimen'},
        TICKET_LINES[0] | {'outcome': 'Stable Disease'},
    ], **_source_headers(other))

    assert response.status_code == 409, response.data
    assert response.data['detail'] == 'Row already attributed to another organization.'
    assert len(_lines(person)) == 1
    assert Episode.objects.filter(pk=line.pk).values().get() == before_episode
    assert list(Observation.objects.filter(person=person).order_by('pk').values()) == before_outcomes
    assert PatientRecord.objects.filter(person=person).values().get() == before_record
    assert list(ProvenanceRecord.objects.order_by('pk').values()) == before_provenance
    assert list(RegimenMappingGap.objects.order_by('pk').values()) == before_gaps


def test_source_guard_checks_only_the_lines_being_updated(etl, person):
    from omop_core.models import Organization

    source = Organization.objects.create(name='Source A', slug='etl-allowed-a')
    other = Organization.objects.create(name='Source B', slug='etl-allowed-b')
    assert _post(etl, person, TICKET_LINES[:1], **_source_headers(source)).status_code == 200
    assert _post(etl, person, TICKET_LINES[1:2], **_source_headers(other)).status_code == 200
    for _ in range(2):
        response = _post(etl, person, [TICKET_LINES[1] | {'outcome': 'Partial Response'}],
                         **_source_headers(other))
        assert response.status_code == 200, response.data
    assert _outcome(person, 2) == 'Partial Response'
    assert ProvenanceRecord.objects.filter(organization=source).count() == 1
    assert ProvenanceRecord.objects.filter(organization=other).count() == 1


@pytest.mark.parametrize('linked', [False, True])
@pytest.mark.parametrize('outcome', ['Stable Disease', None])
def test_foreign_outcome_cannot_be_adopted_overwritten_or_deleted(etl, person, linked, outcome):
    from django.contrib.contenttypes.models import ContentType
    from omop_core.models import Organization
    from tests.factories import ObservationFactory

    source = Organization.objects.create(name='Outcome A', slug='etl-outcome-a')
    other = Organization.objects.create(name='Outcome B', slug='etl-outcome-b')
    if linked:
        assert _post(etl, person, TICKET_LINES[:1]).status_code == 200
        observation = Observation.objects.get(person=person, observation_source_value='LOT-1-outcome')
    else:
        observation = ObservationFactory(
            person=person, observation_concept_id=0, observation_type_concept_id=32817,
            observation_source_value='LOT-1-outcome', value_as_string='Partial Response',
            observation_event_id=None,
        )
    ProvenanceRecord.objects.create(
        content_type=ContentType.objects.get_for_model(Observation), object_id=observation.pk,
        source='outcome-source-a', organization=source,
    )
    before_observation = Observation.objects.filter(pk=observation.pk).values().get()
    before_episodes = list(Episode.objects.filter(person=person).order_by('pk').values())
    before_provenance = list(ProvenanceRecord.objects.order_by('pk').values())
    response = _post(etl, person, [TICKET_LINES[0] | {'outcome': outcome}], **_source_headers(other))
    assert response.status_code == 409, response.data
    assert Observation.objects.filter(pk=observation.pk).values().get() == before_observation
    assert list(Episode.objects.filter(person=person).order_by('pk').values()) == before_episodes
    assert list(ProvenanceRecord.objects.order_by('pk').values()) == before_provenance


@pytest.mark.parametrize('same_source', [False, True])
def test_outcome_guard_allows_own_corrections_and_leaves_omitted_foreign_values_alone(etl, person, same_source):
    from django.contrib.contenttypes.models import ContentType
    from omop_core.models import Organization
    from tests.factories import ObservationFactory

    source = Organization.objects.create(name='Outcome Owner', slug='etl-outcome-owner')
    other = Organization.objects.create(name='Other Source', slug='etl-outcome-other')
    observation = ObservationFactory(
        person=person, observation_concept_id=0, observation_type_concept_id=32817,
        observation_source_value='LOT-1-outcome', value_as_string='Partial Response',
        observation_event_id=None,
    )
    ProvenanceRecord.objects.create(
        content_type=ContentType.objects.get_for_model(Observation), object_id=observation.pk,
        source='outcome-owner', organization=source,
    )
    line = dict(TICKET_LINES[0])
    if same_source:
        line['outcome'] = 'Stable Disease'
    else:
        line.pop('outcome')
    response = _post(etl, person, [line], **_source_headers(source if same_source else other))
    assert response.status_code == 200, response.data
    observation.refresh_from_db()
    assert observation.value_as_string == ('Stable Disease' if same_source else 'Partial Response')
    assert observation.observation_event_id == (response.data['lines'][0]['episode_id'] if same_source else None)


def test_regimen_resolution_uses_mapping_gap_key_order(etl, person, monkeypatch):
    from omop_core.mapping.therapy import normalize_regimen_name
    from patient_portal.api import therapy_line_import

    resolve = therapy_line_import.resolve_regimen_label
    observed = []

    def record_resolution(label, source):
        observed.append(normalize_regimen_name(label)[:255])
        return resolve(label, source)

    monkeypatch.setattr(therapy_line_import, 'resolve_regimen_label', record_resolution)
    response = _post(etl, person, [
        TICKET_LINES[0] | {'regimen': 'BBB'}, TICKET_LINES[1] | {'regimen': 'aaa'},
    ])
    assert response.status_code == 200, response.data
    assert observed == ['aaa', 'bbb']


def _deadlock():
    from django.db import OperationalError
    from psycopg.errors import DeadlockDetected

    error = OperationalError('internal deadlock detail')
    error.__cause__ = DeadlockDetected('internal PostgreSQL detail')
    return error


def test_deadlock_during_resolution_replays_the_whole_import(etl, person, monkeypatch):
    from patient_portal.api import therapy_line_import

    resolve = therapy_line_import.resolve_regimen_label
    attempts = []

    def fail_first_resolution(label, source):
        result = resolve(label, source)
        attempts.append(label)
        if len(attempts) == 1:
            raise _deadlock()
        return result

    monkeypatch.setattr(therapy_line_import, 'resolve_regimen_label', fail_first_resolution)
    etl.raise_request_exception = False
    response = _post(etl, person, TICKET_LINES[:1])
    assert response.status_code == 200, response.data
    assert len(attempts) == 2
    assert Episode.objects.filter(person=person).count() == 1
    assert RegimenMappingGap.objects.count() == 1
    assert Observation.objects.filter(person=person, observation_source_value='LOT-1-outcome').count() == 1


def test_exhausted_resolution_deadlocks_leave_no_partial_import(etl, person, monkeypatch):
    from patient_portal.api import therapy_line_import

    resolve = therapy_line_import.resolve_regimen_label
    attempts = []

    def always_deadlock(label, source):
        resolve(label, source)
        attempts.append(label)
        raise _deadlock()

    monkeypatch.setattr(therapy_line_import, 'resolve_regimen_label', always_deadlock)
    etl.raise_request_exception = False
    response = _post(etl, person, TICKET_LINES[:1])
    assert response.status_code == 409, response.data
    assert response.data == {'detail': CONCURRENT_EDIT_MESSAGE}
    assert len(attempts) == 3
    assert not Episode.objects.filter(person=person).exists()
    assert not RegimenMappingGap.objects.exists()
    assert not Concept.objects.filter(vocabulary_id='HK-Regimen').exists()


def test_deadlock_after_writes_rolls_back_facts_provenance_and_projection(etl, person, monkeypatch):
    from omop_core.models import Organization
    from patient_portal.api import therapy_line_import

    organization = Organization.objects.create(name='Retry Source', slug='etl-retry-source')
    refresh = therapy_line_import.refresh_patient_record
    attempts = []

    def fail_first_refresh(patient):
        record = refresh(patient)
        # This failure happens after every write stage, not before validation.
        assert Episode.objects.filter(person=person).count() == 1
        assert ProvenanceRecord.objects.filter(organization=organization).count() == 1
        attempts.append(record.therapy_lines_count)
        if len(attempts) == 1:
            raise _deadlock()
        return record

    monkeypatch.setattr(therapy_line_import, 'refresh_patient_record', fail_first_refresh)
    etl.raise_request_exception = False
    response = _post(etl, person, TICKET_LINES[:1], **_source_headers(organization))
    assert response.status_code == 200, response.data
    assert attempts == [1, 1]
    assert response.data['created'] == 1
    assert Episode.objects.filter(person=person).count() == 1
    assert ProvenanceRecord.objects.filter(organization=organization).count() == 1
    assert RegimenMappingGap.objects.count() == 1
    assert PatientRecord.objects.get(person=person).therapy_lines_count == 1


@pytest.mark.django_db(transaction=True)
def test_foreign_attribution_is_checked_while_the_episode_is_locked(etl, person, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from django.db import OperationalError, connections, transaction
    from omop_core.models import Organization
    from patient_portal.api import therapy_line_import

    source = Organization.objects.create(name='Locked Source A', slug='etl-lock-source-a')
    other = Organization.objects.create(name='Locked Source B', slug='etl-lock-source-b')
    assert _post(etl, person, TICKET_LINES[:1], **_source_headers(source)).status_code == 200
    guard = therapy_line_import.raise_on_foreign_attribution
    observed = []

    def check_lock(model, ids):
        try:
            with transaction.atomic():
                list(model.objects.select_for_update(nowait=True).filter(pk__in=ids))
        except OperationalError as exc:
            return getattr(exc.__cause__, 'sqlstate', None) or getattr(exc.__cause__, 'pgcode', None)
        finally:
            connections.close_all()
        return None

    def guarded(model, ids, organization):
        with ThreadPoolExecutor(max_workers=1) as pool:
            observed.append(pool.submit(check_lock, model, list(ids)).result(timeout=20))
        return guard(model, ids, organization)

    monkeypatch.setattr(therapy_line_import, 'raise_on_foreign_attribution', guarded)
    response = _post(etl, person, TICKET_LINES[:1], **_source_headers(other))
    assert response.status_code == 409
    assert observed == ['55P03']


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('seed_gaps', [False, True])
def test_concurrent_imports_share_normalized_regimen_lock_order(etl, person, seed_gaps):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from django.db import close_old_connections, connections

    people = [person, *[PatientRecordFactory(disease='multiple myeloma').person for _ in range(7)]]
    # Prepare the source identity; exercise both absent and existing gap rows.
    assert _post(etl, person, []).status_code == 400
    from omop_core.mapping.therapy import resolve_regimen_label
    if seed_gaps:
        for label in ('AAA', 'bbb'):
            resolve_regimen_label(label, 'therapy-lines-api')
    start = Barrier(len(people))

    def import_one(index, patient_id):
        close_old_connections()
        try:
            api = APIClient()
            api.credentials(HTTP_AUTHORIZATION=f'Bearer {ETL_TOKEN}')
            labels = ('AAA', 'bbb') if index % 2 == 0 else ('BBB', 'aaa')
            lines = [dict(TICKET_LINES[i], regimen=label) for i, label in enumerate(labels)]
            start.wait(timeout=20)
            response = api.post(f'{URL}?skip_refresh=true',
                                {'person': patient_id, 'lines': lines}, format='json')
            return response.status_code, response.data
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=len(people)) as pool:
        futures = [pool.submit(import_one, i, patient.pk) for i, patient in enumerate(people)]
        responses = [future.result(timeout=40) for future in futures]
    assert [status for status, _ in responses] == [200] * len(people), responses
    assert all(body['created'] == 2 for _, body in responses)
    for patient in people:
        assert Episode.objects.filter(person=patient).count() == 2
        assert TherapyLineAssertion.objects.filter(episode__person=patient, source='import').count() == 2
    assert set(RegimenMappingGap.objects.values_list('normalized_name', flat=True)) == {'aaa', 'bbb'}
