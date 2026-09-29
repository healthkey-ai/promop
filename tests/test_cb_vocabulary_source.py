"""CancerBot vocabularies as a Code Mapping source (#1649)."""
import csv
import json

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command

from omop_core.mapping.code_resolution import repoint_clinical_rows
from omop_core.models import ConditionOccurrence, MappingDestinationCandidate, SourceCodeConceptMapping
from omop_core.services.source_vocabularies import is_catalog_source
from omop_core.test_utils import ensure_test_concept_zero
from tests.factories import ConceptFactory, ConditionOccurrenceFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

COLUMNS = ['table', 'code', 'title', 'omop_vocabulary_id', 'omop_concept_code', 'omop_domain',
           'value_vocabulary_id', 'value_concept_code', 'match', 'confidence', 'verified',
           'concept_set', 'note', 'trial_count']


def concept(vocabulary_id, code, *, domain='Condition', **kwargs):
    return ConceptFactory(
        vocabulary=VocabularyFactory(vocabulary_id=vocabulary_id, vocabulary_name=vocabulary_id,
                                     vocabulary_version=f'{vocabulary_id} v1'),
        domain=DomainFactory(domain_id=domain, domain_name=domain),
        concept_code=code, concept_name=f'{vocabulary_id} {code}', **kwargs)


def write_csv(path, rows):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, '') for c in COLUMNS})
    return str(path)


def cb(code):
    return SourceCodeConceptMapping.objects.get(source_vocabulary_id='CB', source_code=code)


@pytest.fixture
def concepts():
    return {
        'bc': concept('SNOMED', '254837009'),
        'hk': concept('HK-Labs', 'hkl:gain-1q21', domain='Measurement', standard_concept=None,
                      concept_id=2_100_007_862),
        'fake': concept('SNOMED', '999999999', concept_id=2_000_000_100),
        'set_a': concept('HemOnc', '35807219', domain='Drug'),
        'set_b': concept('HemOnc', '35807319', domain='Drug'),
    }


@pytest.fixture
def proposals(tmp_path, concepts):
    return write_csv(tmp_path / 'proposals.csv', [
        {'table': 'disease', 'code': 'BC', 'title': 'Breast cancer', 'omop_vocabulary_id': 'SNOMED',
         'omop_concept_code': '254837009', 'match': 'auto_exact', 'verified': 'ok', 'trial_count': '12'},
        {'table': 'cytogenicmarker', 'code': '1q21Amplification', 'title': '1q21 Amplification',
         'omop_vocabulary_id': 'HK-Labs', 'omop_concept_code': 'hkl:gain-1q21', 'match': 'llm_exact',
         'verified': 'ok'},
        {'table': 'binetstage', 'code': 'binet_stage_a', 'title': 'Binet A', 'omop_vocabulary_id': 'SNOMED',
         'omop_concept_code': '999999999', 'match': 'auto_exact', 'verified': 'ok'},
        {'table': 'cytogenicmarker', 'code': 'chromothripsis', 'title': 'Chromothripsis',
         'match': 'no_concept', 'note': 'absent from CTOMOP'},
        {'table': 'concomitantmedication', 'code': 'antivirals_and_antifungals', 'title': 'Antivirals',
         'match': 'concept_set', 'concept_set': 'HemOnc|35807219; HemOnc|35807319'},
        {'table': 'therapy', 'code': 'esa', 'title': 'ESA', 'omop_vocabulary_id': 'NDFRT',
         'omop_concept_code': 'N0000175425', 'match': 'curated', 'verified': 'NOT IN MIRROR'},
    ])


# --- the guard ---------------------------------------------------------------

def _stored_row_with_label(label):
    zero = ensure_test_concept_zero()
    return ConditionOccurrenceFactory(condition_concept=zero, condition_source_value=label)


