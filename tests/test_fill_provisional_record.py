"""``manage.py fill_provisional_record``: one patient's full provisional view (#2696).

Proposes for the person's codes through the normal Suggest pipeline, makes their
rows provisional at proposals at or above the site floor, and rederives their
record. The ranker is replaced here; nothing calls Jev or Anthropic.
"""
import logging
import re
from datetime import date
from io import StringIO

import pytest
import requests
from django.core.management import call_command
from django.core.management.base import CommandError

from omop_core.mapping import suggestions
from omop_core.mapping.suggestions import suggestable_queryset
from omop_core.mapping.code_resolution import NO_MATCHING_CONCEPT_ID
from omop_core.models import (
    Measurement, PatientRecord, PatientRecordConfidence, PatientRecordProvisional,
    PatientSourceCode, SourceCodeConceptMapping,
)
from omop_core.services import fill_provisional
from tests.factories import (
    ConceptFactory, DomainFactory, MeasurementFactory, PatientRecordFactory, PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db

PREFLIGHT_CODE = 'HGB'     # the question the command asks the ranker before it starts


@pytest.fixture()
def base(settings, monkeypatch):
    settings.PROVISIONAL_DATA_ENABLED = True
    settings.PROVISIONAL_CONFIDENCE_FLOOR = 70
    settings.CELERY_BROKER_URL = ''
    ConceptFactory(concept_id=NO_MATCHING_CONCEPT_ID, concept_code='0',
                   concept_name='No matching concept', standard_concept=None)
    ConceptFactory(concept_id=32817, concept_code='32817', concept_name='EHR')
    DomainFactory(domain_id='Measurement', domain_name='Measurement')
    loinc = VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC')
    hemoglobin = ConceptFactory(
        concept_id=3000963, concept_code='718-7', concept_name='Hemoglobin [Mass/volume] in Blood',
        vocabulary=loinc, standard_concept='S')
    candidate = {
        'concept_id': hemoglobin.concept_id, 'concept_name': hemoglobin.concept_name,
        'concept_code': hemoglobin.concept_code, 'vocabulary_id': 'LOINC',
        'concept_class_id': hemoglobin.concept_class_id, 'domain_id': 'Measurement',
        'standard_concept': 'S', 'umls_score': 1.0, 'retrieval': 'umls',
    }
    world = type('World', (), {})()
    world.concept, world.candidate, world.calls = hemoglobin, candidate, []
    world.scores = {}          # source code -> confidence; None declines; 'boom'/'fail'/'fallback'
    world.default = 0.9
    monkeypatch.setattr(suggestions, 'umls_candidates', lambda *a, **k: ([candidate], 'C1'))
    return world


@pytest.fixture()
def world(base, monkeypatch):
    """The ranker is replaced wholesale: the dispatcher answers from ``world.scores``."""
    candidate, hemoglobin = base.candidate, base.concept

    def rank(source_value, candidates, *args, **kwargs):
        if source_value != PREFLIGHT_CODE:
            base.calls.append(source_value)
        outcome = base.scores.get(source_value, base.default)
        if outcome == 'boom':
            raise RuntimeError('ranker exploded')
        if outcome == 'fallback':
            return (candidate, 'Best-match fallback (score 1.0). Ranking model unavailable, so this '
                    'is the first candidate in retrieval fallback order.', None, {})
        if outcome is None:
            return None, 'No suitable concept among the candidates.', [], {}
        return candidate, 'ranked', [{'concept_id': hemoglobin.concept_id, 'confidence': outcome}], {}

    monkeypatch.setattr(suggestions, 'rank_candidates_dispatch', rank)
    return base


class _Response:
    def __init__(self, payload=None, fail=False):
        self.payload, self.fail = payload, fail

    def raise_for_status(self):
        if self.fail:
            raise requests.HTTPError('429 Too Many Requests')

    def json(self):
        return self.payload


@pytest.fixture()
def jev(base, settings, monkeypatch):
    """Only Jev's HTTP layer is replaced: the real ranker, fallback plumbing and dispatcher run."""
    settings.JEV_API_KEY = 'abcdefgh-not-a-real-key'

    def post(url, json=None, headers=None, timeout=None):
        code = re.match(r'Source code: (.+?) — ', json['state']).group(1)
        if code != PREFLIGHT_CODE:
            base.calls.append(code)
        outcome = base.scores.get(code, base.default)
        if outcome == 'fail':
            return _Response(fail=True)
        choice = next(k for k in json['questions']['best_match']['criteria'] if k != 'none')
        if outcome is None:
            return _Response({'answers': {'best_match': {'choice': 'none', 'probabilities': {choice: 0.1}}}})
        return _Response({'answers': {'best_match': {'choice': choice, 'probabilities': {choice: outcome}}}})

    monkeypatch.setattr(requests, 'post', post)
    return base


def _person(code='GLU-1', with_row=True, value=11.5, vocabulary='Local', metadata=None, rows=1):
    """A person holding the code unresolved, the way an ETL load leaves it."""
    person = PersonFactory()
    PatientRecordFactory(person=person)
    PatientSourceCode.objects.create(
        person=person, source_value=code, omop_table='measurement', source_vocabulary_id=vocabulary,
        **({'source_metadata': metadata} if metadata else {}))
    if with_row:
        for day in range(rows):
            MeasurementFactory(
                person=person, measurement_concept_id=NO_MATCHING_CONCEPT_ID,
                measurement_source_value=code, measurement_date=date(2026, 1, 1 + day),
                confidence=None, value_as_number=value, value_source_value=str(value),
                unit_source_value='g/dL')
    return person


def _queue_row(code='GLU-1', **kwargs):
    defaults = dict(
        source_vocabulary_id='Local', source_code=code, source_code_description=code,
        domain_id='Measurement', omop_table='measurement', status='proposed', origin='import',
        origin_system='', occurrence_count=3)
    defaults.update(kwargs)
    return SourceCodeConceptMapping.objects.create(**defaults)


def _fill(person, *args):
    out = StringIO()
    call_command('fill_provisional_record', person.person_id, '--strategies', 'umls', *args, stdout=out)
    return out.getvalue()


def _row(person, code='GLU-1'):
    return Measurement.objects.get(person=person, measurement_source_value=code)


# --- the switch ---------------------------------------------------------------------------

def test_with_the_switch_off_it_refuses_and_writes_nothing(world, settings):
    settings.PROVISIONAL_DATA_ENABLED = False
    person = _person()
    row = _queue_row()
    _person(code='NEW-1')

    with pytest.raises(CommandError, match='PROVISIONAL_DATA_ENABLED is off'):
        _fill(person)

    row.refresh_from_db()
    assert row.target_concept_id is None and row.last_suggest_attempt == ''
    assert SourceCodeConceptMapping.objects.count() == 1
    assert world.calls == []
    assert _row(person).measurement_concept_id == NO_MATCHING_CONCEPT_ID


def test_an_unknown_person_is_an_error(world):
    with pytest.raises(CommandError, match='No person'):
        call_command('fill_provisional_record', 987654321, stdout=StringIO())


# --- dry run ------------------------------------------------------------------------------

def test_a_dry_run_counts_and_writes_nothing(world):
    person = _person()
    row = _queue_row()
    PatientSourceCode.objects.create(
        person=person, source_value='NEW-1', omop_table='measurement', source_vocabulary_id='Local')

    out = _fill(person, '--dry-run')

    assert 'Dry run: nothing written' in out
    row.refresh_from_db()
    assert row.target_concept_id is None and row.last_suggest_attempt == ''
    assert SourceCodeConceptMapping.objects.count() == 1, 'no queue row is created by a dry run'
    assert world.calls == []
    assert _row(person).measurement_concept_id == NO_MATCHING_CONCEPT_ID


# --- propose ------------------------------------------------------------------------------

def test_a_proposed_row_without_a_destination_gets_one_like_a_suggest_run_writes_it(world):
    person = _person()
    row = _queue_row()

    _fill(person)

    row.refresh_from_db()
    assert row.target_concept_id == world.concept.concept_id
    assert row.suggestion_confidence == pytest.approx(0.9)
    assert row.origin_system == suggestions.SUGGESTION_PROVENANCE
    assert row.suggestion_model_version == suggestions.SUGGESTION_MODEL_VERSION
    assert row.status == 'proposed'
    assert row.last_suggest_attempt == f'{suggestions.SUGGESTION_MODEL_VERSION}-jev'


def test_approved_rows_and_importer_destinations_are_left_alone(world):
    person = _person()
    approved_target = ConceptFactory(concept_name='Approved target')
    imported_target = ConceptFactory(concept_name='Imported target')
    for code in ('APP-1', 'IMP-1'):
        PatientSourceCode.objects.create(
            person=person, source_value=code, omop_table='measurement', source_vocabulary_id='Local')
        MeasurementFactory(
            person=person, measurement_concept_id=NO_MATCHING_CONCEPT_ID,
            measurement_source_value=code, measurement_date=date(2026, 1, 1), confidence=None)
    approved = _queue_row('APP-1', status='approved', target_concept=approved_target)
    imported = _queue_row('IMP-1', target_concept=imported_target, origin_system='HT-One',
                          suggestion_confidence=None)
    _queue_row('GLU-1')

    _fill(person, '--replace')

    for row, target, status in ((approved, approved_target, 'approved'),
                                (imported, imported_target, 'proposed')):
        row.refresh_from_db()
        assert (row.target_concept_id, row.status, row.suggestion_confidence is None) == (
            target.concept_id, status, True)
    assert world.calls == ['GLU-1']


def test_a_code_with_no_queue_row_gets_one_and_a_proposal(world):
    person = _person(code='NEW-1')

    _fill(person)

    row = SourceCodeConceptMapping.objects.get(source_code='NEW-1')
    assert (row.source_vocabulary_id, row.organization_id, row.omop_table, row.status) == (
        'Local', None, 'measurement', 'proposed')
    assert row.target_concept_id == world.concept.concept_id


def test_a_rerun_resumes_and_does_not_ask_again_for_what_was_attempted(world):
    person = _person()
    _person(code='DEC-1')
    done, declined = _queue_row('GLU-1'), _queue_row('DEC-1')
    world.scores['DEC-1'] = None
    other = SourceCodeConceptMapping.objects.get(pk=declined.pk)

    _fill(person)
    _fill(person)

    assert world.calls == ['GLU-1']
    done.refresh_from_db()
    assert done.target_concept_id is not None
    # The declined code belongs to another person: this run never reached it.
    other.refresh_from_db()
    assert other.target_concept_id is None and other.last_suggest_attempt == ''


def test_a_declined_code_keeps_no_destination_is_not_retried_and_can_be_forced(world):
    person = _person(code='DEC-1')
    row = _queue_row('DEC-1')
    world.scores['DEC-1'] = None

    _fill(person)
    row.refresh_from_db()
    assert row.target_concept_id is None
    assert row.last_suggest_attempt == f'{suggestions.SUGGESTION_MODEL_VERSION}-jev'
    _fill(person)
    assert world.calls == ['DEC-1']
    _fill(person, '--retry-declined')
    assert world.calls == ['DEC-1', 'DEC-1']


def test_a_ranker_that_cannot_answer_stops_the_run_before_anything_is_proposed(world):
    person = _person()
    row = _queue_row()
    world.scores[PREFLIGHT_CODE] = 'fallback'

    with pytest.raises(CommandError, match='did not answer a test question'):
        _fill(person)

    row.refresh_from_db()
    assert row.target_concept_id is None and world.calls == []


# --- materialise ----------------------------------------------------------------------------

def test_rows_become_provisional_at_or_above_the_floor_and_the_report_counts_the_rest(world):
    person = _person(code='HI-1')
    PatientSourceCode.objects.create(
        person=person, source_value='LO-1', omop_table='measurement', source_vocabulary_id='Local')
    MeasurementFactory(
        person=person, measurement_concept_id=NO_MATCHING_CONCEPT_ID, measurement_source_value='LO-1',
        measurement_date=date(2026, 1, 1), confidence=None)
    _queue_row('HI-1'), _queue_row('LO-1')
    world.scores.update({'HI-1': 0.9, 'LO-1': 0.55})

    summary = fill_provisional.run(person.person_id, strategies=['umls'], log=lambda m: None)

    high = _row(person, 'HI-1')
    assert (high.measurement_concept_id, high.confidence) == (world.concept.concept_id, 90)
    low = _row(person, 'LO-1')
    assert (low.measurement_concept_id, low.confidence) == (NO_MATCHING_CONCEPT_ID, None)
    assert summary['rows_provisional'] == 1
    assert summary['report']['status'] == {'provisional': 1, 'below_floor': 1}
    assert summary['report']['bands'] == {'50-59%': 1}
    # The site floor is untouched: a later sweep would revert anything under it.
    assert summary['report']['floor'] == 70


def test_an_inventory_only_code_gets_a_provisional_row_when_inventory_is_included(world):
    person = _person(code='INV-1', with_row=False)
    _queue_row('INV-1')

    summary = fill_provisional.run(person.person_id, strategies=['umls'], include_inventory=True,
                                   log=lambda m: None)

    row = _row(person, 'INV-1')
    assert (row.measurement_concept_id, row.confidence) == (world.concept.concept_id, 90)
    assert summary['rows_created'] == 1 and summary['rows_provisional'] == 1


def test_skip_propose_only_materialises_the_proposals_that_exist(world):
    person = _person()
    _queue_row('GLU-1', target_concept=world.concept, suggestion_confidence=0.85)

    summary = fill_provisional.run(person.person_id, skip_propose=True, log=lambda m: None)

    assert world.calls == [] and summary['propose'] == {}
    assert _row(person).confidence == 85


# --- propose is for unresolved codes; materialise uses every mapping ------------------------

def _inventory_only(person, code, vocabulary='Local'):
    PatientSourceCode.objects.create(
        person=person, source_value=code, omop_table='measurement', source_vocabulary_id=vocabulary)


def test_only_codes_with_unresolved_rows_are_proposed_for_unless_inventory_is_included(world):
    person = _person(code='UNRES-1')
    _inventory_only(person, 'INV-1')
    unresolved, inventory = _queue_row('UNRES-1'), _queue_row('INV-1')

    summary = fill_provisional.run(person.person_id, strategies=['umls'], log=lambda m: None)

    assert world.calls == ['UNRES-1']
    assert summary['codes_considered'] == 2 and summary['codes_to_propose'] == 1
    inventory.refresh_from_db()
    assert inventory.target_concept_id is None and inventory.last_suggest_attempt == ''
    assert not Measurement.objects.filter(person=person, measurement_source_value='INV-1').exists()

    again = fill_provisional.run(person.person_id, strategies=['umls'], include_inventory=True,
                                 log=lambda m: None)

    assert world.calls == ['UNRES-1', 'INV-1']
    assert again['codes_to_propose'] == 2
    inventory.refresh_from_db()
    assert inventory.target_concept_id == world.concept.concept_id
    assert _row(person, 'INV-1').confidence == 90


def test_the_command_defaults_to_unresolved_only_and_the_two_flags_exclude_each_other(world):
    person = _person(code='UNRES-1')
    _inventory_only(person, 'INV-1')
    _queue_row('UNRES-1'), _queue_row('INV-1')

    out = _fill(person)
    assert 'unresolved rows only' in out and world.calls == ['UNRES-1']
    out = _fill(person, '--include-inventory')
    assert 'unresolved rows and inventory' in out and world.calls == ['UNRES-1', 'INV-1']
    assert 'unresolved rows only' in _fill(person, '--unresolved-only')
    with pytest.raises(CommandError):
        _fill(person, '--unresolved-only', '--include-inventory')


def test_no_queue_row_is_created_for_an_inventory_only_code_unless_it_is_included(world):
    person = _person(code='UNRES-1')
    _inventory_only(person, 'INV-NEW')

    summary = fill_provisional.run(person.person_id, strategies=['umls'], log=lambda m: None)
    assert not SourceCodeConceptMapping.objects.filter(source_code='INV-NEW').exists()
    assert summary['queue_rows']['created'] == 1          # UNRES-1's own row

    fill_provisional.run(person.person_id, strategies=['umls'], include_inventory=True,
                         log=lambda m: None)
    assert SourceCodeConceptMapping.objects.filter(source_code='INV-NEW').exists()


def test_materialise_uses_every_mapping_in_one_run(world):
    """Approved makes rows real (an inventory-only code gets a row), a proposal at or above the
    floor makes them provisional, a proposal below it leaves them alone."""
    approved_target = ConceptFactory(concept_name='Approved target')
    person = _person(code='APP-UNRES')                       # an unresolved row, approved mapping
    for code in ('PROP-HI', 'PROP-LO'):                      # unresolved rows, proposals only
        _inventory_only(person, code)
        MeasurementFactory(
            person=person, measurement_concept_id=NO_MATCHING_CONCEPT_ID, measurement_source_value=code,
            measurement_date=date(2026, 1, 1), confidence=None)
    _inventory_only(person, 'APP-INV')                       # inventory only, approved mapping
    _inventory_only(person, 'PROP-INV')                      # inventory only, proposal at the floor
    _inventory_only(person, 'LOW-INV')                       # inventory only, proposal below it
    _queue_row('APP-UNRES', status='approved', target_concept=approved_target)
    _queue_row('APP-INV', status='approved', target_concept=approved_target)
    _queue_row('PROP-HI', target_concept=world.concept, suggestion_confidence=0.9)
    _queue_row('PROP-LO', target_concept=world.concept, suggestion_confidence=0.55)
    _queue_row('PROP-INV', target_concept=world.concept, suggestion_confidence=0.8)
    _queue_row('LOW-INV', target_concept=world.concept, suggestion_confidence=0.4)

    summary = fill_provisional.run(person.person_id, strategies=['umls'], log=lambda m: None)

    assert world.calls == [], 'nothing needed proposing: every queue row already had a destination'
    real = _row(person, 'APP-UNRES')
    assert (real.measurement_concept_id, real.confidence) == (approved_target.concept_id, None)
    created_real = _row(person, 'APP-INV')
    assert (created_real.measurement_concept_id, created_real.confidence) == (
        approved_target.concept_id, None)
    assert (_row(person, 'PROP-HI').measurement_concept_id, _row(person, 'PROP-HI').confidence) == (
        world.concept.concept_id, 90)
    assert (_row(person, 'PROP-INV').measurement_concept_id, _row(person, 'PROP-INV').confidence) == (
        world.concept.concept_id, 80)
    low = _row(person, 'PROP-LO')
    assert (low.measurement_concept_id, low.confidence) == (NO_MATCHING_CONCEPT_ID, None)
    assert not Measurement.objects.filter(person=person, measurement_source_value='LOW-INV').exists()
    # Real, provisional and left are counted apart.
    assert summary['rows_real'] == 2 and summary['rows_real_created'] == 1
    assert summary['rows_provisional'] == 2 and summary['rows_created'] == 2
    assert summary['rows_left'] == 1 and summary['rows_reverted'] == 0


def test_both_records_are_derived_and_the_provisional_value_never_reaches_patient_record(world):
    loinc = world.concept.vocabulary
    albumin = ConceptFactory(
        concept_id=3024561, concept_code='1751-7', vocabulary=loinc, standard_concept='S',
        concept_name='Albumin [Mass/volume] in Serum or Plasma')
    person = _person(code='ALB-REAL', value=4.1)
    _inventory_only(person, 'HGB-LOCAL')
    MeasurementFactory(
        person=person, measurement_concept_id=NO_MATCHING_CONCEPT_ID,
        measurement_source_value='HGB-LOCAL', measurement_date=date(2026, 1, 1), confidence=None,
        value_as_number=11.5, value_source_value='11.5', unit_source_value='g/dL')
    _queue_row('ALB-REAL', status='approved', target_concept=albumin)
    _queue_row('HGB-LOCAL', target_concept=world.concept, suggestion_confidence=0.9)

    summary = fill_provisional.run(person.person_id, strategies=['umls'], log=lambda m: None)

    record = PatientRecord.objects.get(person=person)
    assert float(record.albumin_g_dl) == pytest.approx(4.1), 'the approved mapping feeds patient_record'
    assert record.hemoglobin_g_dl is None, 'a proposal never reaches patient_record'
    provisional = PatientRecordProvisional.objects.get(person=person)
    assert float(provisional.hemoglobin_g_dl) == pytest.approx(11.5)
    marks = PatientRecordConfidence.objects.get(person=person)
    assert marks.hemoglobin_g_dl == 90 and marks.albumin_g_dl is None
    assert 'albumin_g_dl' in summary['record_fields_changed_names']
    assert 'hemoglobin_g_dl' not in summary['record_fields_changed_names']
    assert summary['record_fields_changed'] == len(summary['record_fields_changed_names']) >= 1
    assert summary['provisional_fields'] >= 1
    # Field names only: no value of the record is printed.
    out = _fill(person, '--skip-propose')
    assert 'patient_record (approved rows only)' in out and '4.1' not in out and '11.5' not in out


def test_a_dry_run_counts_exactly_what_the_real_run_proposes(world):
    person = _person(code='HAS-ROW')
    _person_code_without_queue_row = 'NEEDS-ROW'
    _inventory_only(person, _person_code_without_queue_row)
    MeasurementFactory(
        person=person, measurement_concept_id=NO_MATCHING_CONCEPT_ID,
        measurement_source_value=_person_code_without_queue_row, measurement_date=date(2026, 1, 1),
        confidence=None)
    _queue_row('HAS-ROW')

    dry = fill_provisional.run(person.person_id, dry_run=True, log=lambda m: None)
    real = fill_provisional.run(person.person_id, strategies=['umls'], log=lambda m: None)

    assert dry['proposable_rows'] == 2 == real['propose']['considered']
    assert dry['queue_rows']['missing'] == 1 and real['queue_rows']['created'] == 1


def test_a_row_under_a_system_the_patient_does_not_hold_is_never_proposed_for(world):
    person = _person(code='SHORT-6', vocabulary='Cerner')
    other_hospital = _queue_row('SHORT-6', source_vocabulary_id='Epic')

    dry = fill_provisional.run(person.person_id, dry_run=True, log=lambda m: None)
    assert dry['proposable_rows'] == 1, 'only the Cerner row the real run would create'

    fill_provisional.run(person.person_id, strategies=['umls'], log=lambda m: None)

    other_hospital.refresh_from_db()
    assert other_hospital.target_concept_id is None and other_hospital.last_suggest_attempt == ''
    mine = SourceCodeConceptMapping.objects.get(source_code='SHORT-6', source_vocabulary_id='Cerner')
    assert mine.target_concept_id == world.concept.concept_id
    assert world.calls == ['SHORT-6']


# --- rederive -------------------------------------------------------------------------------

def test_the_record_is_rederived_with_provisional_fields_and_patient_record_unchanged(world):
    person = _person(code='HGB-LOCAL', value=11.5)
    _queue_row('HGB-LOCAL')
    before = PatientRecord.objects.get(person=person).hemoglobin_g_dl

    summary = fill_provisional.run(person.person_id, strategies=['umls'], log=lambda m: None)

    assert summary['provisional_fields'] >= 1
    marks = PatientRecordConfidence.objects.get(person=person)
    assert marks.hemoglobin_g_dl == 90
    assert PatientRecord.objects.get(person=person).hemoglobin_g_dl == before


def test_values_are_not_printed_unless_asked(world):
    person = _person(code='HGB-LOCAL', value=11.5)
    _queue_row('HGB-LOCAL')

    quiet = _fill(person)
    loud = _fill(person, '--show-values', '--skip-propose')

    assert 'hemoglobin_g_dl: 90%' in quiet and 'hemoglobin_g_dl: 90% =' not in quiet
    assert 'hemoglobin_g_dl: 90% = 11.5' in loud


# --- scope ----------------------------------------------------------------------------------

def test_another_person_is_untouched(world):
    person = _person()
    other = _person()
    _queue_row('GLU-1')

    _fill(person)

    assert _row(other).measurement_concept_id == NO_MATCHING_CONCEPT_ID
    assert _row(other).confidence is None
    assert not PatientRecordConfidence.objects.filter(person=other).exists()


def test_an_organization_scoped_row_is_never_proposed_for(world):
    from tests.factories import OrganizationFactory
    person = _person()
    scoped = _queue_row('GLU-1', organization=OrganizationFactory())

    _fill(person)

    scoped.refresh_from_db()
    assert scoped.target_concept_id is None
    assert SourceCodeConceptMapping.objects.filter(source_code='GLU-1', organization__isnull=True).exists()


# --- a ranker that cannot answer is never written as an answer (the real plumbing) -----------

def _three(jev_world=None):
    person = _person(code='A-1')
    for code in ('B-1', 'C-1'):
        PatientSourceCode.objects.create(
            person=person, source_value=code, omop_table='measurement', source_vocabulary_id='Local')
        MeasurementFactory(
            person=person, measurement_concept_id=NO_MATCHING_CONCEPT_ID, measurement_source_value=code,
            measurement_date=date(2026, 1, 1), confidence=None)
    return person, {code: _queue_row(code) for code in ('A-1', 'B-1', 'C-1')}


def test_a_mixed_batch_writes_only_the_answered_codes_and_leaves_the_rest_untouched(jev):
    person, rows = _three()
    jev.scores['B-1'] = 'fail'

    with pytest.raises(CommandError, match='did not answer 1 of 3 codes'):
        _fill(person)

    for code in ('A-1', 'C-1'):
        rows[code].refresh_from_db()
        assert rows[code].target_concept_id == jev.concept.concept_id
        assert rows[code].last_suggest_attempt == f'{suggestions.SUGGESTION_MODEL_VERSION}-jev'
    failed = rows['B-1']
    failed.refresh_from_db()
    # Not the first retrieval hit, no score, no provenance, and no attempt stamp.
    assert failed.target_concept_id is None and failed.suggestion_confidence is None
    assert failed.origin_system == '' and failed.last_suggest_attempt == ''
    assert failed.suggest_strategy == '' and failed.umls_cui == ''


def test_a_one_code_batch_the_ranker_cannot_answer_stops_the_run_and_writes_nothing(jev):
    person = _person(code='ONLY-1')
    row = _queue_row('ONLY-1')
    jev.scores['ONLY-1'] = 'fail'

    with pytest.raises(CommandError, match='did not answer 1 of 1 codes'):
        _fill(person)

    row.refresh_from_db()
    assert row.target_concept_id is None and row.last_suggest_attempt == ''


def test_a_rerun_retries_only_what_the_ranker_could_not_answer(jev):
    person, rows = _three()
    jev.scores['B-1'] = 'fail'
    with pytest.raises(CommandError):
        _fill(person)
    assert sorted(jev.calls) == ['A-1', 'B-1', 'C-1']
    jev.calls.clear()
    del jev.scores['B-1']

    _fill(person)

    assert jev.calls == ['B-1'], 'A-1 and C-1 were answered and are not asked again'
    rows['B-1'].refresh_from_db()
    assert rows['B-1'].target_concept_id == jev.concept.concept_id


def test_a_ranker_error_is_left_unstamped_too(world):
    person, rows = _three()
    world.scores['B-1'] = 'boom'

    with pytest.raises(CommandError, match='1 errors'):
        _fill(person)

    rows['B-1'].refresh_from_db()
    assert rows['B-1'].target_concept_id is None and rows['B-1'].last_suggest_attempt == ''
    rows['A-1'].refresh_from_db()
    assert rows['A-1'].target_concept_id is not None


def test_the_abort_message_does_not_advise_replace(jev):
    person = _person(code='ONLY-1')
    _queue_row('ONLY-1')
    jev.scores['ONLY-1'] = 'fail'

    with pytest.raises(CommandError) as raised:
        _fill(person)

    assert '--replace' not in str(raised.value) and 'again' in str(raised.value)


def test_a_declined_code_is_an_answer_and_keeps_its_stamp(jev):
    person = _person(code='DEC-1')
    row = _queue_row('DEC-1')
    jev.scores['DEC-1'] = None

    _fill(person)
    _fill(person)

    row.refresh_from_db()
    assert row.target_concept_id is None
    assert row.last_suggest_attempt == f'{suggestions.SUGGESTION_MODEL_VERSION}-jev'
    assert jev.calls == ['DEC-1']


def test_suggest_without_skip_fallback_still_writes_the_stand_in_for_every_other_caller(jev):
    person = _person(code='ONLY-1')
    row = _queue_row('ONLY-1')
    jev.scores['ONLY-1'] = 'fail'

    results = suggestions.suggest_mappings(
        None, min_occurrences=0, strategies=['umls'], ranking_model='jev', mapping_ids=[row.pk])

    row.refresh_from_db()
    assert row.target_concept_id == jev.concept.concept_id and row.suggestion_confidence is None
    assert 'unanswered' not in results[0]


def test_the_jev_log_never_carries_any_part_of_the_key(jev, caplog):
    person = _person(code='ONLY-1')
    _queue_row('ONLY-1')

    with caplog.at_level(logging.DEBUG):
        _fill(person)

    assert 'abcdefgh' not in caplog.text


# --- failures are reported by step, with the class and never the text ------------------------

@pytest.mark.parametrize('step', ['propose', 'materialise', 'rederive'])
def test_a_failing_step_exits_non_zero_without_quoting_the_exception(world, monkeypatch, step):
    person = _person()
    _queue_row()
    secret = 'DETAIL: Key (source_code)=(MRN-12345) already exists.'

    def boom(*args, **kwargs):
        raise RuntimeError(secret)
    monkeypatch.setattr(fill_provisional, step, boom)

    with pytest.raises(CommandError) as raised:
        _fill(person)

    message = str(raised.value)
    assert 'RuntimeError' in message and 'MRN-12345' not in message and secret not in message
    assert 'again' in message


# --- the queue rows it creates ---------------------------------------------------------------

def test_a_created_row_counts_one_not_this_patients_rows_and_carries_the_inventory_metadata(world):
    metadata = {'resourceType': 'Observation', 'code': {'text': 'Hemoglobin, whole blood'}}
    person = _person(code='NEW-1', rows=3, metadata=metadata)

    _fill(person)

    row = SourceCodeConceptMapping.objects.get(source_code='NEW-1')
    assert row.occurrence_count == 1
    assert row.source_metadata == metadata


def test_a_created_row_is_named_from_athena_like_the_enqueue_command_names_it(world):
    person = _person(code='718-7', vocabulary='LOINC')

    _fill(person, '--skip-propose')
    assert not SourceCodeConceptMapping.objects.filter(source_code='718-7').exists(), \
        'skip-propose creates nothing'
    _fill(person)

    row = SourceCodeConceptMapping.objects.get(source_code='718-7', source_vocabulary_id='LOINC')
    assert row.source_code_description == world.concept.concept_name


def test_an_hk_source_system_gets_no_queue_row_and_is_counted(world):
    person = _person(code='MINT-1', vocabulary='HK-Labs')

    summary = fill_provisional.run(person.person_id, strategies=['umls'], log=lambda m: None)

    assert not SourceCodeConceptMapping.objects.filter(source_code='MINT-1').exists()
    assert summary['queue_rows']['hk_skipped'] == 1 and summary['queue_rows']['created'] == 0


def test_every_code_system_the_inventory_holds_gets_a_row_and_a_case_variant_is_not_a_new_code(world):
    person = _person(code='Glu', vocabulary='Cerner')
    PatientSourceCode.objects.create(
        person=person, source_value='Glu', omop_table='measurement', source_vocabulary_id='Epic')
    epic = _queue_row('GLU', source_vocabulary_id='Epic')

    summary = fill_provisional.run(person.person_id, strategies=['umls'], log=lambda m: None)

    rows = SourceCodeConceptMapping.objects.filter(source_code__iexact='GLU')
    assert sorted(rows.values_list('source_vocabulary_id', 'source_code')) == [
        ('Cerner', 'Glu'), ('Epic', 'GLU')]
    assert summary['queue_rows']['created'] == 1
    epic.refresh_from_db()
    assert epic.target_concept_id == world.concept.concept_id
    assert sorted(world.calls) == ['GLU', 'Glu']


def test_a_row_the_unique_constraint_would_refuse_is_skipped_not_fatal_and_dry_run_agrees(world):
    person = _person(code='COL-1')
    # The same system and code under another table: the constraint ignores the table.
    _queue_row('COL-1', omop_table='observation', domain_id='Observation')

    dry = fill_provisional.run(person.person_id, dry_run=True, log=lambda m: None)
    assert dry['queue_rows'] == {'created': 0, 'missing': 0, 'collisions': 1, 'hk_skipped': 0}

    summary = fill_provisional.run(person.person_id, strategies=['umls'], log=lambda m: None)
    assert summary['queue_rows']['collisions'] == 1 and summary['queue_rows']['created'] == 0
    assert SourceCodeConceptMapping.objects.filter(source_code='COL-1').count() == 1
    assert _row(person, 'COL-1').measurement_concept_id == NO_MATCHING_CONCEPT_ID


# --- the report follows the real floor -------------------------------------------------------

def test_bands_follow_the_floor_and_an_unusable_destination_is_reported_apart(world, settings):
    settings.PROVISIONAL_CONFIDENCE_FLOOR = 80
    person = _person(code='MID-1')
    for code in ('LOW-1', 'BAD-1'):
        PatientSourceCode.objects.create(
            person=person, source_value=code, omop_table='measurement', source_vocabulary_id='Local')
    unusable = ConceptFactory(concept_name='Not standard', standard_concept=None)
    _queue_row('MID-1', target_concept=world.concept, suggestion_confidence=0.75)
    _queue_row('LOW-1', target_concept=world.concept, suggestion_confidence=0.45)
    _queue_row('BAD-1', target_concept=unusable, suggestion_confidence=0.9)

    summary = fill_provisional.run(person.person_id, skip_propose=True, log=lambda m: None)

    assert summary['report']['status'] == {'below_floor': 2, 'destination_not_usable': 1}
    assert summary['report']['bands'] == {'70-79%': 1, 'below 60%': 1}
    assert summary['report']['floor'] == 80


def test_band_labels_for_other_floors():
    assert fill_provisional.band(65, 70) == '60-69%' and fill_provisional.band(55, 70) == '50-59%'
    assert fill_provisional.band(10, 70) == 'below 50%'
    assert fill_provisional.band(75, 80) == '70-79%' and fill_provisional.band(50, 80) == 'below 60%'


# --- mapping_ids narrows Suggest and only narrows it ----------------------------------------

def test_mapping_ids_narrows_the_set_and_an_empty_list_is_nothing_not_everything(world):
    one, two = _queue_row('ONE-1'), _queue_row('TWO-1')

    everything = set(suggestable_queryset(None, min_occurrences=0).values_list('pk', flat=True))
    narrowed = set(suggestable_queryset(None, min_occurrences=0, mapping_ids=[one.pk])
                   .values_list('pk', flat=True))
    empty = list(suggestable_queryset(None, min_occurrences=0, mapping_ids=[]))

    assert everything == {one.pk, two.pk} and narrowed == {one.pk} and empty == []
    approved = _queue_row('APP-1', status='approved', target_concept=world.concept)
    assert not list(suggestable_queryset(None, min_occurrences=0, mapping_ids=[approved.pk]))
