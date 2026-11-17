"""A mapping's confidence: shown, scored by Calc, signed, kept on approval (#2668).

Provisional data reads ``SourceCodeConceptMapping.suggestion_confidence``: a
proposed mapping with a destination and a confidence at or above the floor makes
provisional rows. Suggest and Concept to Code store it; the Edit Mapping dialog
shows it and its Calc button asks ``code-mappings/score/`` for a destination a
curator picked by hand. The server stores only a confidence it produced: the
score endpoint and a Suggest run sign what they computed, and a save accepts the
signed token, never a typed number. The rankers are stubbed throughout.
"""
import pytest
from rest_framework.test import APIClient

from omop_core.mapping import confidence_token
from omop_core.models import GroupAccess, SourceCodeConceptMapping, SuggestRun
from omop_core.services.suggest_jobs import (
    FakeDispatcher, InlineDispatcher, use_dispatcher,
)
from patient_portal.models import Identity
from tests.factories import (
    ConceptClassFactory, ConceptFactory, DomainFactory, OrganizationFactory, VocabularyFactory,
)

pytestmark = pytest.mark.django_db

SCORE = '/api/v1/code-mappings/score/'
SOURCE = {'source_vocabulary_id': 'Local', 'source_code': 'GLU-LOCAL', 'omop_table': 'measurement'}
DESCRIPTION = 'Glucose, serum'


@pytest.fixture()
def lab():
    domain = DomainFactory(domain_id='Measurement', domain_name='Measurement')
    vocabulary = VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC')
    klass = ConceptClassFactory(concept_class_id='Lab Test')

    def make(concept_id, name, **extra):
        return ConceptFactory(**{
            'concept_id': concept_id, 'concept_name': name, 'concept_code': str(concept_id),
            'vocabulary': vocabulary, 'domain': domain, 'concept_class': klass,
            'standard_concept': 'S', **extra,
        })
    return {
        'glucose': make(3004501, 'Glucose in Serum'), 'sodium': make(3019550, 'Sodium in Serum'),
        'make': make, 'klass': klass, 'vocabulary': vocabulary,
    }


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


def _hit(concept):
    return {
        'concept_id': concept.concept_id, 'concept_name': concept.concept_name,
        'concept_code': concept.concept_code, 'vocabulary_id': 'LOINC',
        'concept_class_id': 'Lab Test', 'domain_id': 'Measurement',
        'standard_concept': 'S', 'lexical_score': 0.9, 'retrieval': 'lexical',
    }


def _also_retrieve(monkeypatch, concept):
    monkeypatch.setattr('omop_core.mapping.suggestions.lexical_candidates', lambda *a, **k: [_hit(concept)])


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


def _stub_jev(monkeypatch, pick, scores):
    """Jev chooses *pick* (or declines on None) and, as the real one does, scores
    every candidate it was shown: *scores* maps concept id to probability."""
    def rank(source_value, candidates, *args, **kwargs):
        alts = [{'concept_id': c['concept_id'], 'concept_name': c['concept_name'],
                 'confidence': scores[c['concept_id']], 'ranker': 'jev'}
                for c in candidates if c['concept_id'] in scores]
        chosen = next((c for c in candidates if pick and c['concept_id'] == pick.concept_id), None)
        if chosen is None:
            return None, 'No suitable concept (Jev ranking).', alts
        return chosen, 'high confidence (Jev)', alts
    monkeypatch.setattr('omop_core.mapping.suggestions.rank_candidates_jev', rank)


def _score(client, concept_id, **extra):
    return client.post(SCORE, {
        **SOURCE, 'source_code_description': DESCRIPTION,
        'destination_concept_id': concept_id, **extra,
    }, format='json')


# --- the score endpoint ---------------------------------------------------------------

