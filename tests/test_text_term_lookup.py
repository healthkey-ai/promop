"""Text terms through code-mappings/lookup (#1944): one matcher, not two.

Glucose again, because LOINC splits it on the axes a document can settle:
2345-7 (MCnc, Ser/Plas), 14749-6 (SCnc, Ser/Plas), 2339-0 (MCnc, Bld),
41653-7 (MCnc, BldC, glucometer). Names are kept short so trigram retrieval
finds them from the bare word "Glucose".
"""
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from omop_core.mapping import axis_variants, text_terms
from omop_core.models import Concept, LoincClass, LoincCodeClass, SourceCodeAxisVariant, SourceCodeConceptMapping
from patient_portal.models import Identity
from tests.factories import ConceptClassFactory, ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

URL = '/api/v1/code-mappings/lookup/'
GLUCOSE = {
    #           concept_id name                 property system     method
    '2345-7': (3004501, 'Glucose SerPl MCnc', 'MCnc', 'Ser/Plas', ''),
    '14749-6': (3020491, 'Glucose SerPl SCnc', 'SCnc', 'Ser/Plas', ''),
    '2339-0': (3000483, 'Glucose Bld MCnc', 'MCnc', 'Bld', ''),
    '41653-7': (3031266, 'Glucose BldC meter', 'MCnc', 'BldC', 'Glucometer'),
}
NO_PICK = (None, 'No suitable concept: none fits.', None)


@pytest.fixture()
def glucose():
    domain = DomainFactory(domain_id='Measurement', domain_name='Measurement')
    vocabulary = VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC')
    lab = ConceptClassFactory(concept_class_id='Lab Test')
    LoincClass.objects.create(code='CHEM', display_name='Chemistry')
    concepts = {}
    for code, (cid, name, prop, system, method) in GLUCOSE.items():
        concepts[code] = ConceptFactory(
            concept_id=cid, concept_name=name, concept_code=code, vocabulary=vocabulary,
            domain=domain, concept_class=lab, standard_concept='S',
        )
        LoincCodeClass.objects.create(
            loinc_num=code, loinc_class_id='CHEM', component='Glucose', property=prop,
            time_aspect='Pt', system=system, scale_type='Qn', method_type=method,
        )
    return concepts


@pytest.fixture()
def client():
    api = APIClient()
    api.force_authenticate(Identity.objects.create_user(email='etl@example.test', is_staff=True))
    return api


def term(code, **context):
    return {'source_vocabulary_id': '', 'source_code': code, 'omop_table': 'measurement', **context}


def lookup(client, *entries):
    response = client.post(URL, {'codes': list(entries)}, format='json')
    assert response.status_code == 200, response.data
    return response.data


def approve(code, concept, **fields):
    return SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='', source_code=code, source_code_description=code,
        target_concept=concept, destination_vocabulary_id='LOINC', domain_id='Measurement',
        omop_table='measurement', status='approved', origin='import', **fields)


def test_a_curated_term_resolves_whatever_its_case(client, glucose):
    approve('glucose', glucose['2345-7'])

    data = lookup(client, term('Glucose', source_unit='mg/dL', quantity=95))

    entry = data['mappings']['|Glucose']
    assert entry['resolved'] and entry['match'] == 'resolved'
    assert entry['target_concept_code'] == '2345-7'
    assert data['matcher_version'].startswith('text-terms-')
    # The unit is kept as evidence on the approved row; its destination is untouched.
    row = SourceCodeConceptMapping.objects.get(source_code='glucose')
    assert row.source_unit_evidence == [{'code': 'mg/dL', 'display': 'mg/dL', 'count': 1}]
    assert row.status == 'approved' and row.target_concept == glucose['2345-7']


def test_a_term_in_a_variants_unit_takes_the_variants_loinc(client, glucose):
    mapping = approve('glucose', glucose['2345-7'], source_unit_evidence=[
        {'code': 'mg/dL', 'count': 5}, {'code': 'mmol/L', 'count': 3}])
    axis_variants.refresh(mapping)
    variant = SourceCodeAxisVariant.objects.get(mapping=mapping, axis_key='property=SCnc|scale_type=Qn')
    axis_variants.set_destination(variant, glucose['14749-6'], approve=True, user=None)

    data = lookup(client, term('glucose', source_unit='mmol/L'), term('glucose', source_unit='mg/dL'))

    routed = data['mappings']['|glucose|mmol/L']
    assert routed['target_concept_code'] == '14749-6' and routed['match'] == 'resolved'
    assert data['mappings']['|glucose']['target_concept_code'] == '2345-7'


