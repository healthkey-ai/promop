"""SNOMED candidates judged by hierarchy, defining attributes and subsumption.

A small slice of SNOMED, shaped like the real thing on staging:

    Malignant lymphoma
      └ Non-Hodgkin's lymphoma
          └ Follicular lymphoma                (asso morph: Follicular lymphoma morphology)
              ├ Follicular lymphoma grade 3A
              └ Follicular NHL, small cleaved cell   <- ICD-10-CM C82.00 "Maps to"
    Osteoarthritis of knee      (finding site: Knee joint structure)
      ├ Osteoarthritis of left knee   (finding site: Left knee joint, laterality Left)
      └ Osteoarthritis of right knee  (finding site: Right knee joint, laterality Right)
                                    <- ICD-10-CM M17.11 "right knee" maps to the parent
"""
from datetime import date
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from omop_core.mapping import snomed_attributes as sa
from omop_core.models import (
    ConceptAncestor, ConceptRelationship, Relationship, SourceCodeConceptMapping,
)
from omop_core.services.mapping_suggestions import suggest_one_mapping
from tests.factories import ConceptClassFactory, ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


def relate(a, relationship_id, b):
    Relationship.objects.get_or_create(relationship_id=relationship_id, defaults={
        'relationship_name': relationship_id, 'is_hierarchical': 0, 'defines_ancestry': 0,
        'reverse_relationship_id': '', 'relationship_concept_id': 0})
    ConceptRelationship.objects.create(
        concept_1=a, concept_2=b, relationship_id=relationship_id,
        valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31))