def test_calc_returns_the_rankers_score_for_the_destination(lab, staff, monkeypatch):
    _stub_ranker(monkeypatch, lab['glucose'], confidence=0.82)

    response = _score(staff, lab['glucose'].concept_id)

    assert response.status_code == 200, response.content
    scored = response.data['scored']
    assert (scored['concept_id'], scored['confidence'], scored['ranked'], scored['note']) == (
        lab['glucose'].concept_id, 0.82, True, '')
    # ... and signs it for exactly this source and destination.
    assert confidence_token.verify(
        scored['token'], destination_concept_id=lab['glucose'].concept_id,
        source_description=DESCRIPTION, **SOURCE) == 0.82


def test_the_destination_is_in_the_pool_even_when_retrieval_finds_nothing(lab, staff, monkeypatch):
    seen = _stub_ranker(monkeypatch, lab['glucose'], confidence=0.9)

    _score(staff, lab['glucose'].concept_id)

    assert [c['concept_id'] for c in seen['pool']] == [lab['glucose'].concept_id]
    assert seen['pool'][0]['retrieval'] == 'selected'


def test_the_ranker_preferring_another_concept_gives_no_number(lab, staff, monkeypatch):
    _also_retrieve(monkeypatch, lab['sodium'])
    _stub_ranker(monkeypatch, lab['sodium'], confidence=0.95)

    scored = _score(staff, lab['glucose'].concept_id).data['scored']

    assert scored['confidence'] is None and scored['ranked'] is True and 'token' not in scored
    assert scored['note'].startswith('The ranker preferred Sodium in Serum')
    assert 'over Glucose in Serum' in scored['note']


def test_a_ranker_that_chose_the_destination_without_a_score_says_so(lab, staff, monkeypatch):
    _stub_ranker(monkeypatch, lab['glucose'], alternatives=False)

    scored = _score(staff, lab['glucose'].concept_id).data['scored']

    assert scored['confidence'] is None
    assert 'preferred' not in scored['note'] and 'no score' in scored['note']


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
    assert 'unavailable' in scored['note'].lower() and '%' not in scored['note']


def test_both_rankers_unavailable_has_a_plain_note(lab, staff, settings):
    settings.ANTHROPIC_API_KEY = ''
    settings.JEV_API_KEY = ''

    scored = _score(staff, lab['glucose'].concept_id, ranking_model='both').data['scored']

    assert scored['confidence'] is None and scored['ranked'] is False
    assert 'unavailable' in scored['note'].lower() and '0%' not in scored['note']


def test_a_score_the_server_would_refuse_reads_as_unscored(lab, staff, monkeypatch):
    _stub_ranker(monkeypatch, lab['glucose'], confidence=7)

    assert _score(staff, lab['glucose'].concept_id).data['scored']['confidence'] is None


def test_jev_scoring_every_candidate_does_not_score_one_it_did_not_choose(lab, staff, monkeypatch):
    """Jev lists a probability for every candidate, chosen or not."""
    _also_retrieve(monkeypatch, lab['sodium'])
    _stub_jev(monkeypatch, lab['sodium'], {lab['glucose'].concept_id: 0.6, lab['sodium'].concept_id: 0.9})

    scored = _score(staff, lab['glucose'].concept_id, ranking_model='jev').data['scored']

    assert scored['confidence'] is None
    assert scored['note'].startswith('The ranker preferred Sodium in Serum')


def test_a_declining_jev_with_a_high_score_for_the_destination_gives_no_number(lab, staff, monkeypatch):
    _stub_jev(monkeypatch, None, {lab['glucose'].concept_id: 0.95})

    scored = _score(staff, lab['glucose'].concept_id, ranking_model='jev').data['scored']

    assert scored['confidence'] is None and 'no suitable' in scored['note'].lower()


def test_jev_choosing_the_destination_gives_its_probability(lab, staff, monkeypatch):
    _stub_jev(monkeypatch, lab['glucose'], {lab['glucose'].concept_id: 0.77})

    scored = _score(staff, lab['glucose'].concept_id, ranking_model='jev').data['scored']

    assert scored['confidence'] == 0.77 and scored['ranked'] is True