def test_an_unknown_term_is_queued_with_its_context_and_axis_filtered_candidates(client, glucose):
    with patch('omop_core.mapping.suggestions.rank_candidates', return_value=NO_PICK) as rank:
        data = lookup(client, term('Glucose', source_unit='mmol/L', quantity=5.1, specimen='serum'))

    entry = data['mappings']['|Glucose']
    assert not entry['resolved'] and entry['match'] == 'ambiguous'
    # Only the substance-concentration serum code fits mmol/L in serum.
    assert [c['concept_code'] for c in entry['candidates']] == ['14749-6']
    assert entry['candidates'][0]['axes_matched'] >= 2
    # The suggestion Rule 4 asked the ranker for was filtered by the same axes.
    offered = {c['concept_code'] for c in rank.call_args.args[1]}
    assert offered == {'14749-6'}
    assert rank.call_args.kwargs['source_context']['loinc_axes']['property'] == 'SCnc'

    row = SourceCodeConceptMapping.objects.get(source_vocabulary_id='', source_code='Glucose')
    assert row.status == 'proposed' and row.target_concept is None
    assert row.source_metadata['fhir']['valueQuantity'] == {'value': 5.1, 'unit': 'mmol/L'}
    assert row.source_metadata['fhir']['specimen'] == {'text': 'serum'}
    assert row.source_unit_evidence == [{'code': 'mmol/L', 'display': 'mmol/L', 'count': 1}]
    assert row.domain_id == 'Measurement' and row.omop_table == 'measurement'
    # No placeholder concept for a document phrasing.
    assert not Concept.objects.filter(vocabulary_id='HK-Labs').exists()


def test_a_second_sighting_counts_and_does_not_ask_the_ranker_again(client, glucose):
    with patch('omop_core.mapping.suggestions.rank_candidates', return_value=NO_PICK) as rank:
        lookup(client, term('Glucose', source_unit='mg/dL'))
        lookup(client, term('glucose', source_unit='mg/dL'))

    assert rank.call_count == 1
    row = SourceCodeConceptMapping.objects.get(source_vocabulary_id='')
    assert row.occurrence_count == 2
    assert row.source_unit_evidence == [{'code': 'mg/dL', 'display': 'mg/dL', 'count': 2}]


def test_a_term_seen_in_two_units_is_flagged_with_axis_variants(client, glucose):
    approve('glucose', glucose['2345-7'])

    lookup(client, term('glucose', source_unit='mg/dL'))
    assert SourceCodeConceptMapping.objects.get(source_code='glucose').axis_variant_count == 0
    lookup(client, term('glucose', source_unit='mmol/L'))

    row = SourceCodeConceptMapping.objects.get(source_code='glucose')
    assert row.axis_variant_count == 2
    assert set(row.axis_variants.values_list('axis_key', flat=True)) == {
        'property=MCnc|scale_type=Qn', 'property=SCnc|scale_type=Qn'}


def test_a_term_nothing_matches_is_unresolved_with_no_candidates(client, glucose):
    with patch('omop_core.mapping.suggestions.rank_candidates', return_value=NO_PICK):
        data = lookup(client, term('Troponin I'))

    entry = data['mappings']['|Troponin I']
    assert not entry['resolved'] and entry['match'] == 'unresolved' and entry['candidates'] == []
    assert SourceCodeConceptMapping.objects.filter(source_code='Troponin I', target_concept=None).exists()


def test_wording_is_normalised_to_one_queue_row(client, glucose):
    long = 'Glucose ' * 30
    with patch('omop_core.mapping.suggestions.rank_candidates', return_value=NO_PICK):
        lookup(client, term('  glucose   level '), term('Glucose level'), term(long), term(long.strip()))

    rows = SourceCodeConceptMapping.objects.filter(source_vocabulary_id='')
    assert sorted(rows.values_list('source_code', flat=True)) == [
        text_terms.normalize_term(long), 'glucose level']
    assert rows.get(source_code='glucose level').occurrence_count == 2


def test_a_blank_term_is_rejected(client):
    response = client.post(URL, {'codes': [term('   ')]}, format='json')
    assert response.status_code == 400
    assert 'source_code' in response.data['detail']