@pytest.fixture()
def snomed():
    vocab = VocabularyFactory(vocabulary_id='SNOMED', vocabulary_name='SNOMED')
    condition = DomainFactory(domain_id='Condition', domain_name='Condition')
    spec = DomainFactory(domain_id='Spec Anatomic Site', domain_name='Spec Anatomic Site')
    observation = DomainFactory(domain_id='Observation', domain_name='Observation')
    procedure = DomainFactory(domain_id='Procedure', domain_name='Procedure')
    classes = {name: ConceptClassFactory(concept_class_id=name) for name in (
        'Disorder', 'Clinical Finding', 'Procedure', 'Body Structure', 'Morph Abnormality',
        'Qualifier Value', 'ICD10 code')}
    n = iter(range(4_100_000, 4_200_000))

    def concept(name, cls, domain=condition, standard='S'):
        return ConceptFactory(concept_id=next(n), concept_name=name, concept_code=str(next(n)),
                              vocabulary=vocab, domain=domain, concept_class=classes[cls],
                              standard_concept=standard)

    c = {}
    c['defined'] = concept('Defined', 'Qualifier Value', observation, None)
    c['primitive'] = concept('Primitive', 'Qualifier Value', observation, None)
    c['left'] = concept('Left', 'Qualifier Value', observation, None)
    c['right'] = concept('Right', 'Qualifier Value', observation, None)
    c['side'] = concept('Side', 'Qualifier Value', observation, None)
    c['malignant_lymphoma'] = concept('Malignant lymphoma', 'Disorder')
    c['nhl'] = concept("Non-Hodgkin's lymphoma", 'Disorder')
    c['fl'] = concept('Follicular lymphoma', 'Disorder')
    c['fl_3a'] = concept('Follicular lymphoma grade 3A', 'Disorder')
    c['fl_small'] = concept("Follicular non-Hodgkin's lymphoma, small cleaved cell", 'Disorder')
    c['lymphadenopathy'] = concept('Lymphadenopathy', 'Clinical Finding')
    c['node_biopsy'] = concept('Biopsy of lymph node', 'Procedure', procedure)
    c['fl_morph'] = concept('Follicular lymphoma (morphology)', 'Morph Abnormality', observation, None)
    c['knee'] = concept('Knee joint structure', 'Body Structure', spec)
    c['left_knee'] = concept('Structure of left knee joint', 'Body Structure', spec)
    c['right_knee'] = concept('Structure of right knee joint', 'Body Structure', spec)
    c['oa_knee'] = concept('Osteoarthritis of knee', 'Disorder')
    c['oa_left'] = concept('Osteoarthritis of left knee', 'Disorder')
    c['oa_right'] = concept('Osteoarthritis of right knee', 'Disorder')
    c['arthritis_right'] = concept('Arthritis of right knee', 'Disorder')
    c['pneumonia'] = concept('Pneumonia', 'Disorder')

    for name in ('fl', 'fl_3a', 'fl_small'):
        relate(c[name], 'Has asso morph', c['fl_morph'])
    relate(c['fl'], 'Has status', c['defined'])
    relate(c['nhl'], 'Has status', c['primitive'])
    relate(c['oa_knee'], 'Has finding site', c['knee'])
    relate(c['oa_left'], 'Has finding site', c['left_knee'])
    relate(c['oa_right'], 'Has finding site', c['right_knee'])
    relate(c['arthritis_right'], 'Has finding site', c['right_knee'])
    relate(c['left_knee'], 'Has laterality', c['left'])
    relate(c['right_knee'], 'Has laterality', c['right'])
    # As in real SNOMED: the unlateralised structure is lateralisable, "Side".
    relate(c['knee'], 'Has laterality', c['side'])

    tree = {
        'malignant_lymphoma': [], 'nhl': ['malignant_lymphoma'], 'fl': ['nhl', 'malignant_lymphoma'],
        'fl_3a': ['fl', 'nhl', 'malignant_lymphoma'], 'fl_small': ['fl', 'nhl', 'malignant_lymphoma'],
        'oa_knee': [], 'oa_left': ['oa_knee'], 'oa_right': ['oa_knee', 'arthritis_right'],
        'arthritis_right': [],
        'knee': [], 'left_knee': ['knee'], 'right_knee': ['knee'],
    }
    for child, ancestors in tree.items():
        ConceptAncestor.objects.create(ancestor_concept=c[child], descendant_concept=c[child],
                                       min_levels_of_separation=0, max_levels_of_separation=0)
        for level, ancestor in enumerate(ancestors, start=1):
            ConceptAncestor.objects.create(ancestor_concept=c[ancestor], descendant_concept=c[child],
                                           min_levels_of_separation=level, max_levels_of_separation=level)

    icd = VocabularyFactory(vocabulary_id='ICD10CM', vocabulary_name='ICD10CM')

    def icd_code(code, name, target):
        source = ConceptFactory(concept_id=next(n), concept_name=name, concept_code=code, vocabulary=icd,
                                domain=condition, concept_class=classes['ICD10 code'], standard_concept=None)
        relate(source, 'Maps to', target)
        return source

    c['c82'] = icd_code('C82.00', 'Follicular lymphoma grade I, unspecified site', c['fl_small'])
    c['m17'] = icd_code('M17.11', 'Unilateral primary osteoarthritis, right knee', c['oa_knee'])
    return c


def candidate(concept):
    return {'concept_id': concept.concept_id, 'concept_name': concept.concept_name,
            'concept_code': concept.concept_code, 'vocabulary_id': concept.vocabulary_id,
            'concept_class_id': concept.concept_class_id, 'domain_id': concept.domain_id,
            'standard_concept': concept.standard_concept, 'retrieval': 'lexical'}


def source_for(snomed, key, description=''):
    return sa.resolve_source(source_concept=snomed[key], description=description)


# --- the source side ------------------------------------------------------------

def test_an_icd10_code_brings_its_maps_to_target_as_the_reference(snomed):
    source = source_for(snomed, 'c82')
    assert [r['concept_id'] for r in source['references']] == [snomed['fl_small'].pk]
    assert source['reference_basis'] == sa.BASIS_MAPS_TO
    assert source['hierarchy']['value'] == 'Disorder'
    assert source['attributes']['Has asso morph'][0][1] == 'Follicular lymphoma (morphology)'


def test_laterality_comes_from_the_source_text_when_the_map_loses_it(snomed):
    source = source_for(snomed, 'm17')
    assert source['references'][0]['concept_id'] == snomed['oa_knee'].pk
    assert source['laterality'] == {'value': 'Right', 'basis': sa.BASIS_TEXT,
                                    'evidence': 'Named in the source text'}


