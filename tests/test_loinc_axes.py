"""The six LOINC axes in Code to Concept Suggest, and axis variants.

Glucose is the running example because LOINC splits it on every axis the
feature cares about: 2345-7 (mass concentration, serum or plasma), 14749-6
(substance concentration, serum or plasma), 2339-0 (mass concentration, whole
blood), 41653-7 (capillary blood, by glucometer).
"""
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from omop_core.mapping import axis_variants, loinc_axes
from omop_core.mapping.code_resolution import resolve_source_code
from omop_core.models import (
    LoincClass, LoincCodeClass, PatientSourceCode, SourceCodeAxisVariant,
    SourceCodeConceptMapping,
)
from omop_core.services.mapping_suggestions import suggest_one_mapping
from tests.factories import (
    ConceptClassFactory, ConceptFactory, DomainFactory, MeasurementFactory, PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db

LAB = 'ACME-LAB'
GLUCOSE = {
    #          concept_id component    property time  system      scale  method
    '2345-7': (3004501, 'Glucose', 'MCnc', 'Pt', 'Ser/Plas', 'Qn', ''),
    '14749-6': (3020491, 'Glucose', 'SCnc', 'Pt', 'Ser/Plas', 'Qn', ''),
    '2339-0': (3000483, 'Glucose', 'MCnc', 'Pt', 'Bld', 'Qn', ''),
    '41653-7': (3031266, 'Glucose', 'MCnc', 'Pt', 'BldC', 'Qn', 'Glucometer'),
}


@pytest.fixture()
def glucose():
    domain = DomainFactory(domain_id='Measurement', domain_name='Measurement')
    vocabulary = VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC')
    lab = ConceptClassFactory(concept_class_id='Lab Test')
    LoincClass.objects.create(code='CHEM', display_name='Chemistry')
    concepts = {}
    for code, (cid, component, prop, time, system, scale, method) in GLUCOSE.items():
        concepts[code] = ConceptFactory(
            concept_id=cid, concept_name=f'{component} [{prop}] in {system}', concept_code=code,
            vocabulary=vocabulary, domain=domain, concept_class=lab, standard_concept='S',
        )
        LoincCodeClass.objects.create(
            loinc_num=code, loinc_class_id='CHEM', component=component, property=prop,
            time_aspect=time, system=system, scale_type=scale, method_type=method,
        )
    return concepts


def candidate(concept, retrieval='lexical'):
    return {
        'concept_id': concept.concept_id, 'concept_name': concept.concept_name,
        'concept_code': concept.concept_code, 'vocabulary_id': concept.vocabulary_id,
        'concept_class_id': concept.concept_class_id, 'domain_id': 'Measurement',
        'standard_concept': 'S', 'retrieval': retrieval,
    }


def settled(**values):
    return {axis: {'value': value, 'basis': 'curator', 'evidence': ''} for axis, value in values.items()}


# --- comparing axes ---------------------------------------------------------------

class TestCompare:
    def test_same_value_ignoring_case_and_spacing_matches(self):
        assert loinc_axes.compare('component', ' glucose ', 'Glucose') == loinc_axes.MATCH

    def test_serum_is_compatible_with_serum_or_plasma(self):
        assert loinc_axes.compare('system', 'Ser', 'Ser/Plas') == loinc_axes.COMPATIBLE
        assert loinc_axes.compare('system', 'Ser/Plas', 'Ser') == loinc_axes.COMPATIBLE

    def test_a_different_specimen_conflicts(self):
        assert loinc_axes.compare('system', 'Ser', 'Bld') == loinc_axes.CONFLICT

    def test_a_challenge_component_is_a_different_test(self):
        assert loinc_axes.compare(
            'component', 'Glucose', 'Glucose^2H post 75 g glucose PO') == loinc_axes.CONFLICT

    def test_an_unsettled_axis_or_unknown_candidate_is_unknown(self):
        assert loinc_axes.compare('method_type', None, 'Glucometer') == loinc_axes.UNKNOWN
        assert loinc_axes.compare('method_type', '', None) == loinc_axes.UNKNOWN

    def test_methodless_conflicts_with_a_method(self):
        assert loinc_axes.compare('method_type', '', 'Glucometer') == loinc_axes.CONFLICT
        assert loinc_axes.compare('method_type', '', '') == loinc_axes.MATCH


class TestApply:
    def test_conflicting_candidates_are_excluded_with_the_axis_they_broke(self, glucose):
        pool = [candidate(c) for c in glucose.values()]
        eligible, excluded = loinc_axes.apply(pool, settled(property='SCnc'))

        assert [c['concept_code'] for c in eligible] == ['14749-6']
        assert {c['concept_code']: c['axis_conflicts'] for c in excluded} == {
            '2345-7': ['property'], '2339-0': ['property'], '41653-7': ['property'],
        }

    def test_eligible_candidates_are_ranked_by_axes_matched(self, glucose):
        pool = [candidate(glucose[code]) for code in ('2339-0', '41653-7', '2345-7')]
        eligible, excluded = loinc_axes.apply(
            pool, settled(component='Glucose', property='MCnc', system='Ser/Plas'))

        # 2339-0 (Bld) and 41653-7 (BldC) conflict on System; 2345-7 matches all three.
        assert [c['concept_code'] for c in eligible] == ['2345-7']
        assert eligible[0]['axes_matched'] == 3
        assert eligible[0]['loinc_axes']['system'] == 'Ser/Plas'
        assert {c['concept_code'] for c in excluded} == {'2339-0', '41653-7'}

    def test_more_matches_rank_above_fewer_and_compatible_above_unknown(self, glucose):
        pool = [candidate(glucose['14749-6']), candidate(glucose['2345-7'])]
        eligible, _ = loinc_axes.apply(pool, settled(component='Glucose', system='Ser'))
        # Both match component and are compatible on system: retrieval order holds.
        assert [c['concept_code'] for c in eligible] == ['14749-6', '2345-7']
        assert all(c['axis_match']['system'] == 'compatible' for c in eligible)

        eligible, _ = loinc_axes.apply(pool, settled(component='Glucose', property='MCnc'))
        assert [c['concept_code'] for c in eligible] == ['2345-7']

    def test_a_non_loinc_candidate_is_kept_and_ranked_below_matches(self, glucose):
        snomed = ConceptFactory(vocabulary=VocabularyFactory(vocabulary_id='SNOMED'),
                                domain=DomainFactory(domain_id='Measurement'))
        pool = [candidate(snomed), candidate(glucose['2345-7'])]
        eligible, excluded = loinc_axes.apply(pool, settled(property='MCnc'))
        assert [c['concept_id'] for c in eligible] == [glucose['2345-7'].pk, snomed.pk]
        assert eligible[1]['loinc_axes'] is None and not excluded


# --- the source side --------------------------------------------------------------

class TestInfer:
    def test_a_loinc_source_code_settles_all_six(self, glucose):
        axes, _ = loinc_axes.infer('LOINC', '41653-7', {})
        assert loinc_axes.values(axes) == {
            'component': 'Glucose', 'property': 'MCnc', 'time_aspect': 'Pt',
            'system': 'BldC', 'scale_type': 'Qn', 'method_type': 'Glucometer',
        }
        assert {entry['basis'] for entry in axes.values()} == {loinc_axes.BASIS_SOURCE_CODE}

    def test_a_loinc_coding_on_the_resource_settles_them(self, glucose):
        axes, _ = loinc_axes.infer(LAB, 'GLU', {'fhir': {'code': {'coding': [
            {'system': 'urn:oid:1.2.3', 'code': 'GLU'},
            {'system': 'http://loinc.org', 'code': '2345-7'},
        ]}}})
        assert axes['system']['value'] == 'Ser/Plas'
        assert axes['system']['basis'] == loinc_axes.BASIS_FHIR_CODING
        assert '2345-7' in axes['system']['evidence']

    def test_a_unit_settles_property_and_scale(self):
        axes, _ = loinc_axes.infer(LAB, 'GLU', {'fhir': {'valueQuantity': {'value': 5.4, 'unit': 'mmol/L'}}})
        assert loinc_axes.values(axes) == {'property': 'SCnc', 'scale_type': 'Qn'}
        assert axes['property']['evidence'] == 'valueQuantity.unit = mmol/L'

    def test_an_ambiguous_unit_settles_only_the_scale(self):
        axes, _ = loinc_axes.infer(LAB, 'A1C', {'fhir': {'valueQuantity': {'value': 6.1, 'unit': '%'}}})
        assert loinc_axes.values(axes) == {'scale_type': 'Qn'}

    def test_the_code_level_aggregate_settles_scale_from_value_types(self):
        axes, _ = loinc_axes.infer(LAB, 'GLU', {'value_types': {'quantity': 99.5}})
        assert loinc_axes.values(axes) == {'scale_type': 'Qn'}
        axes, _ = loinc_axes.infer(LAB, 'GLU', {'value_types': {'quantity': 60.0}})
        assert axes == {}

    def test_a_specimen_display_settles_system_but_a_name_never_does(self):
        axes, _ = loinc_axes.infer(LAB, 'GLU', {'fhir': {'specimen': {'display': 'Urine'}}})
        assert axes['system']['value'] == 'Urine'
        axes, _ = loinc_axes.infer(LAB, 'BUN', {'fhir': {'code': {'text': 'Blood urea nitrogen'}}})
        assert 'system' not in axes

    def test_method_and_period_are_hints_not_constraints(self):
        axes, hints = loinc_axes.infer(LAB, 'GLU', {'fhir': {
            'method': {'text': 'Hexokinase'}, 'effectivePeriod': {},
            'specimen': {'display': 'Drain fluid'},
        }})
        assert axes == {}
        assert set(hints) == {'method_type', 'time_aspect', 'system'}

    def test_curator_overrides_set_clear_and_mark_methodless(self, glucose):
        axes, _ = loinc_axes.resolve('LOINC', '41653-7', {}, {
            'system': 'Bld', 'component': None, 'method_type': '',
        })
        assert axes['system'] == {'value': 'Bld', 'basis': 'curator', 'evidence': 'Set by a curator'}
        assert 'component' not in axes
        assert axes['method_type']['value'] == ''

    def test_overrides_are_validated(self):
        assert loinc_axes.clean_overrides({'system': ' Ser '}) == {'system': 'Ser'}
        with pytest.raises(ValueError, match='Unknown'):
            loinc_axes.clean_overrides({'colour': 'red'})
        with pytest.raises(ValueError):
            loinc_axes.clean_overrides({'system': 5})
        with pytest.raises(ValueError):
            loinc_axes.clean_overrides(['system'])


def test_axis_retrieval_finds_the_code_the_axes_describe(glucose):
    hits = loinc_axes.axis_candidates(
        settled(component='Glucose', property='SCnc', system='Ser'), 'Measurement')
    assert [hit['concept_code'] for hit in hits] == ['14749-6']
    assert hits[0]['retrieval'] == loinc_axes.RETRIEVAL_AXES
    assert loinc_axes.axis_candidates(settled(property='SCnc'), 'Measurement') == []


# --- the pipeline -----------------------------------------------------------------

@pytest.fixture()
def pipeline(glucose, monkeypatch):
    """Lexical returns every glucose code; the ranker records what it saw."""
    seen = {}

    def lexical(text, domain_id, limit=None):
        return [candidate(glucose[code]) for code in ('2339-0', '41653-7', '2345-7', '14749-6')]

    def rank(source_value, candidates, source_description='', **kwargs):
        seen['candidates'] = candidates
        seen['context'] = kwargs['source_context']
        return (candidates[0] if candidates else None), 'high confidence: axes', []

    monkeypatch.setattr('omop_core.mapping.suggestions.lexical_candidates', lexical)
    monkeypatch.setattr('omop_core.mapping.suggestions.semantic_candidates', lambda *a, **k: [])
    monkeypatch.setattr('omop_core.mapping.suggestions.umls_candidates', lambda *a: ([], None))
    monkeypatch.setattr('omop_core.mapping.suggestions.rank_candidates', rank)
    return seen


def test_the_ranker_never_sees_a_conflicting_candidate(pipeline, glucose):
    result = suggest_one_mapping(
        'GLU', LAB, 'measurement', source_description='Glucose',
        row_metadata={'fhir': {'valueQuantity': {'value': 5.4, 'unit': 'mmol/L'}}},
    )

    assert [c['concept_code'] for c in pipeline['candidates']] == ['14749-6']
    assert result['suggested']['concept_code'] == '14749-6'
    assert pipeline['context']['loinc_axes'] == {'property': 'SCnc', 'scale_type': 'Qn'}
    assert {c['concept_code'] for c in result['axis_excluded']} == {'2339-0', '41653-7', '2345-7'}
    assert result['source_axes']['axes']['property']['basis'] == loinc_axes.BASIS_UNIT


def test_a_curator_component_adds_the_code_found_by_its_axes(pipeline, glucose, monkeypatch):
    monkeypatch.setattr('omop_core.mapping.suggestions.lexical_candidates', lambda *a, **k: [])
    result = suggest_one_mapping(
        'GLU', LAB, 'measurement', source_description='GLU',
        axis_overrides={'component': 'Glucose', 'property': 'MCnc', 'system': 'Bld'},
    )
    assert [c['concept_code'] for c in pipeline['candidates']] == ['2339-0']
    assert result['suggested']['retrieval'] == 'axes'


def test_when_everything_conflicts_the_note_says_which_axis(pipeline, glucose):
    result = suggest_one_mapping(
        'GLU', LAB, 'measurement', source_description='Glucose',
        axis_overrides={'system': 'Urine'},
    )
    assert result['suggested'] is None
    assert 'conflicted' in result['note'] and 'System' in result['note']


def test_a_domain_without_loinc_is_left_alone(glucose, monkeypatch):
    monkeypatch.setattr('omop_core.mapping.suggestions.lexical_candidates', lambda *a, **k: [])
    monkeypatch.setattr('omop_core.mapping.suggestions.semantic_candidates', lambda *a, **k: [])
    monkeypatch.setattr('omop_core.mapping.suggestions.umls_candidates', lambda *a: ([], None))
    DomainFactory(domain_id='Condition', domain_name='Condition')
    result = suggest_one_mapping('C90.0', 'ICD10CM', 'condition', source_description='MM')
    assert 'source_axes' not in result


def test_jev_is_told_the_axes(settings):
    from omop_core.mapping.suggestions import rank_candidates_jev

    settings.JEV_API_KEY = 'key'
    with patch('requests.post') as post:
        post.return_value.json.return_value = {'answers': {'best_match': {'choice': 'none'}}}
        rank_candidates_jev('GLU', [{
            'concept_id': 1, 'concept_code': '2345-7', 'concept_name': 'Glucose',
            'vocabulary_id': 'LOINC', 'loinc_axes': {'property': 'MCnc'},
        }], source_context={'code': 'GLU', 'loinc_axes': {'property': 'SCnc'}})
    payload = post.call_args.kwargs['json']
    assert 'Property=SCnc' in payload['state']
    assert 'Property=MCnc' in payload['questions']['best_match']['criteria']['1']


# --- axis variants ----------------------------------------------------------------

@pytest.fixture()
def two_unit_code(glucose):
    """A hospital glucose code that arrives in mg/dL for two patients and mmol/L for one."""
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='GLU', source_code_description='Glucose',
        omop_table='measurement', domain_id='Measurement', status='approved',
        target_concept=glucose['2345-7'],
    )
    for unit, count in (('mg/dL', 7), ('mg/dL', 3), ('mmol/L', 4)):
        PatientSourceCode.objects.create(
            person=PersonFactory(), source_value='GLU', source_vocabulary_id=LAB,
            omop_table='measurement', source_unit=unit, occurrence_count=count,
        )
    return mapping