def test_in_dual_mode_the_ranker_that_chose_the_destination_scores_it(lab, staff, monkeypatch):
    """Anthropic prefers another concept; Jev chose the destination: Jev's score is used."""
    _also_retrieve(monkeypatch, lab['sodium'])
    _stub_ranker(monkeypatch, lab['sodium'], confidence=0.95)
    _stub_jev(monkeypatch, lab['glucose'], {lab['glucose'].concept_id: 0.7, lab['sodium'].concept_id: 0.4})

    scored = _score(staff, lab['glucose'].concept_id, ranking_model='both').data['scored']

    assert scored['confidence'] == 0.7


def test_in_dual_mode_the_higher_of_the_two_that_chose_it_is_used(lab, staff, monkeypatch):
    _stub_ranker(monkeypatch, lab['glucose'], confidence=0.6)
    _stub_jev(monkeypatch, lab['glucose'], {lab['glucose'].concept_id: 0.8})

    assert _score(staff, lab['glucose'].concept_id, ranking_model='both').data['scored']['confidence'] == 0.8


def test_in_dual_mode_neither_choosing_it_gives_no_number(lab, staff, monkeypatch):
    _also_retrieve(monkeypatch, lab['sodium'])
    _stub_ranker(monkeypatch, lab['sodium'], confidence=0.9)
    _stub_jev(monkeypatch, None, {lab['glucose'].concept_id: 0.9, lab['sodium'].concept_id: 0.3})

    assert _score(staff, lab['glucose'].concept_id, ranking_model='both').data['scored']['confidence'] is None


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


def test_calc_refuses_a_destination_the_retrieval_filters_would_have_dropped(lab, staff, monkeypatch):
    _stub_ranker(monkeypatch, None)
    non_standard = lab['make'](3000001, 'Brand name', standard_concept=None)
    retired = lab['make'](3000002, 'Retired test', invalid_reason='D')
    other_domain = ConceptFactory(
        concept_id=3000003, concept_name='A condition', concept_code='3000003',
        vocabulary=lab['vocabulary'], domain=DomainFactory(domain_id='Condition', domain_name='Condition'),
        concept_class=lab['klass'], standard_concept='S',
    )

    for concept in (non_standard, retired, other_domain):
        response = _score(staff, concept.concept_id)
        assert response.status_code == 400, concept.concept_name
        assert 'destination_concept_id' in response.data
        assert 'cannot be scored' in response.data['destination_concept_id']


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
    assert result['scored']['token']


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


def test_a_suggest_one_alternative_carries_a_token_for_its_own_destination(lab, staff, monkeypatch):
    _stub_ranker(monkeypatch, lab['glucose'], confidence=0.9)
    monkeypatch.setattr('omop_core.mapping.suggestions.lexical_candidates', lambda *a, **k: [_hit(lab['glucose'])])

    response = staff.post('/api/v1/code-mappings/suggest-one/', {**SOURCE, 'source_code_description': 'Glucose'},
                          format='json')

    [alternative] = response.data['alternatives']
    assert confidence_token.verify(
        alternative['confidence_token'], destination_concept_id=lab['glucose'].concept_id,
        source_description='Glucose', **SOURCE) == 0.9


# --- the token ------------------------------------------------------------------------

def _token(concept, confidence=0.82, **overrides):
    return confidence_token.issue(**{
        **SOURCE, 'destination_concept_id': concept.concept_id, 'confidence': confidence,
        'source_description': DESCRIPTION, **overrides,
    })


def test_a_token_is_bound_to_its_source_table_and_destination(lab):
    token = _token(lab['glucose'])
    good = {**SOURCE, 'destination_concept_id': lab['glucose'].concept_id, 'source_description': DESCRIPTION}

    assert confidence_token.verify(token, **good) == 0.82
    for change in ({'destination_concept_id': lab['sodium'].concept_id}, {'source_code': 'OTHER'},
                   {'omop_table': 'observation'}, {'source_vocabulary_id': 'Epic'},
                   {'source_description': 'Hemoglobin A1c'}):
        with pytest.raises(ValueError):
            confidence_token.verify(token, **{**good, **change})
    with pytest.raises(ValueError):
        confidence_token.verify(token + 'x', **good)
    with pytest.raises(ValueError):
        confidence_token.verify('not a token', **good)
    with pytest.raises(ValueError):
        confidence_token.verify(token, **{**good, 'destination_concept_id': None})