@pytest.mark.parametrize('text, side', [
    ('Pain in left knee', 'Left'), ('RT shoulder', 'Right'), ('Bilateral cataract', 'Right and left'),
    ('Left and right hip', 'Right and left'), ('Leftover', None), ('Lymphoma', None),
])
def test_laterality_in_text(text, side):
    assert sa.laterality_in_text(text) == side


def test_curator_settings_override_and_are_validated(snomed):
    source = sa.resolve_source(source_concept=snomed['c82'], overrides={
        'reference_concept_id': snomed['fl'].pk, 'laterality': None, 'hierarchy': 'Disorder'})
    assert source['references'][0]['concept_id'] == snomed['fl'].pk
    assert source['reference_basis'] == sa.BASIS_CURATOR and source['laterality'] is None
    with pytest.raises(ValueError):
        sa.clean_overrides({'hierarchy': 'Recipe'})
    with pytest.raises(ValueError):
        sa.clean_overrides({'laterality': 'Up'})
    with pytest.raises(ValueError):
        sa.clean_overrides({'colour': 'red'})


# --- the rule -----------------------------------------------------------------

def test_exact_then_general_by_distance_and_more_specific_held_back(snomed):
    pool = [candidate(snomed[k]) for k in ('malignant_lymphoma', 'fl_3a', 'fl', 'fl_small', 'nhl')]
    eligible, excluded = sa.apply(pool, source_for(snomed, 'c82'))

    assert [c['concept_name'] for c in eligible] == [
        "Follicular non-Hodgkin's lymphoma, small cleaved cell",
        'Follicular lymphoma', "Non-Hodgkin's lymphoma", 'Malignant lymphoma']
    assert [c['snomed']['relation'] for c in eligible] == ['exact', 'general', 'general', 'general']
    assert [c['snomed']['levels'] for c in eligible] == [0, 1, 2, 3]
    # A sibling the source does not support is neither an ancestor nor exact.
    assert {c['concept_name']: c['exclusion_reason'] for c in excluded} == {
        'Follicular lymphoma grade 3A': sa.REASON_CONFLICT}


def test_a_descendant_of_the_reference_needs_curator_evidence(snomed):
    source = sa.resolve_source(source_concept=snomed['c82'],
                               overrides={'reference_concept_id': snomed['fl'].pk})
    eligible, excluded = sa.apply([candidate(snomed['fl_3a']), candidate(snomed['fl'])], source)
    assert [c['concept_name'] for c in eligible] == ['Follicular lymphoma']
    assert excluded[0]['concept_name'] == 'Follicular lymphoma grade 3A'
    assert excluded[0]['exclusion_reason'] == sa.REASON_SPECIFIC


def test_another_hierarchy_is_excluded(snomed):
    pool = [candidate(snomed[k]) for k in ('lymphadenopathy', 'node_biopsy', 'fl')]
    eligible, excluded = sa.apply(pool, source_for(snomed, 'c82'))
    assert [c['concept_name'] for c in eligible] == ['Follicular lymphoma']
    assert all('Hierarchy' in c['snomed_conflicts'] for c in excluded)


def test_the_side_the_map_dropped_is_restored_and_the_other_side_excluded(snomed):
    pool = [candidate(snomed[k]) for k in ('oa_left', 'oa_knee', 'arthritis_right', 'oa_right')]
    eligible, excluded = sa.apply(pool, source_for(snomed, 'm17'))

    # The map gives the unlateralised parent; the source text names the right
    # side, so its right-side child adds nothing unsupported and ranks first.
    assert [c['concept_name'] for c in eligible] == [
        'Osteoarthritis of right knee', 'Osteoarthritis of knee', 'Arthritis of right knee']
    assert eligible[0]['snomed']['lateralised'] is True
    assert eligible[1]['snomed']['laterality_not_captured'] == 'Right'
    # An ancestor on the right side is a valid general answer, not "more specific".
    assert eligible[2]['snomed']['relation'] == 'general'
    assert {c['concept_name']: c['snomed_conflicts'] for c in excluded} == {
        'Osteoarthritis of left knee': ['Laterality']}


