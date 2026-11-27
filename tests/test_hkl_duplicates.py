"""Retiring the HK-Labs concepts that duplicate a standard serum/plasma LOINC test.

Covers the migration (0380) and the read-only audit command, which share one
planner, and the guards that stop a duplicate being minted again: the curator's
Mint dialog, the quarantine minter the code-mapping lookup uses, the lab sync
and copy_reference_data.
"""
import importlib
from io import StringIO
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import connection
from rest_framework.test import APIClient

from omop_core.mapping.therapy import _get_or_create_quarantine_concept
from omop_core.models import (
    Concept, ConceptAncestor, ConceptMappingNote, ConceptMappingPriority, ConceptSynonym,
    FieldConceptMapping, Measurement, PatientRecord, SourceCodeConceptMapping, Vocabulary,
)
from omop_core.services import hkl_duplicates as hkl
from omop_core.services.instance_copy import local_concept_ids
from patient_portal.models import Identity
from tests.factories import (
    ConceptClassFactory, ConceptFactory, DomainFactory, MeasurementFactory, PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db

migration = importlib.import_module('omop_core.migrations.0380_retire_hkl_duplicates')


@pytest.fixture()
def vocab():
    VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC')
    VocabularyFactory(vocabulary_id='HK-Labs', vocabulary_name='HealthKey Labs')
    domain = DomainFactory(domain_id='Measurement', domain_name='Measurement')
    lab = ConceptClassFactory(concept_class_id='Lab Test')
    return domain, lab


def loinc(vocab, code='2857-1', name='Prostate specific Ag [Mass/volume] in Serum or Plasma', **kw):
    domain, lab = vocab
    return ConceptFactory(
        concept_name=name, concept_code=code, vocabulary_id='LOINC', domain=domain,
        concept_class=lab, standard_concept=kw.pop('standard_concept', 'S'), **kw)


def hk(vocab, code='hkl:psa', name='PSA'):
    domain, lab = vocab
    return ConceptFactory(
        concept_name=name, concept_code=code, vocabulary_id='HK-Labs', domain=domain,
        concept_class=lab, standard_concept=None, source='HealthKey')


def run_migration():
    migration.retire_hkl_duplicates(apps, SimpleNamespace(connection=connection))


def measurement(person, source_concept, **kw):
    return MeasurementFactory(
        person=person, measurement_concept_id=0, measurement_source_concept=source_concept, **kw)


@pytest.fixture()
def zero():
    # concept 0, OMOP's "no matching concept", which unmapped measurements carry.
    ConceptFactory(concept_id=0, concept_name='No matching concept', concept_code='0',
                   standard_concept=None, vocabulary_id='LOINC')


# --- the table ------------------------------------------------------------------

def test_the_table_is_the_eighteen_approved_codes():
    assert len(hkl.RETIRED_HKL_DUPLICATES) == 18
    assert hkl.RETIRED_HKL_DUPLICATES['hkl:psa'] == '2857-1'
    assert hkl.RETIRED_HKL_DUPLICATES['hkl:dheas'] == hkl.RETIRED_HKL_DUPLICATES['hkl:dhea-s'] == '2191-5'
    # The urine panels, the LOINC-coded ones and the local ones are not in it.
    assert not any(c in hkl.RETIRED_HKL_DUPLICATES for c in
                   ('hkl:24hr-free-cortisol', 'hkl:85319-5', 'hkl:lp95826-0', 'hkl:indican'))


def test_every_foreign_key_to_concept_is_found_by_discovery():
    found = {(m._meta.label, f.name) for m, f in hkl.concept_fk_columns(apps)}
    assert ('omop_core.Measurement', 'measurement_source_concept') in found
    assert ('omop_core.FieldConceptMapping', 'concept') in found
    assert ('omop_core.SourceCodeConceptMapping', 'target_concept') in found
    assert len(found) > 100
    # The classes of reference the module names are real columns on real models.
    for label, column in (*hkl.DEPENDENT_COLUMNS, *hkl.MOVE_OR_DROP_COLUMNS):
        assert (label, column) in found, (label, column)
    for (label, fk), (vocab_col, code_col) in hkl.TWIN_COLUMNS.items():
        model = apps.get_model(label)
        names = {f.name for f in model._meta.concrete_fields}
        assert fk in names and vocab_col in names and (not code_col or code_col in names)
    for label, column in (*hkl.ID_COLUMNS, *hkl.INFORMATIONAL_ID_COLUMNS, *hkl.JSON_ID_LISTS):
        assert column in {f.name for f in apps.get_model(label)._meta.concrete_fields}, (label, column)
    for label, vocab_col, code_col in (*hkl.CODE_COLUMNS, *hkl.INFORMATIONAL_CODE_COLUMNS):
        names = {f.name for f in apps.get_model(label)._meta.concrete_fields}
        assert {vocab_col, code_col} <= names, label


# --- the migration ---------------------------------------------------------------

def test_a_fresh_database_with_no_hkl_concepts_is_untouched(vocab):
    loinc(vocab)
    before = Concept.objects.count()
    run_migration()
    run_migration()
    assert Concept.objects.count() == before


def test_a_concept_with_no_references_is_deleted(vocab):
    loinc(vocab)
    hk(vocab)
    run_migration()
    assert not Concept.objects.filter(vocabulary_id='HK-Labs', concept_code='hkl:psa').exists()
    assert Concept.objects.filter(vocabulary_id='LOINC', concept_code='2857-1').exists()


def test_references_move_to_the_standard_concept_and_the_concept_is_deleted(vocab, zero):
    target = loinc(vocab)
    old = hk(vocab)
    person = PersonFactory()
    PatientRecord.objects.create(person=person, derivation_version=5)
    m = measurement(person, old, measurement_source_value='PSA', unit_source_value='ng/mL')
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='Lab', source_code='PSA-1', source_code_description='PSA',
        target_concept=old, suggested_target_concept=old, destination_vocabulary_id='HK-Labs',
        status='proposed', omop_table='measurement',
        pending_repoint_concept_ids=[old.pk, 7], deferred_repoint_concept_ids=[7])
    field = FieldConceptMapping.objects.create(
        field_name='psa', concept=old, vocabulary_id='HK-Labs', concept_code='hkl:psa',
        omop_table='measurement')

    run_migration()

    m.refresh_from_db()
    assert m.measurement_source_concept_id == target.pk
    mapping.refresh_from_db()
    assert (mapping.target_concept_id, mapping.suggested_target_concept_id) == (target.pk, target.pk)
    assert mapping.destination_vocabulary_id == 'LOINC'
    assert mapping.pending_repoint_concept_ids == [7]
    field.refresh_from_db()
    assert (field.concept_id, field.vocabulary_id, field.concept_code) == (target.pk, 'LOINC', '2857-1')
    assert not Concept.objects.filter(pk=old.pk).exists()
    # The patient's record is marked for re-derivation.
    assert PatientRecord.objects.get(person=person).derivation_version == 0