def test_a_token_expires(lab, monkeypatch):
    token = _token(lab['glucose'])
    monkeypatch.setattr(confidence_token, 'MAX_AGE_SECONDS', -1)

    with pytest.raises(ValueError, match='expired'):
        confidence_token.verify(token, **SOURCE, destination_concept_id=lab['glucose'].concept_id,
                                source_description=DESCRIPTION)


# --- the save keeps only a confidence the server signed -------------------------------

def _create(client, concept, **extra):
    return client.post('/api/v1/code-mappings/', {
        **SOURCE, 'domain_id': 'Measurement', 'destination_concept_id': concept.concept_id,
        'status': 'proposed', 'source_code_description': DESCRIPTION, **extra,
    }, format='json')


def _patch(client, mapping_id, concept, **extra):
    return client.patch(f'/api/v1/code-mappings/{mapping_id}/', {
        'destination_concept_id': concept.concept_id, 'status': 'proposed',
        'domain_id': 'Measurement', 'omop_table': 'measurement', **extra,
    }, format='json')


def _stored(mapping_id=None):
    qs = SourceCodeConceptMapping.objects.filter(source_code='GLU-LOCAL')
    return (qs.get(pk=mapping_id) if mapping_id else qs.get()).suggestion_confidence


def test_a_typed_confidence_is_never_stored(lab, staff):
    """An analyst could otherwise make a never-ranked mapping yield provisional rows."""
    response = _create(staff, lab['glucose'], suggestion_confidence=1.0)

    assert response.status_code == 201, response.content
    assert _stored() is None


def test_a_signed_confidence_is_stored_and_returned(lab, staff):
    response = _create(staff, lab['glucose'], suggestion_confidence_token=_token(lab['glucose']))

    assert response.status_code == 201, response.content
    assert _stored() == 0.82
    assert response.data['suggestion_confidence'] == 0.82


def test_a_forged_mismatched_or_expired_token_is_a_400_and_stores_nothing(lab, staff, monkeypatch):
    for token in ('forged', _token(lab['sodium']), _token(lab['glucose'], source_code='OTHER'),
                  _token(lab['glucose'], omop_table='observation'),
                  _token(lab['glucose'], source_description='Hemoglobin A1c')):
        response = _create(staff, lab['glucose'], suggestion_confidence_token=token)
        assert response.status_code == 400, token
        assert 'suggestion_confidence_token' in response.data
    assert not SourceCodeConceptMapping.objects.filter(source_code='GLU-LOCAL').exists()

    token = _token(lab['glucose'])
    monkeypatch.setattr(confidence_token, 'MAX_AGE_SECONDS', -1)
    assert _create(staff, lab['glucose'], suggestion_confidence_token=token).status_code == 400


def test_a_token_cannot_be_used_for_a_different_source_on_patch(lab, staff):
    mapping_id = _create(staff, lab['glucose']).data['mapping_id']

    response = _patch(staff, mapping_id, lab['glucose'], source_code='OTHER-CODE',
                      suggestion_confidence_token=_token(lab['glucose']))

    assert response.status_code == 400
    assert _stored(mapping_id) is None


def test_a_save_without_a_token_keeps_the_stored_confidence_while_the_destination_stands(lab, staff):
    mapping_id = _create(staff, lab['glucose'], suggestion_confidence_token=_token(lab['glucose'])).data['mapping_id']

    response = _patch(staff, mapping_id, lab['glucose'], suggestion_confidence=1.0)

    assert response.status_code == 200
    assert _stored(mapping_id) == 0.82