def test_a_curator_reference_to_the_right_knee_makes_it_exact(snomed):
    source = sa.resolve_source(source_concept=snomed['m17'],
                               overrides={'reference_concept_id': snomed['oa_right'].pk})
    eligible, _ = sa.apply([candidate(snomed['oa_right']), candidate(snomed['oa_knee'])], source)
    assert [c['snomed']['relation'] for c in eligible] == ['exact', 'general']
    assert eligible[0]['snomed']['laterality'] == 'Right'


def test_primitive_gaps_are_unknown_not_conflicts(snomed):
    eligible, excluded = sa.apply([candidate(snomed['nhl'])], source_for(snomed, 'c82'))
    assert not excluded
    morph = next(a for a in eligible[0]['snomed']['attributes'] if a['label'] == 'Morphology')
    assert morph == {'relationship_id': 'Has asso morph', 'label': 'Morphology', 'values': [],
                     'verdict': 'unknown', 'primitive_gap': True}
    assert eligible[0]['snomed']['status'] == 'Primitive'


def test_without_evidence_candidates_are_only_described(snomed):
    source = sa.resolve_source(source_concept=None, description='lymphoma')
    eligible, excluded = sa.apply([candidate(snomed['fl_3a']), candidate(snomed['node_biopsy'])], source)
    assert not excluded and len(eligible) == 2
    assert eligible[0]['snomed']['semantic_tag'] == 'disorder'


def test_hierarchy_retrieval_offers_the_reference_and_its_ancestors(snomed):
    hits = sa.hierarchy_candidates(source_for(snomed, 'c82'), 'Condition')
    assert [h['concept_name'] for h in hits] == [
        "Follicular non-Hodgkin's lymphoma, small cleaved cell", 'Follicular lymphoma',
        "Non-Hodgkin's lymphoma", 'Malignant lymphoma']
    assert {h['retrieval'] for h in hits} == {sa.RETRIEVAL_HIERARCHY}


# --- the pipeline -------------------------------------------------------------

@pytest.fixture()
def ranker(monkeypatch, snomed):
    seen = {}

    def lexical(text, domain_id, limit=None):
        return [candidate(snomed[k]) for k in ('fl_3a', 'lymphadenopathy', 'pneumonia')]

    def rank(source_value, candidates, source_description='', **kwargs):
        seen['candidates'], seen['context'] = candidates, kwargs['source_context']
        return (candidates[0] if candidates else None), 'high confidence', []

    monkeypatch.setattr('omop_core.mapping.suggestions.lexical_candidates', lexical)
    monkeypatch.setattr('omop_core.mapping.suggestions.semantic_candidates', lambda *a, **k: [])
    monkeypatch.setattr('omop_core.mapping.suggestions.umls_candidates', lambda *a: ([], None))
    monkeypatch.setattr('omop_core.mapping.suggestions.rank_candidates', rank)
    return seen


def test_suggest_offers_the_mapped_concept_first_and_reports_what_it_held_back(ranker, snomed):
    result = suggest_one_mapping('C82.00', 'ICD10CM', 'condition', source_description='FL grade I')

    names = [c['concept_name'] for c in ranker['candidates']]
    assert names[0] == "Follicular non-Hodgkin's lymphoma, small cleaved cell"
    assert 'Follicular lymphoma grade 3A' not in names and 'Lymphadenopathy' not in names
    assert result['suggested']['retrieval'] == sa.RETRIEVAL_HIERARCHY
    assert ranker['context']['snomed']['references'][0]['concept_id'] == snomed['fl_small'].pk
    held = {c['concept_name']: c['exclusion_reason'] for c in result['axis_excluded']}
    assert held['Lymphadenopathy'] == sa.REASON_CONFLICT
    assert result['source_snomed']['hierarchy']['value'] == 'Disorder'