def test_units_that_imply_different_properties_are_variants(two_unit_code):
    variants, notes = axis_variants.detect(two_unit_code)
    assert [(v['axis_key'], v['units'], v['occurrence_count']) for v in variants] == [
        ('property=MCnc|scale_type=Qn', ['mg/dL'], 10),
        ('property=SCnc|scale_type=Qn', ['mmol/L'], 4),
    ]
    assert notes == []


def test_one_property_in_two_spellings_is_one_variant(glucose):
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='NA', omop_table='measurement')
    for unit in ('mmol/L', 'mEq/L'):
        PatientSourceCode.objects.create(person=PersonFactory(), source_value='NA',
                                         source_vocabulary_id=LAB, omop_table='measurement',
                                         source_unit=unit)
    assert len(axis_variants.detect(mapping)[0]) == 1
    assert axis_variants.refresh(mapping) == []
    mapping.refresh_from_db()
    assert mapping.axis_variant_count == 0


def test_refresh_flags_the_code_and_keeps_a_curators_choice(two_unit_code, glucose):
    stored = axis_variants.refresh(two_unit_code)
    two_unit_code.refresh_from_db()
    assert two_unit_code.axis_variant_count == 2 and len(stored) == 2

    substance = SourceCodeAxisVariant.objects.get(axis_key='property=SCnc|scale_type=Qn')
    axis_variants.set_destination(substance, glucose['14749-6'], approve=False, user=None)
    axis_variants.refresh(two_unit_code)
    substance.refresh_from_db()
    assert substance.target_concept_id == glucose['14749-6'].pk


