"""``manage.py fill_provisional_record``: one patient's full provisional view (#2696).

Proposes for the person's codes through the normal Suggest pipeline, makes their
rows provisional at proposals at or above the site floor, and rederives their
record. The ranker is replaced here; nothing calls Jev or Anthropic.
"""
from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from omop_core.mapping import suggestions
from omop_core.mapping.code_resolution import NO_MATCHING_CONCEPT_ID
from omop_core.models import (
    Measurement, PatientRecord, PatientRecordConfidence, PatientSourceCode,
    SourceCodeConceptMapping,
)
from omop_core.services import fill_provisional
from tests.factories import (
    ConceptFactory, DomainFactory, MeasurementFactory, PatientRecordFactory, PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db

PREFLIGHT_CODE = 'HGB'     # the question the command asks the ranker before it starts


@pytest.fixture()
def world(settings, monkeypatch):
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
    world.concept, world.calls = hemoglobin, []
    world.scores = {}          # source code -> confidence; None declines; 'boom' raises; 'fallback'
    world.default = 0.9

    def rank(source_value, candidates, *args, **kwargs):
        if source_value != PREFLIGHT_CODE:
            world.calls.append(source_value)
        outcome = world.scores.get(source_value, world.default)
        if outcome == 'boom':
            raise RuntimeError('ranker exploded')
        if outcome == 'fallback':
            return (candidate, 'Best-match fallback (score 1.0). Ranking model unavailable, so this '
                    'is the first candidate in retrieval fallback order.', None, {})
        if outcome is None:
            return None, 'No suitable concept among the candidates.', [], {}
        return candidate, 'ranked', [{'concept_id': hemoglobin.concept_id, 'confidence': outcome}], {}

    monkeypatch.setattr(suggestions, 'umls_candidates', lambda *a, **k: ([candidate], 'C1'))
    monkeypatch.setattr(suggestions, 'rank_candidates_dispatch', rank)
    return world


def _person(code='GLU-1', with_row=True, value=11.5):
    """A person holding the code unresolved, the way an ETL load leaves it."""
    person = PersonFactory()
    PatientRecordFactory(person=person)
    PatientSourceCode.objects.create(
        person=person, source_value=code, omop_table='measurement', source_vocabulary_id='Local')
    if with_row:
        MeasurementFactory(
            person=person, measurement_concept_id=NO_MATCHING_CONCEPT_ID,
            measurement_source_value=code, measurement_date=date(2026, 1, 1), confidence=None,
            value_as_number=value, value_source_value=str(value), unit_source_value='g/dL')
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


def test_a_ranker_error_on_one_code_does_not_stop_the_others(world):
    person = _person(code='ERR-1')
    PatientSourceCode.objects.create(
        person=person, source_value='OK-1', omop_table='measurement', source_vocabulary_id='Local')
    MeasurementFactory(
        person=person, measurement_concept_id=NO_MATCHING_CONCEPT_ID, measurement_source_value='OK-1',
        measurement_date=date(2026, 1, 1), confidence=None)
    bad, good = _queue_row('ERR-1'), _queue_row('OK-1')
    world.scores['ERR-1'] = 'boom'

    summary = fill_provisional.run(person.person_id, strategies=['umls'], log=lambda m: None)

    assert summary['propose']['errors'] == 1 and summary['propose']['proposed'] == 1
    good.refresh_from_db()
    assert good.target_concept_id == world.concept.concept_id
    bad.refresh_from_db()
    assert bad.target_concept_id is None


def test_a_ranker_that_cannot_answer_stops_the_run_before_anything_is_proposed(world):
    person = _person()
    row = _queue_row()
    world.scores[PREFLIGHT_CODE] = 'fallback'

    with pytest.raises(CommandError, match='did not answer a test question'):
        _fill(person)

    row.refresh_from_db()
    assert row.target_concept_id is None and world.calls == []


def test_a_batch_where_every_code_fell_back_stops_the_run(world):
    person = _person(code='F-0')
    for code in ('F-1', 'F-2'):
        PatientSourceCode.objects.create(
            person=person, source_value=code, omop_table='measurement', source_vocabulary_id='Local')
    for code in ('F-0', 'F-1', 'F-2'):
        _queue_row(code)
        world.scores[code] = 'fallback'

    with pytest.raises(CommandError, match='did not answer any of a batch'):
        _fill(person)


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


def test_an_inventory_only_code_gets_a_provisional_row(world):
    person = _person(code='INV-1', with_row=False)
    _queue_row('INV-1')

    summary = fill_provisional.run(person.person_id, strategies=['umls'], log=lambda m: None)

    row = _row(person, 'INV-1')
    assert (row.measurement_concept_id, row.confidence) == (world.concept.concept_id, 90)
    assert summary['rows_created'] == 1


def test_skip_propose_only_materialises_the_proposals_that_exist(world):
    person = _person()
    _queue_row('GLU-1', target_concept=world.concept, suggestion_confidence=0.85)

    summary = fill_provisional.run(person.person_id, skip_propose=True, log=lambda m: None)

    assert world.calls == [] and summary['propose'] == {}
    assert _row(person).confidence == 85


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