def test_dependent_rows_go_with_the_concept_and_a_note_moves(vocab):
    target = loinc(vocab)
    old = hk(vocab)
    other = loinc(vocab, code='9999-9', name='Something else')
    ConceptSynonym.objects.create(concept=old, concept_synonym_name='prostate ag',
                                  language_concept_id=other.pk)
    ConceptAncestor.objects.create(ancestor_concept=old, descendant_concept=old,
                                   min_levels_of_separation=0, max_levels_of_separation=0)
    ConceptMappingPriority.objects.create(concept=other, position=1)
    ConceptMappingPriority.objects.create(concept=old, position=2)
    ConceptMappingNote.objects.create(concept=old, text='check the assay')

    run_migration()

    assert not ConceptSynonym.objects.filter(concept_synonym_name='prostate ag').exists()
    assert not ConceptAncestor.objects.filter(descendant_concept_id=old.pk).exists()
    assert ConceptMappingNote.objects.get(concept=target).text == 'check the assay'
    # The surviving priority stays dense.
    assert list(ConceptMappingPriority.objects.values_list('position', flat=True)) == [1]


def test_a_note_is_dropped_when_the_standard_concept_has_one(vocab):
    target = loinc(vocab)
    old = hk(vocab)
    ConceptMappingNote.objects.create(concept=target, text='keep me')
    ConceptMappingNote.objects.create(concept=old, text='drop me')
    run_migration()
    assert list(ConceptMappingNote.objects.values_list('text', flat=True)) == ['keep me']


