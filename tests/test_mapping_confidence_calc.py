"""A mapping's confidence: shown, scored by Calc, kept on approval (#2668).

Provisional data reads ``SourceCodeConceptMapping.suggestion_confidence``: a
proposed mapping with a destination and a confidence at or above the floor makes
provisional rows. Suggest and Concept to Code store it; the Edit Mapping dialog
shows it and its Calc button asks ``code-mappings/score/`` for a destination a
curator picked by hand. The ranker is stubbed throughout: no model runs here.
"""
import pytest
from rest_framework.test import APIClient

from omop_core.models import GroupAccess, SourceCodeConceptMapping, SuggestRun
from patient_portal.models import Identity
from omop_core.services.suggest_jobs import (
    FakeDispatcher, InlineDispatcher, use_dispatcher,
)
from tests.factories import (
    ConceptClassFactory, ConceptFactory, DomainFactory, OrganizationFactory, VocabularyFactory,
)

pytestmark = pytest.mark.django_db

SCORE = '/api/v1/code-mappings/score/'


@pytest.fixture()
def lab():
    domain = DomainFactory(domain_id='Measurement', domain_name='Measurement')
    vocabulary = VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC')
    klass = ConceptClassFactory(concept_class_id='Lab Test')

    def make(concept_id, name):
        return ConceptFactory(
            concept_id=concept_id, concept_name=name, concept_code=str(concept_id),
            vocabulary=vocabulary, domain=domain, concept_class=klass, standard_concept='S',
        )
    return {'glucose': make(3004501, 'Glucose in Serum'), 'sodium': make(3019550, 'Sodium in Serum')}


@pytest.fixture()
def staff():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='conf-staff@example.test', is_staff=True))
    return client


@pytest.fixture(autouse=True)
def no_retrieval(monkeypatch):
    """The retrieval searches find nothing, so the pool is the chosen destination."""
    monkeypatch.setattr('omop_core.mapping.suggestions.umls_candidates', lambda *a, **k: ([], None))
    monkeypatch.setattr('omop_core.mapping.suggestions.lexical_candidates', lambda *a, **k: [])
    monkeypatch.setattr('omop_core.mapping.suggestions.semantic_candidates', lambda *a, **k: [])


def _stub_ranker(monkeypatch, pick, confidence=None, note='high confidence: fits', alternatives=True):
    """Make the Anthropic ranker pick *pick* (a concept) and record the pool it saw."""
    seen = {}

    def rank(source_value, candidates, *args, **kwargs):
        seen['pool'] = [dict(c) for c in candidates]
        chosen = next((c for c in candidates if c['concept_id'] == pick.concept_id), None) if pick else None
        if chosen is None:
            return None, 'No suitable concept: nothing fits', None
        alts = [{'concept_id': pick.concept_id, 'concept_name': pick.concept_name,
                 'confidence': confidence, 'ranker': 'anthropic'}] if alternatives else None
        return chosen, note, alts
    monkeypatch.setattr('omop_core.mapping.suggestions.rank_candidates', rank)
    return seen


def _score(client, concept_id, **extra):
    return client.post(SCORE, {
        'source_code': 'GLU-LOCAL', 'source_vocabulary_id': 'Local', 'omop_table': 'measurement',
        'source_code_description': 'Glucose, serum', 'destination_concept_id': concept_id, **extra,
    }, format='json')


# --- the score endpoint ---------------------------------------------------------------

def test_calc_returns_the_rankers_score_for_the_destination(lab, staff, monkeypatch):
    _stub_ranker(monkeypatch, lab['glucose'], confidence=0.82)

    response = _score(staff, lab['glucose'].concept_id)

    assert response.status_code == 200, response.content
    assert response.data['scored'] == {
        'concept_id': lab['glucose'].concept_id, 'confidence': 0.82, 'ranked': True, 'note': '',
    }