def test_upload_hook_refreshes_only_multi_unit_codes(two_unit_code):
    single = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='K', omop_table='measurement')
    PatientSourceCode.objects.create(person=PersonFactory(), source_value='K',
                                     source_vocabulary_id=LAB, omop_table='measurement',
                                     source_unit='mmol/L')
    assert axis_variants.refresh_multi_unit_codes({(LAB, 'GLU'), (LAB, 'K')}) == 1
    two_unit_code.refresh_from_db()
    single.refresh_from_db()
    assert (two_unit_code.axis_variant_count, single.axis_variant_count) == (2, 0)


@pytest.fixture()
def second_two_unit_code(two_unit_code):
    """Another multi-unit code, and a stale flag on a code that stopped varying."""
    other = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='GLU2', omop_table='measurement')
    for unit in ('mg/dL', 'mmol/L'):
        PatientSourceCode.objects.create(person=PersonFactory(), source_value='GLU2',
                                         source_vocabulary_id=LAB, omop_table='measurement',
                                         source_unit=unit)
    stale = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='OLD', omop_table='measurement', axis_variant_count=2)
    return two_unit_code, other, stale


def _counts(*mappings):
    for mapping in mappings:
        mapping.refresh_from_db()
    return [m.axis_variant_count for m in mappings]