def test_a_concept_whose_standard_target_is_missing_is_kept(vocab, zero, caplog):
    old = hk(vocab)                       # no LOINC 2857-1 on this instance
    person = PersonFactory()
    m = measurement(person, old)
    with caplog.at_level('WARNING'):
        run_migration()
    assert Concept.objects.filter(pk=old.pk).exists()
    m.refresh_from_db()
    assert m.measurement_source_concept_id == old.pk
    assert 'kept hkl:psa (skip_target)' in caplog.text


def test_an_inactive_or_non_standard_target_is_kept(vocab):
    loinc(vocab, invalid_reason='D')
    loinc(vocab, code='2986-8', name='Testosterone', standard_concept=None)
    a, b = hk(vocab), hk(vocab, 'hkl:testosterone', 'Testosterone')
    run_migration()
    assert Concept.objects.filter(pk__in=[a.pk, b.pk]).count() == 2
    assert {p.target_state for p in hkl.build_plan(apps)} == {'inactive', 'not_standard'}


def test_non_serum_references_stay_and_the_serum_ones_move_but_the_concept_is_kept(
        vocab, zero, caplog):
    target = loinc(vocab, code='2143-6', name='Cortisol [Mass/volume] in Serum or Plasma')
    old = hk(vocab, 'hkl:cortisol', 'Cortisol')
    serum = measurement(PersonFactory(), old, measurement_source_value='Cortisol',
                        unit_source_value='ug/dL')
    urine = measurement(PersonFactory(), old, measurement_source_value='Cortisol, urine',
                        unit_source_value='ug/24h')
    per_mass = measurement(PersonFactory(), old, measurement_source_value='Cortisol',
                           unit_source_value='ng/mg')
    with caplog.at_level('WARNING'):
        run_migration()
    # The serum row follows the mapping; the other two are left on the local
    # concept, which cannot then be deleted.
    assert Concept.objects.filter(pk=old.pk).exists()
    for row, expected in ((serum, target.pk), (urine, old.pk), (per_mass, old.pk)):
        row.refresh_from_db()
        assert row.measurement_source_concept_id == expected
    assert 'kept hkl:cortisol (partial_non_serum)' in caplog.text
    plan = hkl.build_plan(apps)[0]
    assert plan.non_serum_total == 2 and plan.outcome == 'partial_non_serum'
    assert any('urine' in line.lower() for line in plan.non_serum)
    # Idempotent: a second run changes nothing more.
    run_migration()
    serum.refresh_from_db(), urine.refresh_from_db()
    assert (serum.measurement_source_concept_id, urine.measurement_source_concept_id) == (target.pk, old.pk)


def test_a_mapping_whose_text_says_urine_is_left_on_the_local_concept(vocab):
    loinc(vocab)
    old = hk(vocab)
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='Lab', source_code='PSA-U', source_code_description='PSA, 24 hour urine',
        target_concept=old, destination_vocabulary_id='HK-Labs', status='proposed',
        omop_table='measurement')
    run_migration()
    mapping.refresh_from_db()
    assert mapping.target_concept_id == old.pk and Concept.objects.filter(pk=old.pk).exists()


@pytest.mark.parametrize('text', [
    'Cortisol, free, 24 hr urine', 'cortisol 24h', 'Cortisol Ur', 'salivary cortisol',
    'ng/mg creat', 'ug/24 h', 'random urine', 'stool', 'ng/mg', 'pg / mg', 'ug/g',
])
def test_non_serum_phrases_are_recognised(text):
    assert hkl.non_serum_reason(text)