def test_approval_keeps_the_confidence(lab, staff):
    mapping_id = _create(staff, lab['glucose'], suggestion_confidence_token=_token(lab['glucose'])).data['mapping_id']

    response = staff.patch(f'/api/v1/code-mappings/{mapping_id}/', {
        'status': 'approved', 'destination_concept_id': lab['glucose'].concept_id,
        'domain_id': 'Measurement', 'omop_table': 'measurement',
    }, format='json')

    assert response.status_code == 200, response.content
    mapping = SourceCodeConceptMapping.objects.get(pk=mapping_id)
    assert mapping.status == 'approved' and mapping.suggestion_confidence == 0.82


def test_a_moved_destination_clears_the_machines_confidence(lab, staff):
    mapping_id = _create(staff, lab['glucose'], suggestion_confidence_token=_token(lab['glucose'])).data['mapping_id']

    _patch(staff, mapping_id, lab['sodium'])

    assert _stored(mapping_id) is None


def test_a_calcd_token_saved_with_the_move_persists_through_approval(lab, staff):
    mapping_id = _create(staff, lab['glucose'], suggestion_confidence_token=_token(lab['glucose'])).data['mapping_id']

    _patch(staff, mapping_id, lab['sodium'], suggestion_confidence_token=_token(lab['sodium'], 0.64))
    staff.patch(f'/api/v1/code-mappings/{mapping_id}/', {
        'destination_concept_id': lab['sodium'].concept_id, 'status': 'approved',
        'domain_id': 'Measurement', 'omop_table': 'measurement',
    }, format='json')

    mapping = SourceCodeConceptMapping.objects.get(pk=mapping_id)
    assert (mapping.status, mapping.suggestion_confidence) == ('approved', 0.64)


def test_clearing_sends_null(lab, staff):
    mapping_id = _create(staff, lab['glucose'], suggestion_confidence_token=_token(lab['glucose'])).data['mapping_id']

    _patch(staff, mapping_id, lab['glucose'], suggestion_confidence=None)

    assert _stored(mapping_id) is None


def test_the_list_returns_the_stored_confidence(lab, staff):
    _create(staff, lab['glucose'], suggestion_confidence_token=_token(lab['glucose']))

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


def _preview(concept, row, confidence):
    """A finished Concept to Code preview that listed *row* as a candidate for *concept*."""
    candidate = {'source_code': row.source_code, 'source_vocabulary_id': row.source_vocabulary_id,
                 'mapping_id': row.pk, 'source_code_description': 'Glucose, serum'}
    if confidence is not None:
        candidate['confidence'] = confidence
    return SuggestRun.objects.create(
        direction='reverse', state=SuggestRun.SUCCESS,
        activity=[{'concept': {'concept_id': concept.concept_id}, 'candidates': [candidate]}],
    )


def _proposal_pointing_at(concept, confidence=0.3):
    return SourceCodeConceptMapping.objects.create(
        source_code='GLU-LOCAL', source_vocabulary_id='Local', omop_table='measurement',
        domain_id='Measurement', status='proposed', target_concept=concept,
        suggestion_confidence=confidence, origin='import', source_code_description=DESCRIPTION,
    )


def _apply(client, concept, row, run=None, **extra):
    body = {'status': 'proposed', 'expected_updated_at': row.updated_at.isoformat(), **extra}
    if run is not None:
        body['run_id'] = str(run.pk)
    return client.post(f'/api/v1/concept-to-code/{concept.concept_id}/mappings/{row.pk}/', body, format='json')


def test_applying_a_destination_stores_the_previews_score_for_it(lab, staff):
    row = _proposal_pointing_at(lab['sodium'])
    run = _preview(lab['glucose'], row, 0.77)

    response = _apply(staff, lab['glucose'], row, run)

    assert response.status_code == 200, response.content
    row.refresh_from_db()
    assert (row.target_concept_id, row.suggestion_confidence) == (lab['glucose'].concept_id, 0.77)
    assert row.decision_confidence == 0.77


def test_applying_a_candidate_the_preview_did_not_score_stores_none(lab, staff):
    row = _proposal_pointing_at(lab['sodium'], confidence=0.9)
    run = _preview(lab['glucose'], row, None)

    _apply(staff, lab['glucose'], row, run)

    row.refresh_from_db()
    assert row.target_concept_id == lab['glucose'].concept_id and row.suggestion_confidence is None