def test_refresh_all_works_through_every_candidate_in_batches(second_two_unit_code):
    assert axis_variants.multi_unit_keys() == {(LAB, 'GLU'), (LAB, 'GLU2'), (LAB, 'OLD')}
    assert axis_variants.refresh_all(batch_size=1) == 3
    assert _counts(*second_two_unit_code) == [2, 2, 0]


def test_one_failing_code_does_not_stop_the_rest(second_two_unit_code):
    real = axis_variants.refresh

    def refresh(mapping, *args, **kwargs):
        if mapping.source_code == 'GLU':
            # A database error, which would abort the batch without a savepoint.
            SourceCodeConceptMapping.objects.raw('SELECT * FROM no_such_table')[0]
        return real(mapping, *args, **kwargs)

    with patch.object(axis_variants, 'refresh', refresh):
        assert axis_variants.refresh_all() == 2
    assert _counts(*second_two_unit_code) == [0, 2, 0]


def _backfill():
    import importlib

    return importlib.import_module('omop_core.migrations.0300_backfill_axis_variants').backfill


def test_the_migration_backfills_existing_codes(second_two_unit_code):
    _backfill()(None, None)
    assert _counts(*second_two_unit_code) == [2, 2, 0]


def test_the_migration_does_not_block_a_deploy_on_a_database_error(two_unit_code):
    from django.db import ProgrammingError

    with patch.object(axis_variants, 'refresh_all', side_effect=ProgrammingError('no column')):
        _backfill()(None, None)
    assert _counts(two_unit_code) == [0]


def test_the_command_reports_and_refreshes(second_two_unit_code):
    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    call_command('refresh_axis_variants', '--dry-run', stdout=out)
    assert '3 source codes have more than one unit.' in out.getvalue()
    assert _counts(*second_two_unit_code) == [0, 0, 2]
    call_command('refresh_axis_variants', stdout=out)
    assert 'Refreshed 3 mappings; 2 now have axis variants.' in out.getvalue()


def test_specimen_variation_is_reported_but_not_routed(glucose):
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='GLU2', omop_table='measurement')
    for specimen in ('Serum', 'Urine'):
        PatientSourceCode.objects.create(
            person=PersonFactory(), source_value='GLU2', source_vocabulary_id=LAB,
            omop_table='measurement', source_unit='mg/dL',
            source_metadata={'fhir': {'specimen': {'display': specimen}}})
    variants, notes = axis_variants.detect(mapping)
    assert len(variants) == 1
    assert notes and 'Ser' in notes[0] and 'Urine' in notes[0]


class TestRouting:
    @pytest.fixture(autouse=True)
    def _approved_substance_variant(self, two_unit_code, glucose):
        axis_variants.refresh(two_unit_code)
        variant = SourceCodeAxisVariant.objects.get(axis_key='property=SCnc|scale_type=Qn')
        axis_variants.set_destination(variant, glucose['14749-6'], approve=True, user=None)
        two_unit_code.refresh_from_db()
        self.glucose = glucose

    def resolve(self, unit):
        return resolve_source_code(source_vocabulary_id=LAB, source_code='GLU',
                                   omop_table='measurement', source_unit=unit)[0]

    def test_a_record_in_the_variants_unit_takes_its_concept(self):
        assert self.resolve('mmol/L') == self.glucose['14749-6']

    def test_another_unit_with_the_same_property_routes_by_property(self):
        assert self.resolve('umol/L') == self.glucose['14749-6']

    def test_other_units_and_no_unit_keep_the_mappings_concept(self):
        assert self.resolve('mg/dL') == self.glucose['2345-7']
        assert self.resolve(None) == self.glucose['2345-7']
        assert self.resolve('%') == self.glucose['2345-7']

    def test_lookup_api_keys_a_routed_entry_by_unit(self):
        from patient_portal.models import Identity
        client = APIClient()
        client.force_authenticate(Identity.objects.create_user(email='etl@example.test', is_staff=True))
        response = client.post('/api/v1/code-mappings/lookup/', {'codes': [
            {'source_vocabulary_id': LAB, 'source_code': 'GLU', 'omop_table': 'measurement',
             'source_unit': unit} for unit in ('mg/dL', 'mmol/L')
        ]}, format='json')
        assert response.status_code == 200, response.data
        results = response.data['mappings']
        assert results[f'{LAB}|GLU']['target_concept_code'] == '2345-7'
        routed = results[f'{LAB}|GLU|mmol/L']
        assert routed['target_concept_code'] == '14749-6'
        assert routed['axis_values'] == {'property': 'SCnc', 'scale_type': 'Qn'}