@pytest.mark.parametrize('text', [
    'PSA', 'ng/mL', 'Estradiol (E2) pg/mL', 'Serum or Plasma', 'Prostate specific Ag', 'Ser/Plas',
    'ng/dL', 'µg/dL', 'IU/mL', 'mg/dL', 'mIU/L',
])
def test_serum_phrases_are_not_flagged(text):
    assert not hkl.non_serum_reason(text)


def test_a_field_answer_coded_with_the_hkl_code_blocks_the_concept(vocab):
    from omop_core.models import FieldChoice, FieldChoiceCode
    loinc(vocab)
    old = hk(vocab)
    choice = FieldChoice.objects.create(field_name='psa_flag', code='x', display='X')
    FieldChoiceCode.objects.create(choice=choice, code='hkl:psa', vocabulary_id='HK-Labs')
    run_migration()
    assert Concept.objects.filter(pk=old.pk).exists()
    assert hkl.build_plan(apps)[0].outcome == 'skip_unhandled'


def test_two_codes_share_one_standard_concept_and_a_second_run_does_nothing(vocab):
    target = loinc(vocab, code='2191-5', name='DHEA sulfate [Mass/volume] in Serum or Plasma')
    a, b = hk(vocab, 'hkl:dhea-s', 'DHEA-S'), hk(vocab, 'hkl:dheas', 'DHEAS')
    run_migration()
    assert not Concept.objects.filter(pk__in=[a.pk, b.pk]).exists()
    before = Concept.objects.count()
    run_migration()
    assert Concept.objects.count() == before and Concept.objects.filter(pk=target.pk).exists()


def test_a_concept_that_fails_is_rolled_back_and_the_rest_go_on(vocab, zero, monkeypatch):
    loinc(vocab)
    loinc(vocab, code='2842-3', name='Prolactin')
    bad, good = hk(vocab), hk(vocab, 'hkl:prolactin', 'Prolactin')
    person = PersonFactory()
    m = measurement(person, bad)
    real = hkl._apply_one

    def flaky(registry, using, plan, partial=False):
        if plan.hkl_code == 'hkl:psa':
            Measurement.objects.filter(pk=m.pk).update(measurement_source_concept=plan.target_id)
            raise RuntimeError('boom')
        return real(registry, using, plan, partial=partial)

    monkeypatch.setattr(hkl, '_apply_one', flaky)
    run_migration()
    assert Concept.objects.filter(pk=bad.pk).exists()
    assert not Concept.objects.filter(pk=good.pk).exists()
    m.refresh_from_db()
    assert m.measurement_source_concept_id == bad.pk          # the savepoint undid it


def test_the_reverse_is_a_no_op(vocab):
    migration.reverse_noop(apps, SimpleNamespace(connection=connection))


# --- the audit command -----------------------------------------------------------

def audit():
    out = StringIO()
    try:
        call_command('audit_hkl_duplicates', stdout=out)
        code = 0
    except SystemExit as exc:
        code = exc.code
    return code, out.getvalue()


def test_audit_with_nothing_present_exits_zero(vocab):
    code, text = audit()
    assert code == 0 and 'Nothing to remove' in text


def test_audit_lists_references_and_the_target_and_exits_zero_when_all_will_go(vocab, zero):
    target = loinc(vocab)
    old = hk(vocab)
    measurement(PersonFactory(), old)
    code, text = audit()
    assert code == 0
    assert 'hkl:psa' in text and f'concept {target.pk}' in text
    assert 'omop_core.Measurement.measurement_source_concept=1 [patient; repoint]' in text
    assert 'will be removed' in text
    assert Concept.objects.filter(pk=old.pk).exists()      # read-only