def test_applying_without_a_preview_run_stores_none_and_ignores_a_client_number(lab, staff):
    row = _proposal_pointing_at(lab['sodium'], confidence=0.9)

    _apply(staff, lab['glucose'], row, confidence=1.0, suggestion_confidence=1.0)

    row.refresh_from_db()
    assert row.target_concept_id == lab['glucose'].concept_id and row.suggestion_confidence is None


def test_a_preview_for_another_concept_gives_no_score(lab, staff):
    row = _proposal_pointing_at(lab['sodium'])
    run = _preview(lab['sodium'], row, 0.99)          # the preview was for a different concept

    _apply(staff, lab['glucose'], row, run)

    row.refresh_from_db()
    assert row.suggestion_confidence is None


def test_a_later_approval_keeps_the_applied_score(lab, staff):
    row = _proposal_pointing_at(lab['sodium'])
    run = _preview(lab['glucose'], row, 0.77)
    _apply(staff, lab['glucose'], row, run)
    row.refresh_from_db()

    response = _apply(staff, lab['glucose'], row, run, status='approved')

    assert response.status_code == 200, response.content
    row.refresh_from_db()
    assert (row.status, row.suggestion_confidence) == ('approved', 0.77)


def test_a_curator_moving_the_destination_afterwards_clears_it(lab, staff):
    row = _proposal_pointing_at(lab['sodium'])
    _apply(staff, lab['glucose'], row, _preview(lab['glucose'], row, 0.77))

    _patch(staff, row.pk, lab['sodium'])

    row.refresh_from_db()
    assert row.target_concept_id == lab['sodium'].concept_id and row.suggestion_confidence is None


def test_a_proposal_whose_preview_gave_no_score_stores_none(lab, staff, monkeypatch):
    concept = lab['glucose']
    candidate = {'source_code': 'GLU-LOCAL', 'source_vocabulary_id': 'Local', 'mapping_id': None,
                 'source_code_description': 'Glucose, serum'}
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
    assert SourceCodeConceptMapping.objects.get(source_code='GLU-LOCAL').suggestion_confidence is None


# --- review round: what is signed, what it is about, when it goes ------------------------

def _tokens(alternatives):
    return {a['concept_id']: a.get('confidence_token') for a in alternatives or []}


def test_the_score_response_signs_only_the_destination_a_ranker_chose(lab, staff, monkeypatch):
    """Jev scores every candidate it is shown; only the chosen destination's score is signed."""
    _also_retrieve(monkeypatch, lab['sodium'])
    _stub_jev(monkeypatch, lab['glucose'], {lab['glucose'].concept_id: 0.8, lab['sodium'].concept_id: 0.6})

    response = _score(staff, lab['glucose'].concept_id, ranking_model='jev')

    scored = response.data['scored']
    assert scored['confidence'] == 0.8 and scored['token']
    assert all(token is None for token in _tokens(response.data['alternatives']).values())


def test_a_declining_ranker_signs_nothing_in_suggest_one(lab, staff, monkeypatch):
    _also_retrieve(monkeypatch, lab['sodium'])
    _stub_jev(monkeypatch, None, {lab['glucose'].concept_id: 0.9, lab['sodium'].concept_id: 0.8})

    response = staff.post('/api/v1/code-mappings/suggest-one/', {
        **SOURCE, 'source_code_description': DESCRIPTION, 'ranking_model': 'jev'}, format='json')

    assert response.data['suggested'] is None
    assert response.data['alternatives'] and all(
        token is None for token in _tokens(response.data['alternatives']).values())