def test_approving_a_variant_repoints_only_its_units(two_unit_code, glucose):
    person = PersonFactory()
    in_mmol = MeasurementFactory(person=person, measurement_concept=glucose['2345-7'],
                                 measurement_source_value='GLU', unit_source_value='mmol/L',
                                 value_as_number=5.4)
    in_mg = MeasurementFactory(person=person, measurement_concept=glucose['2345-7'],
                               measurement_source_value='GLU', unit_source_value='mg/dL',
                               value_as_number=97, measurement_date='2024-01-16')
    variant = {v.axis_key: v for v in axis_variants.refresh(two_unit_code)}['property=SCnc|scale_type=Qn']

    result = axis_variants.set_destination(variant, glucose['14749-6'], approve=True, user=None)

    in_mmol.refresh_from_db()
    in_mg.refresh_from_db()
    assert result['rows_updated'] == 1
    assert in_mmol.measurement_concept_id == glucose['14749-6'].pk
    assert in_mg.measurement_concept_id == glucose['2345-7'].pk


# --- the API ----------------------------------------------------------------------

class TestApi:
    @pytest.fixture(autouse=True)
    def _client(self, two_unit_code):
        from patient_portal.models import Identity
        self.client = APIClient()
        self.user = Identity.objects.create_user(email='axes@example.test', is_staff=True, is_superuser=True)
        self.client.force_authenticate(self.user)
        self.mapping = two_unit_code
        self.url = f'/api/v1/code-mappings/{two_unit_code.pk}/loinc-axes/'

    def test_get_shows_axes_basis_and_detected_variants(self):
        response = self.client.get(self.url)
        assert response.status_code == 200, response.data
        assert response.data['applies'] is True
        assert [v['axis_key'] for v in response.data['variants']] == [
            'property=MCnc|scale_type=Qn', 'property=SCnc|scale_type=Qn']
        assert response.data['variants'][0]['id'] is None

    def test_put_saves_overrides_and_they_resolve(self):
        response = self.client.put(self.url, {'loinc_axes': {
            'component': 'Glucose', 'system': 'Ser/Plas', 'method_type': '',
        }}, format='json')
        assert response.status_code == 200, response.data
        self.mapping.refresh_from_db()
        assert self.mapping.loinc_axes == {'component': 'Glucose', 'system': 'Ser/Plas', 'method_type': ''}
        axes = response.data['source_axes']['axes']
        assert axes['component']['basis'] == 'curator' and axes['method_type']['value'] == ''

    def test_put_rejects_an_unknown_axis(self):
        response = self.client.put(self.url, {'loinc_axes': {'colour': 'red'}}, format='json')
        assert response.status_code == 400

    def test_approving_a_variant_needs_a_destination(self, glucose):
        url = f'/api/v1/code-mappings/{self.mapping.pk}/axis-variants/'
        response = self.client.post(url, {'axis_key': 'property=SCnc|scale_type=Qn', 'approve': True},
                                    format='json')
        assert response.status_code == 400
        response = self.client.post(url, {
            'axis_key': 'property=SCnc|scale_type=Qn', 'approve': True,
            'target_concept_id': glucose['14749-6'].pk,
        }, format='json')
        assert response.status_code == 200, response.data
        approved = [v for v in response.data['variants'] if v['status'] == 'approved']
        assert approved[0]['target_concept']['concept_code'] == '14749-6'
        assert 'repoint' in response.data

    def test_an_unknown_variant_is_rejected(self, glucose):
        response = self.client.post(f'/api/v1/code-mappings/{self.mapping.pk}/axis-variants/', {
            'axis_key': 'property=NCnc', 'target_concept_id': glucose['14749-6'].pk,
        }, format='json')
        assert response.status_code == 400

    def test_non_admins_are_refused(self):
        from patient_portal.models import Identity
        client = APIClient()
        client.force_authenticate(Identity.objects.create_user(email='patient@example.test'))
        assert client.get(self.url).status_code == 403
        assert client.get('/api/v1/loinc/axis-values/?axis=system').status_code == 403

    def test_axis_values_autocomplete(self):
        response = self.client.get('/api/v1/loinc/axis-values/?axis=system&q=bld')
        assert response.status_code == 200
        assert {row['value'] for row in response.data['values']} == {'Bld', 'BldC'}
        assert self.client.get('/api/v1/loinc/axis-values/?axis=colour').status_code == 400

    def test_queue_rows_carry_the_variant_count(self):
        axis_variants.refresh(self.mapping)
        response = self.client.get(f'/api/v1/code-mappings/{self.mapping.pk}/')
        assert response.status_code == 200
        assert response.data['axis_variant_count'] == 2

    def test_suggest_one_for_a_variant_uses_its_axes(self, pipeline):
        response = self.client.post('/api/v1/code-mappings/suggest-one/', {
            'source_code': 'GLU', 'source_vocabulary_id': LAB, 'omop_table': 'measurement',
            'source_code_description': 'Glucose', 'axis_key': 'property=SCnc|scale_type=Qn',
        }, format='json')
        assert response.status_code == 200, response.data
        assert response.data['suggested']['concept_code'] == '14749-6'
        assert pipeline['context']['loinc_axes']['property'] == 'SCnc'