def test_audit_exits_one_for_a_missing_target_and_a_non_serum_row(vocab, zero):
    old = hk(vocab)                                        # LOINC missing
    code, text = audit()
    assert code == 1 and 'standard LOINC 2857-1 is missing' in text

    loinc(vocab)
    measurement(PersonFactory(), old, measurement_source_value='PSA urine')
    code, text = audit()
    assert code == 1 and 'serum rows move' in text


def test_audit_json_and_the_audit_agrees_with_the_migration(vocab, zero):
    import json
    loinc(vocab)
    loinc(vocab, code='2143-6', name='Cortisol')
    hk(vocab)
    hk(vocab, 'hkl:cortisol', 'Cortisol')
    measurement(PersonFactory(), Concept.objects.get(concept_code='hkl:cortisol'),
                measurement_source_value='Cortisol 24 hour urine')
    out = StringIO()
    with pytest.raises(SystemExit):
        call_command('audit_hkl_duplicates', '--json', stdout=out)
    data = json.loads(out.getvalue())
    removable = {c['hkl_code'] for c in data['concepts'] if c['outcome'] == 'delete'}
    present = {c['hkl_code'] for c in data['concepts']}
    run_migration()
    gone = {c for c in present
            if not Concept.objects.filter(vocabulary_id='HK-Labs', concept_code=c).exists()}
    assert present == {'hkl:psa', 'hkl:cortisol'}
    assert removable == {'hkl:psa'} and gone == removable


# --- the guards ------------------------------------------------------------------

def test_the_standard_duplicate_is_found_by_name_or_code(vocab):
    target = loinc(vocab, name='Prostate specific Ag [Mass/volume] in Serum or Plasma')
    assert hkl.find_standard_duplicate(
        name='  prostate SPECIFIC ag [mass/volume]  in serum or plasma') == target
    assert hkl.find_standard_duplicate(name='Something', code='hkl:2857-1') == target
    assert hkl.find_standard_duplicate(name='PSA') is None
    loinc(vocab, code='1-1', name='Inactive test', invalid_reason='D')
    assert hkl.find_standard_duplicate(name='Inactive test') is None


def test_quarantine_minter_reuses_the_standard_concept(vocab):
    target = loinc(vocab)
    kwargs = dict(vocabulary_id='HK-Labs', domain_id='Measurement', concept_class_id='Lab Test')
    # A retired code, the same name, and a LOINC code behind an hkl: prefix.
    assert _get_or_create_quarantine_concept(
        concept_code='hkl:psa', concept_name='PSA', **kwargs) == target
    assert _get_or_create_quarantine_concept(
        concept_code='hkl:x', concept_name=target.concept_name, **kwargs) == target
    assert _get_or_create_quarantine_concept(
        concept_code='hkl:2857-1', concept_name='whatever', **kwargs) == target
    assert not Concept.objects.filter(vocabulary_id='HK-Labs').exists()


def test_quarantine_minter_still_mints_a_genuinely_local_lab(vocab):
    loinc(vocab)
    concept = _get_or_create_quarantine_concept(
        vocabulary_id='HK-Labs', domain_id='Measurement', concept_class_id='Lab Test',
        concept_code='hkl:indican', concept_name='Indican')
    assert concept.vocabulary_id == 'HK-Labs' and concept.concept_code == 'hkl:indican'


def test_a_retired_code_is_minted_when_the_standard_concept_is_not_loaded(vocab):
    concept = _get_or_create_quarantine_concept(
        vocabulary_id='HK-Labs', domain_id='Measurement', concept_class_id='Lab Test',
        concept_code='hkl:psa', concept_name='PSA')
    assert concept.vocabulary_id == 'HK-Labs'


def mint_request(client, vocab_id='HK-Labs', **changes):
    body = {'vocabulary_id': vocab_id, 'concept_name': 'PSA', 'concept_code': 'hkl:psa',
            'domain_id': 'Measurement', 'source_code': 'PSA', 'source_vocabulary_id': 'Lab'}
    body.update(changes)
    review = client.post('/api/v1/code-mappings/mint-destination/', {**body, 'action': 'review'}, format='json')
    assert review.status_code == 200, review.content
    return client.post('/api/v1/code-mappings/mint-destination/', {
        **body, 'action': 'mint', 'none_match': True, 'review_token': review.data['review_token'],
    }, format='json')