def test_jev_is_told_the_reference(settings):
    from omop_core.mapping.suggestions import rank_candidates_jev

    settings.JEV_API_KEY = 'key'
    with patch('requests.post') as post:
        post.return_value.json.return_value = {'answers': {'best_match': {'choice': 'none'}}}
        rank_candidates_jev('C82.00', [{
            'concept_id': 1, 'concept_code': '1', 'concept_name': 'Follicular lymphoma',
            'vocabulary_id': 'SNOMED', 'snomed': {'semantic_tag': 'disorder', 'relation': 'general',
                                                  'levels': 1, 'attributes': []},
        }], source_context={'code': 'C82.00', 'snomed': {'references': [{'concept_name': 'FL small cleaved'}]}})
    payload = post.call_args.kwargs['json']
    assert 'means at least FL small cleaved' in payload['state']
    assert 'more general than the source by 1' in payload['questions']['best_match']['criteria']['1']


# --- the API ------------------------------------------------------------------

class TestApi:
    @pytest.fixture(autouse=True)
    def _client(self, snomed):
        from patient_portal.models import Identity
        self.client = APIClient()
        self.client.force_authenticate(Identity.objects.create_user(
            email='snomed@example.test', is_staff=True, is_superuser=True))
        self.mapping = SourceCodeConceptMapping.objects.create(
            source_vocabulary_id='ICD10CM', source_code='M17.11', omop_table='condition_occurrence',
            domain_id='Condition', source_code_description='Unilateral primary osteoarthritis, right knee')
        self.url = f'/api/v1/code-mappings/{self.mapping.pk}/snomed-attributes/'
        self.snomed = snomed

    def test_get_shows_the_reference_and_laterality(self):
        response = self.client.get(self.url)
        assert response.status_code == 200, response.data
        assert response.data['applies'] is True
        assert response.data['source']['references'][0]['concept_name'] == 'Osteoarthritis of knee'
        assert response.data['source']['laterality']['value'] == 'Right'

    def test_put_saves_a_reference(self):
        response = self.client.put(self.url, {'snomed_attributes': {
            'reference_concept_id': self.snomed['oa_right'].pk}}, format='json')
        assert response.status_code == 200, response.data
        assert response.data['source']['reference_basis'] == 'curator'
        self.mapping.refresh_from_db()
        assert self.mapping.snomed_attributes == {'reference_concept_id': self.snomed['oa_right'].pk}

    def test_put_refuses_a_non_standard_reference(self):
        response = self.client.put(self.url, {'snomed_attributes': {
            'reference_concept_id': self.snomed['fl_morph'].pk}}, format='json')
        assert response.status_code == 400

    def test_non_admins_are_refused(self):
        from patient_portal.models import Identity
        client = APIClient()
        client.force_authenticate(Identity.objects.create_user(email='p@example.test'))
        assert client.get(self.url).status_code == 403



def test_without_evidence_the_order_is_left_alone(snomed):
    loinc = {'concept_id': 1, 'concept_name': 'Glucose [Mass/volume]', 'concept_code': '2345-7',
             'vocabulary_id': 'LOINC', 'concept_class_id': 'Lab Test', 'domain_id': 'Measurement'}
    pool = [loinc, candidate(snomed['fl']), candidate(snomed['pneumonia'])]
    eligible, excluded = sa.apply(pool, sa.resolve_source(source_concept=None, description='glucose'))
    assert [c['concept_id'] for c in eligible] == [1, snomed['fl'].pk, snomed['pneumonia'].pk]
    assert not excluded and 'snomed' in eligible[1]


def test_a_concept_sited_left_and_right_is_bilateral(snomed):
    both = ConceptFactory(concept_id=4199999, concept_name='Bilateral osteoarthritis of knees',
                          concept_code='4199999', vocabulary=snomed['oa_knee'].vocabulary,
                          domain=snomed['oa_knee'].domain, concept_class=snomed['oa_knee'].concept_class)
    relate(both, 'Has finding site', snomed['left_knee'])
    relate(both, 'Has finding site', snomed['right_knee'])
    assert sa.concept_facts([both.pk])[both.pk]['laterality'] == 'Right and left'


def test_none_as_laterality_means_no_side():
    assert sa.clean_overrides({'laterality': 'none'}) == {'laterality': None}