@pytest.mark.parametrize('vocabulary, moved', [('CB', 0), ('EPIC', 1)])
def test_approving_a_cb_term_does_not_repoint_rows_that_share_its_label(concepts, vocabulary, moved):
    # EPIC is the control: the same row under an ingest vocabulary does move,
    # which is what makes the CB case a test of the guard and not of the setup.
    row = _stored_row_with_label('Breast cancer')
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=vocabulary, source_code='disease:BC',
        source_code_description='Breast cancer', origin='import', omop_table='condition',
        target_concept=concepts['bc'], status='approved')

    result = repoint_clinical_rows(mapping=mapping, old_concept_id=0,
                                   new_concept_id=concepts['bc'].concept_id)

    assert result['rows_updated'] == moved
    row.refresh_from_db()
    assert row.condition_concept_id == (concepts['bc'].concept_id if moved else 0)


def test_cb_is_a_catalog_source_and_ingest_vocabularies_are_not():
    assert is_catalog_source('CB')
    assert not is_catalog_source('EPIC')
    assert not is_catalog_source('')


# --- import -------------------------------------------------------------------

def test_dry_run_writes_nothing(proposals):
    call_command('import_cb_vocabularies', '--file', proposals)
    assert not SourceCodeConceptMapping.objects.filter(source_vocabulary_id='CB').exists()


def test_import_seeds_proposed_rows_with_usable_destinations_only(proposals, concepts):
    call_command('import_cb_vocabularies', '--file', proposals, '--apply')

    bc = cb('disease:BC')
    assert (bc.status, bc.origin, bc.origin_system) == ('proposed', 'import', 'cancerbot')
    assert bc.target_concept_id == concepts['bc'].concept_id
    assert bc.source_code_description == 'Breast cancer (disease)'
    assert bc.omop_table == 'condition'
    assert bc.occurrence_count == 12
    # HK-* destinations are accepted although non-standard.
    assert cb('cytogenicmarker:1q21Amplification').target_concept_id == concepts['hk'].concept_id
    # A local-range id posing as SNOMED is kept as a candidate, not a destination.
    fake = cb('binetstage:binet_stage_a')
    assert fake.target_concept_id is None
    assert 'local-range id labelled SNOMED' in fake.notes
    # A concept this instance does not hold: no destination, candidate kept unresolved.
    esa = cb('therapy:esa')
    assert esa.target_concept_id is None
    assert esa.destination_candidates.get().target_concept_id is None
    assert cb('cytogenicmarker:chromothripsis').target_concept_id is None


def test_concept_set_members_become_candidates(proposals, concepts):
    call_command('import_cb_vocabularies', '--file', proposals, '--apply')

    mapping = cb('concomitantmedication:antivirals_and_antifungals')
    assert mapping.target_concept_id is None
    candidates = {(c.target_concept_id, tuple(c.origins)) for c in mapping.destination_candidates.all()}
    assert candidates == {(concepts['set_a'].concept_id, ('cb-concept-set',)),
                          (concepts['set_b'].concept_id, ('cb-concept-set',))}


def test_reimport_keeps_reviewer_decisions_and_reports_absent_terms(tmp_path, proposals, concepts):
    call_command('import_cb_vocabularies', '--file', proposals, '--apply')
    other = concept('SNOMED', '4112853')
    SourceCodeConceptMapping.objects.filter(source_code='disease:BC').update(target_concept=other)
    SourceCodeConceptMapping.objects.filter(source_code='cytogenicmarker:chromothripsis').update(status='rejected')
    smaller = write_csv(tmp_path / 'smaller.csv', [
        {'table': 'disease', 'code': 'BC', 'title': 'Breast cancer', 'omop_vocabulary_id': 'SNOMED',
         'omop_concept_code': '254837009', 'match': 'auto_exact', 'verified': 'ok', 'trial_count': '15'},
        {'table': 'cytogenicmarker', 'code': 'chromothripsis', 'title': 'Chromothripsis', 'match': 'no_concept'},
    ])
    before = SourceCodeConceptMapping.objects.filter(source_vocabulary_id='CB').count()

    from io import StringIO
    out = StringIO()
    call_command('import_cb_vocabularies', '--file', smaller, '--apply', stdout=out)

    assert SourceCodeConceptMapping.objects.filter(source_vocabulary_id='CB').count() == before
    bc = cb('disease:BC')
    assert bc.target_concept_id == other.concept_id  # the reviewer's choice survives
    assert bc.occurrence_count == 15
    assert cb('cytogenicmarker:chromothripsis').status == 'rejected'
    assert 'absent: therapy:esa' in out.getvalue()
    assert MappingDestinationCandidate.objects.filter(mapping=bc).count() == 1