def test_suggest_one_signs_only_the_concept_that_was_chosen(lab, staff, monkeypatch):
    monkeypatch.setattr('omop_core.mapping.suggestions.lexical_candidates',
                        lambda *a, **k: [_hit(lab['glucose']), _hit(lab['sodium'])])
    _stub_jev(monkeypatch, lab['sodium'], {lab['glucose'].concept_id: 0.7, lab['sodium'].concept_id: 0.9})

    response = staff.post('/api/v1/code-mappings/suggest-one/', {
        **SOURCE, 'source_code_description': DESCRIPTION, 'ranking_model': 'jev'}, format='json')

    tokens = _tokens(response.data['alternatives'])
    assert tokens[lab['sodium'].concept_id] and tokens[lab['glucose'].concept_id] is None
    assert confidence_token.verify(
        tokens[lab['sodium'].concept_id], destination_concept_id=lab['sodium'].concept_id,
        source_description=DESCRIPTION, **SOURCE) == 0.9


def test_a_token_for_another_description_is_refused_on_save(lab, staff):
    """A score is about the text it was computed for; the row being saved must say the same."""
    token = _token(lab['glucose'], source_description='Totally different text')

    response = _create(staff, lab['glucose'], suggestion_confidence_token=token)

    assert response.status_code == 400 and 'suggestion_confidence_token' in response.data
    assert not SourceCodeConceptMapping.objects.filter(source_code='GLU-LOCAL').exists()


def test_the_description_is_matched_ignoring_case_and_spacing(lab, staff):
    token = _token(lab['glucose'], source_description='  GLUCOSE,   serum ')

    assert _create(staff, lab['glucose'], suggestion_confidence_token=token).status_code == 201


def test_a_token_cannot_follow_a_description_edit_on_save(lab, staff):
    mapping_id = _create(staff, lab['glucose']).data['mapping_id']

    response = _patch(staff, mapping_id, lab['glucose'], source_code_description='Something else',
                      suggestion_confidence_token=_token(lab['glucose']))

    assert response.status_code == 400 and _stored(mapping_id) is None


def test_scoring_an_existing_mapping_uses_its_stored_text_not_the_body(lab, staff, monkeypatch):
    SourceCodeConceptMapping.objects.create(
        **SOURCE, domain_id='Measurement', status='proposed', target_concept=lab['glucose'],
        source_code_description=DESCRIPTION, source_metadata={'fhir': {'code': {'text': 'stored metadata'}}},
    )
    seen = {}

    def rank(source_value, candidates, source_description, *args, source_context=None, **kwargs):
        seen['description'], seen['context'] = source_description, source_context
        chosen = candidates[0]
        return chosen, 'high confidence: ok', [
            {'concept_id': chosen['concept_id'], 'concept_name': chosen['concept_name'],
             'confidence': 0.9, 'ranker': 'anthropic'}]
    monkeypatch.setattr('omop_core.mapping.suggestions.rank_candidates', rank)

    response = _score(staff, lab['glucose'].concept_id, source_code_description='Chosen by the caller',
                      source_metadata={'fhir': {'code': {'text': 'chosen metadata'}}})

    assert seen['description'] == DESCRIPTION
    assert 'chosen metadata' not in str(seen['context']) and 'stored metadata' in str(seen['context'])
    # ... and the token is about the stored text, so it saves on that row and nowhere else.
    assert confidence_token.verify(
        response.data['scored']['token'], destination_concept_id=lab['glucose'].concept_id,
        source_description=DESCRIPTION, **SOURCE) == 0.9
    with pytest.raises(ValueError):
        confidence_token.verify(
            response.data['scored']['token'], destination_concept_id=lab['glucose'].concept_id,
            source_description='Chosen by the caller', **SOURCE)


def test_scoring_a_new_row_binds_the_description_it_was_given(lab, staff, monkeypatch):
    _stub_ranker(monkeypatch, lab['glucose'], confidence=0.8)

    response = _score(staff, lab['glucose'].concept_id, source_code_description='A new row description')

    assert confidence_token.verify(
        response.data['scored']['token'], destination_concept_id=lab['glucose'].concept_id,
        source_description='A new row description', **SOURCE) == 0.8


def test_re_pointing_the_source_clears_the_stored_confidence(lab, staff):
    for change in ({'source_code': 'OTHER-CODE'}, {'source_vocabulary_id': 'ICD10CM'}):
        SourceCodeConceptMapping.objects.all().delete()
        mapping_id = _create(staff, lab['glucose'], suggestion_confidence_token=_token(lab['glucose'])).data['mapping_id']
        assert _stored(mapping_id) == 0.82

        response = _patch(staff, mapping_id, lab['glucose'], **change)

        assert response.status_code == 200, response.content
        assert SourceCodeConceptMapping.objects.get(pk=mapping_id).suggestion_confidence is None, change