def test_the_destination_is_in_the_pool_even_when_retrieval_finds_nothing(lab, staff, monkeypatch):
    seen = _stub_ranker(monkeypatch, lab['glucose'], confidence=0.9)

    _score(staff, lab['glucose'].concept_id)

    assert [c['concept_id'] for c in seen['pool']] == [lab['glucose'].concept_id]
    assert seen['pool'][0]['retrieval'] == 'selected'


def test_the_ranker_preferring_another_concept_gives_no_number(lab, staff, monkeypatch):
    sodium = lab['sodium']
    monkeypatch.setattr('omop_core.mapping.suggestions.lexical_candidates', lambda *a, **k: [{
        'concept_id': sodium.concept_id, 'concept_name': sodium.concept_name,
        'concept_code': sodium.concept_code, 'vocabulary_id': 'LOINC',
        'concept_class_id': 'Lab Test', 'domain_id': 'Measurement',
        'standard_concept': 'S', 'lexical_score': 0.9, 'retrieval': 'lexical',
    }])
    _stub_ranker(monkeypatch, sodium, confidence=0.95)

    response = _score(staff, lab['glucose'].concept_id)

    assert response.status_code == 200
    scored = response.data['scored']
    assert scored['confidence'] is None and scored['ranked'] is True
    assert 'preferred Sodium in Serum' in scored['note']


def test_a_declining_ranker_gives_no_number(lab, staff, monkeypatch):
    _stub_ranker(monkeypatch, None)

    scored = _score(staff, lab['glucose'].concept_id).data['scored']

    assert scored['confidence'] is None and scored['ranked'] is True
    assert 'no suitable' in scored['note'].lower()


def test_an_unavailable_ranker_gives_no_number_not_a_fallback(lab, staff, settings):
    """No API key: the ranker hands back the first retrieval hit as a stand-in. That
    is not a verdict, and it must never become a confident-looking number."""
    settings.ANTHROPIC_API_KEY = ''

    scored = _score(staff, lab['glucose'].concept_id).data['scored']

    assert scored['confidence'] is None and scored['ranked'] is False
    assert 'unavailable' in scored['note'].lower()


def test_a_score_the_server_would_refuse_reads_as_unscored(lab, staff, monkeypatch):
    _stub_ranker(monkeypatch, lab['glucose'], confidence=7)

    assert _score(staff, lab['glucose'].concept_id).data['scored']['confidence'] is None


def test_calc_writes_nothing(lab, staff, monkeypatch):
    mapping = SourceCodeConceptMapping.objects.create(
        source_code='GLU-LOCAL', source_vocabulary_id='Local', omop_table='measurement',
        domain_id='Measurement', status='proposed', target_concept=lab['glucose'],
        suggestion_confidence=0.5,
    )
    before = SourceCodeConceptMapping.objects.filter(pk=mapping.pk).values().get()
    _stub_ranker(monkeypatch, lab['glucose'], confidence=0.9)

    _score(staff, lab['glucose'].concept_id)

    assert SourceCodeConceptMapping.objects.filter(pk=mapping.pk).values().get() == before


def test_calc_needs_a_real_destination(lab, staff):
    assert _score(staff, '').status_code == 400
    assert _score(staff, 0).status_code == 400
    assert _score(staff, 99999999).status_code == 400
    assert staff.post(SCORE, {'source_code': 'X', 'omop_table': 'measurement'}, format='json').status_code == 400


def test_calc_asks_the_same_question_as_suggest_one_but_queued(lab, staff):
    with use_dispatcher(FakeDispatcher()) as fake:
        response = _score(staff, lab['glucose'].concept_id, **{'async': True})

    assert response.status_code == 202
    assert fake.calls[0][1]['preview']['score_concept_id'] == lab['glucose'].concept_id
    # It is an individual preview, so the page's "latest run" never picks it up.
    assert SuggestRun.objects.get(pk=response.data['run_id']).selection['mode'] == 'individual'