def test_fhir_sync_routes_an_observation_by_its_unit(glucose):
    from patient_portal.api.fhir.sync import FhirSyncView
    from omop_core.services.source_vocabularies import fhir_source_vocabulary

    system = 'urn:acme:lab'
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=fhir_source_vocabulary(system), source_code='GLU',
        omop_table='measurement', status='approved', target_concept=glucose['2345-7'],
    )
    for unit in ('mg/dL', 'mmol/L'):
        PatientSourceCode.objects.create(
            person=PersonFactory(), source_value='GLU', source_vocabulary_id=mapping.source_vocabulary_id,
            omop_table='measurement', source_unit=unit)
    variant = {v.axis_key: v for v in axis_variants.refresh(mapping)}['property=SCnc|scale_type=Qn']
    axis_variants.set_destination(variant, glucose['14749-6'], approve=True, user=None)

    view, cache = FhirSyncView(), {}
    code = {'coding': [{'system': system, 'code': 'GLU'}]}
    assert view._axis_variant_concept(code, 'mmol/L', cache) == glucose['14749-6']
    assert view._axis_variant_concept(code, 'mg/dL', cache) is None
    assert view._axis_variant_concept(code, None, cache) is None


def test_units_across_records_settle_only_what_they_agree_on():
    axes, _ = loinc_axes.infer(LAB, 'GLU', {}, observed_units=['mg/dL', 'g/L'])
    assert loinc_axes.values(axes) == {'property': 'MCnc', 'scale_type': 'Qn'}
    axes, _ = loinc_axes.infer(LAB, 'GLU', {}, observed_units=['mg/dL', 'mmol/L'])
    assert loinc_axes.values(axes) == {'scale_type': 'Qn'}


def test_the_panel_infers_from_patients_units_when_the_code_has_no_metadata(two_unit_code):
    from patient_portal.models import Identity
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='units@example.test', is_staff=True, is_superuser=True))
    axes = client.get(f'/api/v1/code-mappings/{two_unit_code.pk}/loinc-axes/').data['source_axes']['axes']
    assert axes['scale_type']['value'] == 'Qn' and 'property' not in axes


# --- review fixes (#1950) -------------------------------------------------------

def test_copying_a_mapping_keeps_its_axes_and_variant_count(two_unit_code):
    from omop_core.services.field_curation_transfer import apply_payload, read_payload

    two_unit_code.loinc_axes = {'component': 'Glucose'}
    two_unit_code.save()
    axis_variants.refresh(two_unit_code)
    payload = read_payload('default', tables=('code_mappings',))
    SourceCodeConceptMapping.objects.update(loinc_axes={}, axis_variant_count=0)
    apply_payload(payload, tables=('code_mappings',))
    two_unit_code.refresh_from_db()
    assert (two_unit_code.loinc_axes, two_unit_code.axis_variant_count) == ({'component': 'Glucose'}, 2)


def test_a_payload_written_before_the_axis_fields_still_applies(two_unit_code):
    from omop_core.services.field_curation_transfer import apply_payload, read_payload

    payload = read_payload('default', tables=('code_mappings',))
    for row in payload['code_mappings']:
        row.pop('loinc_axes'), row.pop('axis_variant_count')
    apply_payload(payload, tables=('code_mappings',))
    two_unit_code.refresh_from_db()
    assert two_unit_code.loinc_axes == {}


def test_an_approved_variant_keeps_routing_when_its_units_leave_the_evidence(two_unit_code, glucose):
    variant = {v.axis_key: v for v in axis_variants.refresh(two_unit_code)}['property=SCnc|scale_type=Qn']
    axis_variants.set_destination(variant, glucose['14749-6'], approve=True, user=None)
    PatientSourceCode.objects.filter(source_unit='mmol/L').delete()

    axis_variants.refresh(two_unit_code)
    two_unit_code.refresh_from_db()
    assert two_unit_code.axis_variant_count == 2
    assert resolve_source_code(source_vocabulary_id=LAB, source_code='GLU', omop_table='measurement',
                               source_unit='mmol/L')[0] == glucose['14749-6']


def test_a_retired_or_non_standard_variant_concept_is_not_routed(two_unit_code, glucose):
    variant = {v.axis_key: v for v in axis_variants.refresh(two_unit_code)}['property=SCnc|scale_type=Qn']
    axis_variants.set_destination(variant, glucose['14749-6'], approve=True, user=None)
    glucose['14749-6'].__class__.objects.filter(pk=glucose['14749-6'].pk).update(standard_concept=None)
    two_unit_code.refresh_from_db()
    assert axis_variants.variant_for(two_unit_code, 'mmol/L') is None


def test_reopening_an_approved_variant_moves_its_rows_back(two_unit_code, glucose):
    row = MeasurementFactory(measurement_concept=glucose['2345-7'], measurement_source_value='GLU',
                             unit_source_value='mmol/L', value_as_number=5.4)
    variant = {v.axis_key: v for v in axis_variants.refresh(two_unit_code)}['property=SCnc|scale_type=Qn']
    axis_variants.set_destination(variant, glucose['14749-6'], approve=True, user=None)
    row.refresh_from_db()
    assert row.measurement_concept_id == glucose['14749-6'].pk

    axis_variants.set_destination(variant, glucose['14749-6'], approve=False, user=None, reopen=True)
    row.refresh_from_db()
    assert row.measurement_concept_id == glucose['2345-7'].pk