@pytest.fixture()
def curator():
    api = APIClient()
    api.force_authenticate(Identity.objects.create_user(email='curator@example.test', is_staff=True))
    return api


def test_mint_dialog_refuses_a_duplicate_and_names_the_standard_concept(vocab, curator, monkeypatch):
    monkeypatch.setattr('patient_portal.api.concept_mint.umls_candidates', lambda *a, **k: ([], 0))
    monkeypatch.setattr('patient_portal.api.concept_mint.lexical_candidates', lambda *a, **k: [])
    loinc(vocab)
    response = mint_request(curator)
    assert response.status_code == 409
    assert response.data['code'] == 'standard_concept_exists'
    assert '2857-1' in response.data['detail']
    assert not Concept.objects.filter(vocabulary_id='HK-Labs').exists()


def test_mint_dialog_still_mints_a_new_local_lab(vocab, curator, monkeypatch):
    monkeypatch.setattr('patient_portal.api.concept_mint.umls_candidates', lambda *a, **k: ([], 0))
    monkeypatch.setattr('patient_portal.api.concept_mint.lexical_candidates', lambda *a, **k: [])
    loinc(vocab)
    response = mint_request(curator, concept_name='Indican', concept_code='hkl:indican')
    assert response.status_code == 201
    assert Concept.objects.filter(vocabulary_id='HK-Labs', concept_code='hkl:indican').exists()


def test_lab_sync_reuses_the_standard_concept_for_a_retired_name(vocab):
    from patient_portal.api.lab_results.sync import SyncView
    target = loinc(vocab)
    cache = SyncView()._preload_hk_concepts(
        [{'test_name': 'PSA'}, {'test_name': 'Indican'}], loinc_cache={})
    assert cache['PSA'] == target.pk
    assert Concept.objects.get(pk=cache['Indican']).concept_code == 'hkl:indican'
    assert not Concept.objects.filter(concept_code='hkl:psa').exists()


# --- copy_reference_data ---------------------------------------------------------

def test_a_retired_ref_resolves_to_the_standard_concept_and_a_kept_one_to_itself(vocab):
    target = loinc(vocab)
    ids = local_concept_ids([('HK-Labs', 'hkl:psa'), ('HK-Labs', 'hkl:indican')])
    assert ids == {('HK-Labs', 'hkl:psa'): target.pk}
    old = hk(vocab)
    assert local_concept_ids([('HK-Labs', 'hkl:psa')]) == {('HK-Labs', 'hkl:psa'): old.pk}


def test_copy_reference_data_does_not_recreate_a_retired_concept(vocab):
    from omop_core.services.field_curation_transfer import TransferStats
    from omop_core.services.reference_transfer import _apply_local_concepts
    loinc(vocab)
    row = {k: getattr(hk(vocab), k) for k in (
        'concept_id', 'concept_name', 'domain_id', 'vocabulary_id', 'concept_class_id',
        'standard_concept', 'concept_code', 'valid_start_date', 'valid_end_date',
        'invalid_reason', 'source')}
    Concept.objects.filter(vocabulary_id='HK-Labs').delete()
    local = {'concept_id': 1, **row, 'concept_code': 'hkl:indican', 'concept_name': 'Indican'}
    stats = TransferStats()
    _apply_local_concepts({'vocabularies': [], 'domains': [], 'classes': [],
                           'concepts': [row, local]}, stats)
    codes = set(Concept.objects.filter(vocabulary_id='HK-Labs').values_list('concept_code', flat=True))
    assert codes == {'hkl:indican'}
    assert any('duplicate a standard LOINC test' in w for w in stats.warnings)
    assert Vocabulary.objects.filter(vocabulary_id='HK-Labs').exists()