def test_a_queued_calc_ends_in_a_scored_result_event(lab, staff, monkeypatch):
    _stub_ranker(monkeypatch, lab['glucose'], confidence=0.7)
    with use_dispatcher(InlineDispatcher()):
        response = _score(staff, lab['glucose'].concept_id, **{'async': True})

    assert response.data['state'] == 'success'
    result = [e for e in response.data['activity'] if e['stage'] == 'result'][-1]
    assert result['scored']['confidence'] == 0.7 and result['suggested'] is None


def test_calc_is_a_mapping_writers_action(lab, monkeypatch):
    _stub_ranker(monkeypatch, lab['glucose'], confidence=0.8)
    org = OrganizationFactory()

    def client_with(role):
        identity = Identity.objects.create_user(email=f'conf-{role}@example.test')
        GroupAccess.objects.create(identity=identity, org=org, role=role)
        client = APIClient()
        client.force_authenticate(identity)
        return client

    assert _score(client_with('doctor'), lab['glucose'].concept_id).status_code == 403
    assert _score(client_with('analyst'), lab['glucose'].concept_id).status_code == 200
    assert _score(APIClient(), lab['glucose'].concept_id).status_code in (401, 403)


# --- the save keeps the number --------------------------------------------------------

def _create(client, concept, **extra):
    return client.post('/api/v1/code-mappings/', {
        'source_vocabulary_id': 'Local', 'source_code': 'GLU-LOCAL', 'omop_table': 'measurement',
        'domain_id': 'Measurement', 'destination_concept_id': concept.concept_id,
        'status': 'proposed', **extra,
    }, format='json')


def test_a_save_stores_the_confidence_the_dialog_sends(lab, staff):
    response = _create(staff, lab['glucose'], suggestion_confidence=0.82)

    assert response.status_code == 201, response.content
    mapping = SourceCodeConceptMapping.objects.get(source_code='GLU-LOCAL')
    assert mapping.suggestion_confidence == 0.82
    assert response.data['suggestion_confidence'] == 0.82


def test_a_save_without_the_key_stores_none_for_a_hand_picked_destination(lab, staff):
    _create(staff, lab['glucose'])

    assert SourceCodeConceptMapping.objects.get(source_code='GLU-LOCAL').suggestion_confidence is None


def test_a_bad_confidence_is_a_400(lab, staff):
    for bad in (1.5, -0.1, 'high', True):
        response = _create(staff, lab['glucose'], suggestion_confidence=bad)
        assert response.status_code == 400, bad
        assert 'suggestion_confidence' in response.data


def test_approval_keeps_the_confidence(lab, staff):
    mapping_id = _create(staff, lab['glucose'], suggestion_confidence=0.82).data['mapping_id']

    response = staff.patch(f'/api/v1/code-mappings/{mapping_id}/', {
        'status': 'approved', 'destination_concept_id': lab['glucose'].concept_id,
        'domain_id': 'Measurement', 'omop_table': 'measurement',
    }, format='json')

    assert response.status_code == 200, response.content
    mapping = SourceCodeConceptMapping.objects.get(pk=mapping_id)
    assert mapping.status == 'approved' and mapping.suggestion_confidence == 0.82


def test_a_moved_destination_clears_the_machines_confidence(lab, staff):
    mapping_id = _create(staff, lab['glucose'], suggestion_confidence=0.82).data['mapping_id']

    staff.patch(f'/api/v1/code-mappings/{mapping_id}/', {
        'destination_concept_id': lab['sodium'].concept_id, 'status': 'proposed',
        'domain_id': 'Measurement', 'omop_table': 'measurement',
    }, format='json')

    assert SourceCodeConceptMapping.objects.get(pk=mapping_id).suggestion_confidence is None