def test_an_approved_variant_is_not_reopened_by_picking_a_destination(two_unit_code, glucose):
    variant = {v.axis_key: v for v in axis_variants.refresh(two_unit_code)}['property=SCnc|scale_type=Qn']
    axis_variants.set_destination(variant, glucose['14749-6'], approve=True, user=None)
    with pytest.raises(ValueError, match='reopen'):
        axis_variants.set_destination(variant, glucose['2345-7'], approve=False, user=None)
    variant.refresh_from_db()
    assert (variant.status, variant.target_concept_id) == ('approved', glucose['14749-6'].pk)


def test_without_a_code_destination_rows_move_off_and_back_to_concept_0(two_unit_code, glucose):
    from tests.factories import ConceptFactory as CF
    zero = CF(concept_id=0, concept_name='No matching concept', concept_code='No matching concept')
    SourceCodeConceptMapping.objects.filter(pk=two_unit_code.pk).update(target_concept=None)
    two_unit_code.refresh_from_db()
    row = MeasurementFactory(measurement_concept=zero, measurement_source_value='GLU',
                             unit_source_value='mmol/L', value_as_number=5.4)
    variant = {v.axis_key: v for v in axis_variants.refresh(two_unit_code)}['property=SCnc|scale_type=Qn']

    result = axis_variants.set_destination(variant, glucose['14749-6'], approve=True, user=None)
    row.refresh_from_db()
    assert result['rows_updated'] == 1 and row.measurement_concept_id == glucose['14749-6'].pk

    axis_variants.set_destination(variant, glucose['14749-6'], approve=False, user=None, reopen=True)
    row.refresh_from_db()
    assert row.measurement_concept_id == 0


def test_an_unrecognised_unit_spelling_is_a_note_not_a_variant(glucose):
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='GLU3', omop_table='measurement')
    for unit in ('mg/dL', 'MG/DL', 'mg/100mL'):
        PatientSourceCode.objects.create(person=PersonFactory(), source_value='GLU3',
                                         source_vocabulary_id=LAB, omop_table='measurement', source_unit=unit)
    variants, notes = axis_variants.detect(mapping)
    assert [v['axis_key'] for v in variants] == ['property=MCnc|scale_type=Qn']
    assert notes and 'mg/100mL' in notes[0]
    axes, _ = loinc_axes.infer(LAB, 'GLU3', {}, observed_units=['mg/dL', 'mg/100mL'])
    assert axes['property']['value'] == 'MCnc'


@pytest.mark.parametrize('unit', ['U/mL', 'U/L', 'IU/L', '%'])
def test_a_unit_that_fits_several_properties_settles_none(unit):
    axes, _ = loinc_axes.infer(LAB, 'CA125', {'fhir': {'valueQuantity': {'value': 35, 'unit': unit}}})
    assert 'property' not in axes and axes['scale_type']['value'] == 'Qn'


def test_unit_evidence_is_read_once_for_a_batch(two_unit_code, monkeypatch):
    from omop_core.services import mapping_unit_consistency as muc
    calls = []
    real = muc._unit_counts
    monkeypatch.setattr(muc, '_unit_counts', lambda mappings: calls.append(len(mappings)) or real(mappings))
    other = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='K', omop_table='measurement')
    counts = axis_variants.unit_counts_for([two_unit_code, other])
    assert calls == [2]
    assert set(counts[two_unit_code.pk]) == {'mg/dL', 'mmol/L'}


def test_metadata_hints_do_not_reach_the_ranker(pipeline):
    suggest_one_mapping('GLU', LAB, 'measurement', source_description='Glucose', row_metadata={'fhir': {
        'specimen': {'display': 'Specimen 23-SP-0042 for Jane Doe'}, 'method': {'text': 'Jane'}}})
    # The hints quote the stored metadata; what the ranker gets of it is the
    # de-identified source_metadata, which has its own rules.
    assert 'loinc_axis_hints' not in pipeline['context']
    assert 'specimen = ' not in str(pipeline['context'])


def test_the_sample_unit_does_not_settle_property_when_record_units_disagree():
    axes, hints = loinc_axes.infer(
        LAB, 'A1C', {'fhir': {'valueQuantity': {'value': 5.4, 'unit': 'mg/dL'}}},
        observed_units=['mg/dL', '%'])
    assert loinc_axes.values(axes) == {'scale_type': 'Qn'}
    assert 'different properties' in hints['property']


@pytest.mark.parametrize('unit, prop', [
    ('mM', 'SCnc'), ('mm', 'Len'), ('mg/dl', 'MCnc'), ('mmol/l', 'SCnc'), ('S', ''),
])
def test_units_are_case_sensitive_with_common_lab_spellings_listed(unit, prop):
    assert loinc_axes.unit_property(unit) == prop


def test_a_blood_specimen_is_a_hint_because_serum_is_drawn_from_blood():
    axes, hints = loinc_axes.infer(LAB, 'GLU', {'fhir': {'specimen': {'display': 'Blood'}}})
    assert 'system' not in axes and hints['system'] == 'specimen = Blood'