def test_keys_that_differ_only_in_case_stop_the_import(tmp_path):
    path = write_csv(tmp_path / 'dup.csv', [
        {'table': 'disease', 'code': 'BC', 'title': 'a', 'match': 'no_concept'},
        {'table': 'disease', 'code': 'bc', 'title': 'b', 'match': 'no_concept'},
    ])
    with pytest.raises(CommandError, match='duplicate key'):
        call_command('import_cb_vocabularies', '--file', path, '--apply')
    assert not SourceCodeConceptMapping.objects.filter(source_vocabulary_id='CB').exists()


# --- bulk approval ------------------------------------------------------------

@pytest.fixture
def reviewer():
    return get_user_model().objects.create_user(email='sme@example.com', password='x')


def test_bulk_approval_signs_off_only_unchanged_verified_proposals(proposals, concepts, reviewer):
    call_command('import_cb_vocabularies', '--file', proposals, '--apply')
    call_command('approve_cb_mappings', '--file', proposals, '--reviewer', reviewer.email, '--apply')

    bc = cb('disease:BC')
    assert (bc.status, bc.reviewer_id) == ('approved', reviewer.pk)
    assert bc.reviewed_at is not None
    # Rejected at import (local-range id) and unverified curated rows stay for the SME.
    assert cb('binetstage:binet_stage_a').status == 'proposed'
    assert cb('therapy:esa').status == 'proposed'
    # Categories outside the bulk set are untouched.
    assert cb('cytogenicmarker:1q21Amplification').status == 'proposed'


def test_bulk_approval_skips_a_row_a_reviewer_repointed(proposals, concepts, reviewer):
    call_command('import_cb_vocabularies', '--file', proposals, '--apply')
    SourceCodeConceptMapping.objects.filter(source_code='disease:BC').update(
        target_concept=concept('SNOMED', '4112853'))
    call_command('approve_cb_mappings', '--file', proposals, '--reviewer', reviewer.email, '--apply')
    assert cb('disease:BC').status == 'proposed'


def test_bulk_approval_refuses_categories_outside_the_decision(proposals, reviewer):
    with pytest.raises(CommandError, match='Only'):
        call_command('approve_cb_mappings', '--file', proposals, '--reviewer', reviewer.email,
                     '--match', 'llm_exact', '--apply')


def test_bulk_approval_dry_run_writes_nothing(proposals, concepts, reviewer):
    call_command('import_cb_vocabularies', '--file', proposals, '--apply')
    call_command('approve_cb_mappings', '--file', proposals, '--reviewer', reviewer.email)
    assert not SourceCodeConceptMapping.objects.filter(source_vocabulary_id='CB', status='approved').exists()


# --- export -------------------------------------------------------------------

def test_export_is_a_complete_snapshot_keyed_by_natural_concept_key(tmp_path, proposals, concepts, reviewer):
    call_command('import_cb_vocabularies', '--file', proposals, '--apply')
    call_command('approve_cb_mappings', '--file', proposals, '--reviewer', reviewer.email, '--apply')
    SourceCodeConceptMapping.objects.create(source_vocabulary_id='EPIC', source_code='X', status='approved')
    out = tmp_path / 'export.json'

    call_command('export_cb_mappings', '--out', str(out))

    payload = json.loads(out.read_text())
    by_code = {m['source_code']: m for m in payload['mappings']}
    assert set(by_code) == {r.source_code for r in SourceCodeConceptMapping.objects.filter(source_vocabulary_id='CB')}
    assert payload['counts'] == {'approved': 1, 'proposed': 5}
    bc = by_code['disease:BC']
    assert (bc['table'], bc['code'], bc['status'], bc['reviewer']) == ('disease', 'BC', 'approved', 'sme@example.com')
    assert (bc['target']['vocabulary_id'], bc['target']['concept_code']) == ('SNOMED', '254837009')
    assert by_code['cytogenicmarker:chromothripsis']['target'] is None
    assert {'vocabulary_id': 'HK-Labs', 'vocabulary_version': 'HK-Labs v1'} in payload['vocabularies']