def test_a_calcd_number_saved_with_the_move_persists_through_approval(lab, staff):
    mapping_id = _create(staff, lab['glucose'], suggestion_confidence=0.82).data['mapping_id']

    staff.patch(f'/api/v1/code-mappings/{mapping_id}/', {
        'destination_concept_id': lab['sodium'].concept_id, 'status': 'proposed',
        'suggestion_confidence': 0.64, 'domain_id': 'Measurement', 'omop_table': 'measurement',
    }, format='json')
    staff.patch(f'/api/v1/code-mappings/{mapping_id}/', {
        'destination_concept_id': lab['sodium'].concept_id, 'status': 'approved',
        'domain_id': 'Measurement', 'omop_table': 'measurement',
    }, format='json')

    mapping = SourceCodeConceptMapping.objects.get(pk=mapping_id)
    assert (mapping.status, mapping.suggestion_confidence) == ('approved', 0.64)


def test_clearing_sends_null(lab, staff):
    mapping_id = _create(staff, lab['glucose'], suggestion_confidence=0.82).data['mapping_id']

    staff.patch(f'/api/v1/code-mappings/{mapping_id}/', {
        'destination_concept_id': lab['glucose'].concept_id, 'suggestion_confidence': None,
        'status': 'proposed', 'domain_id': 'Measurement', 'omop_table': 'measurement',
    }, format='json')

    assert SourceCodeConceptMapping.objects.get(pk=mapping_id).suggestion_confidence is None


def test_the_list_returns_the_stored_confidence(lab, staff):
    _create(staff, lab['glucose'], suggestion_confidence=0.82)

    rows = staff.get('/api/v1/code-mappings/', {'search': 'GLU-LOCAL'}).json()
    rows = rows['results'] if isinstance(rows, dict) else rows

    assert [r['suggestion_confidence'] for r in rows if r['source_code'] == 'GLU-LOCAL'] == [0.82]


def test_group_approval_keeps_the_confidence(lab, staff):
    """The group actions go through the same writer; a stored confidence stays."""
    from patient_portal.api.views import _upsert_source_code_mapping
    user = Identity.objects.get(email='conf-staff@example.test')
    mapping = SourceCodeConceptMapping.objects.create(
        source_code='GLU-LOCAL', source_vocabulary_id='Local', omop_table='measurement',
        domain_id='Measurement', status='proposed', target_concept=lab['glucose'],
        suggestion_confidence=0.82,
    )

    _upsert_source_code_mapping(lab['glucose'], {
        'status': 'approved', 'domain_id': 'Measurement', 'omop_table': 'measurement',
    }, user, mapping=mapping, record_decision=False)

    mapping.refresh_from_db()
    assert (mapping.status, mapping.suggestion_confidence) == ('approved', 0.82)


# --- Concept to Code stores the preview's score on a proposal --------------------------

def test_a_concept_to_code_proposal_stores_the_previews_confidence(lab, staff, monkeypatch):
    concept = lab['glucose']
    candidate = {
        'source_code': 'GLU-LOCAL', 'source_vocabulary_id': 'Local', 'mapping_id': None,
        'source_code_description': 'Glucose, serum', 'confidence': 0.77,
    }
    run = SuggestRun.objects.create(
        direction='reverse', state=SuggestRun.SUCCESS,
        activity=[{'concept': {'concept_id': concept.concept_id}, 'candidates': [candidate]}],
    )
    monkeypatch.setattr('omop_core.mapping.reverse_catalog.refresh_catalog_candidate',
                        lambda cand, conc: dict(cand))

    response = staff.post(f'/api/v1/concept-to-code/{concept.concept_id}/mappings/', {
        'run_id': str(run.pk), 'source_code': 'GLU-LOCAL', 'source_vocabulary_id': 'Local',
    }, format='json')

    assert response.status_code == 201, response.content
    mapping = SourceCodeConceptMapping.objects.get(source_code='GLU-LOCAL')
    assert (mapping.status, mapping.suggestion_confidence) == ('proposed', 0.77)