def test_a_save_that_changes_nothing_about_the_source_keeps_it(lab, staff):
    mapping_id = _create(staff, lab['glucose'], suggestion_confidence_token=_token(lab['glucose'])).data['mapping_id']

    _patch(staff, mapping_id, lab['glucose'], source_code=SOURCE['source_code'],
           source_vocabulary_id=SOURCE['source_vocabulary_id'], notes='checked')

    assert _stored(mapping_id) == 0.82


def _age(run, **delta):
    from datetime import timedelta
    from django.utils import timezone
    SuggestRun.objects.filter(pk=run.pk).update(created_at=timezone.now() - timedelta(**delta))


def test_a_stale_preview_stores_no_score_on_apply(lab, staff):
    row = _proposal_pointing_at(lab['sodium'])
    run = _preview(lab['glucose'], row, 0.77)
    _age(run, hours=25)

    response = _apply(staff, lab['glucose'], row, run)

    assert response.status_code == 200
    row.refresh_from_db()
    assert row.target_concept_id == lab['glucose'].concept_id and row.suggestion_confidence is None
    assert row.decision_confidence is None


def test_a_fresh_preview_still_stores_it(lab, staff):
    row = _proposal_pointing_at(lab['sodium'])
    run = _preview(lab['glucose'], row, 0.77)
    _age(run, hours=23)

    _apply(staff, lab['glucose'], row, run)

    row.refresh_from_db()
    assert row.suggestion_confidence == 0.77


def test_a_preview_of_other_text_stores_no_score_on_apply(lab, staff):
    """The row's description changed after the preview scored it."""
    row = _proposal_pointing_at(lab['sodium'])
    run = _preview(lab['glucose'], row, 0.77)
    SourceCodeConceptMapping.objects.filter(pk=row.pk).update(source_code_description='Hemoglobin A1c')
    row.refresh_from_db()

    _apply(staff, lab['glucose'], row, run)

    row.refresh_from_db()
    assert row.target_concept_id == lab['glucose'].concept_id and row.suggestion_confidence is None


def test_a_preview_matches_its_text_ignoring_case_and_spacing(lab, staff):
    row = _proposal_pointing_at(lab['sodium'])
    run = _preview(lab['glucose'], row, 0.77)
    SourceCodeConceptMapping.objects.filter(pk=row.pk).update(source_code_description='  GLUCOSE,  Serum')
    row.refresh_from_db()

    _apply(staff, lab['glucose'], row, run)

    row.refresh_from_db()
    assert row.suggestion_confidence == 0.77


def test_a_stale_preview_still_proposes_but_stores_no_score(lab, staff, monkeypatch):
    concept = lab['glucose']
    candidate = {'source_code': 'GLU-LOCAL', 'source_vocabulary_id': 'Local', 'mapping_id': None,
                 'source_code_description': DESCRIPTION, 'confidence': 0.77}
    run = SuggestRun.objects.create(
        direction='reverse', state=SuggestRun.SUCCESS,
        activity=[{'concept': {'concept_id': concept.concept_id}, 'candidates': [candidate]}],
    )
    _age(run, hours=48)
    monkeypatch.setattr('omop_core.mapping.reverse_catalog.refresh_catalog_candidate',
                        lambda cand, conc: dict(cand))

    response = staff.post(f'/api/v1/concept-to-code/{concept.concept_id}/mappings/', {
        'run_id': str(run.pk), 'source_code': 'GLU-LOCAL', 'source_vocabulary_id': 'Local',
    }, format='json')

    assert response.status_code == 201, response.content
    mapping = SourceCodeConceptMapping.objects.get(source_code='GLU-LOCAL')
    assert mapping.status == 'proposed' and mapping.suggestion_confidence is None