class TestReviewFixesApi:
    @pytest.fixture(autouse=True)
    def _client(self, two_unit_code):
        from patient_portal.models import Identity
        self.client = APIClient()
        self.client.force_authenticate(Identity.objects.create_user(
            email='review@example.test', is_staff=True, is_superuser=True))
        self.mapping = two_unit_code

    def test_a_non_standard_destination_is_refused(self, glucose):
        source_only = ConceptFactory(vocabulary=glucose['2345-7'].vocabulary,
                                     domain=glucose['2345-7'].domain, standard_concept=None)
        response = self.client.post(f'/api/v1/code-mappings/{self.mapping.pk}/axis-variants/', {
            'axis_key': 'property=SCnc|scale_type=Qn', 'target_concept_id': source_only.pk,
        }, format='json')
        assert response.status_code == 400
        assert 'standard' in response.data['target_concept_id']

    def test_a_routed_lookup_answer_keeps_the_old_key_too(self, glucose):
        variant = {v.axis_key: v for v in axis_variants.refresh(self.mapping)}['property=SCnc|scale_type=Qn']
        axis_variants.set_destination(variant, glucose['14749-6'], approve=True, user=None)
        response = self.client.post('/api/v1/code-mappings/lookup/', {'codes': [
            {'source_vocabulary_id': LAB, 'source_code': 'GLU', 'omop_table': 'measurement',
             'source_unit': 'mmol/L'},
        ]}, format='json')
        results = response.data['mappings']
        assert results[f'{LAB}|GLU']['target_concept_code'] == '14749-6'
        assert results[f'{LAB}|GLU|mmol/L']['target_concept_code'] == '14749-6'



def test_suggest_one_never_borrows_another_hospitals_axes(pipeline, glucose):
    from patient_portal.models import Identity
    from tests.factories import OrganizationFactory
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='orgs@example.test', is_staff=True, is_superuser=True))
    a = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='GLU', omop_table='measurement',
        organization=OrganizationFactory(slug='org-a'))
    SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='GLU', omop_table='measurement',
        organization=OrganizationFactory(slug='org-b'), loinc_axes={'property': 'SCnc'})
    body = {'source_code': 'GLU', 'source_vocabulary_id': LAB, 'omop_table': 'measurement',
            'source_code_description': 'Glucose'}

    assert client.post('/api/v1/code-mappings/suggest-one/', body, format='json').status_code == 200
    assert 'property' not in pipeline['context'].get('loinc_axes', {})
    assert client.post('/api/v1/code-mappings/suggest-one/', {**body, 'mapping_id': a.pk},
                       format='json').status_code == 200
    assert 'property' not in pipeline['context'].get('loinc_axes', {})


# --- LOINC preferred for labs ---------------------------------------------------

@pytest.mark.parametrize('vocab, metadata, reason', [
    ('LOINC', {}, 'The source code is LOINC'),
    (LAB, {'fhir': {'code': {'coding': [{'system': 'http://loinc.org', 'code': '2345-7'}]}}}, 'LOINC coding'),
    (LAB, {'fhir': {'category': [{'coding': [{'code': 'laboratory'}]}]}}, 'laboratory result'),
    (LAB, {'category': {'top': 'laboratory', 'mix': ''}}, 'laboratory result'),
    (LAB, {'fhir': {'valueQuantity': {'value': 5, 'unit': 'mmol/L'}}}, 'property is settled'),
])
def test_a_lab_signal_names_its_evidence(vocab, metadata, reason):
    axes, _ = loinc_axes.infer(vocab, 'X', metadata)
    assert reason in loinc_axes.lab_signal(vocab, metadata, axes)


def test_free_text_with_no_lab_evidence_is_not_a_lab_signal():
    assert loinc_axes.lab_signal(LAB, {'fhir': {'code': {'text': 'Glucose'}}}, {}) is None


def test_a_lab_puts_loinc_ahead_of_snomed_and_tells_the_ranker(pipeline, glucose, monkeypatch):
    snomed_measurement = ConceptFactory(
        concept_name='Glucose measurement, serum', concept_code='22569008',
        vocabulary=VocabularyFactory(vocabulary_id='SNOMED'), domain=glucose['2345-7'].domain)
    snomed_hit = candidate(snomed_measurement)
    monkeypatch.setattr('omop_core.mapping.suggestions.lexical_candidates',
                        lambda *a, **k: [snomed_hit, candidate(glucose['2345-7'])])
    suggest_one_mapping('GLU', LAB, 'measurement', source_description='Glucose', row_metadata={
        'fhir': {'category': [{'coding': [{'code': 'laboratory'}]}]}})

    assert [c['vocabulary_id'] for c in pipeline['candidates']] == ['LOINC', 'SNOMED']
    assert pipeline['context']['expected_vocabulary']['vocabulary_id'] == 'LOINC'


def test_without_a_lab_signal_the_order_is_untouched(pipeline, glucose, monkeypatch):
    snomed_measurement = ConceptFactory(
        concept_name='Heart rate', concept_code='364075005',
        vocabulary=VocabularyFactory(vocabulary_id='SNOMED'), domain=glucose['2345-7'].domain)
    monkeypatch.setattr('omop_core.mapping.suggestions.lexical_candidates',
                        lambda *a, **k: [candidate(snomed_measurement), candidate(glucose['2345-7'])])
    suggest_one_mapping('HR', LAB, 'measurement', source_description='Heart rate')

    assert [c['vocabulary_id'] for c in pipeline['candidates']] == ['SNOMED', 'LOINC']
    assert 'expected_vocabulary' not in pipeline['context']


def test_jev_is_told_loinc_is_expected(settings):
    from omop_core.mapping.suggestions import rank_candidates_jev

    settings.JEV_API_KEY = 'key'
    with patch('requests.post') as post:
        post.return_value.json.return_value = {'answers': {'best_match': {'choice': 'none'}}}
        rank_candidates_jev('GLU', [{'concept_id': 1, 'concept_code': '2345-7', 'concept_name': 'Glucose',
                                     'vocabulary_id': 'LOINC'}],
                            source_context={'code': 'GLU', 'expected_vocabulary': {
                                'vocabulary_id': 'LOINC', 'reason': 'filed as a laboratory result'}})
    assert 'laboratory-style test (filed as a laboratory result): prefer a LOINC' in post.call_args.kwargs['json']['state']
