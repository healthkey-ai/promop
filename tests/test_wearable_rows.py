"""Wearable readings as source codes (#2835): which code a metric arrives under,
what the resolver's device rules decide, and the rule that every metric an
import can write has a 30-day field.

What an import stores, how its rows follow a mapping through the API, and the
pages that show it are in ``patient_portal.tests.WearableSourceCodesTest``.
"""
import importlib
import inspect
import re
from datetime import timedelta

import pytest
from django.apps import apps
from django.utils import timezone

from omop_core.mapping.code_resolution import (
    choose_proposal, proposal_percent, provisional_proposal,
)
from omop_core.models import (
    Measurement, Observation, PatientRecord, PatientRecordConfidence, PatientSourceCode,
    SourceCodeConceptMapping, WearableUpload,
)
from omop_core.services import garmin_connect, garmin_oauth, open_wearables, wearable_parsers, wearable_rows
from omop_core.services.mappings import (
    WEARABLE_ARTIFACT_BOUNDS, WEARABLE_CONCEPT_CODE, WEARABLE_CONCEPT_VOCAB, WEARABLE_METRIC_FIELDS,
    WEARABLE_OBSERVATION_METRICS, WEARABLE_TYPE_CONCEPT_ID,
)
from omop_core.services.patient_record_service import _get_wearable_data
from omop_core.services.source_vocabularies import WEARABLE_DEVICE_VOCABULARIES, WEARABLE_SOURCE_VOCABULARIES
from omop_core.signals import suppress_patient_record_refresh
from tests.factories import (
    ConceptFactory, DomainFactory, MeasurementFactory, ObservationFactory, OrganizationFactory,
    PatientRecordFactory, VocabularyFactory,
)

pytestmark = pytest.mark.django_db


def concept_for(metric, standard='S'):
    domain = 'Observation' if metric in WEARABLE_OBSERVATION_METRICS else 'Measurement'
    local = WEARABLE_CONCEPT_VOCAB[metric] != 'LOINC'
    return ConceptFactory(
        vocabulary=VocabularyFactory(vocabulary_id=WEARABLE_CONCEPT_VOCAB[metric]),
        concept_code=WEARABLE_CONCEPT_CODE[metric], domain=DomainFactory(domain_id=domain),
        standard_concept=None if local else standard)


def mapping(vocabulary, code, metric, status='approved', **more):
    table = more.pop('omop_table', 'observation' if metric in WEARABLE_OBSERVATION_METRICS else 'measurement')
    return SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=vocabulary, source_code=code, status=status, omop_table=table,
        target_concept=more.pop('concept', None) or concept_for(metric), **more)


# ── Every metric an import can write has a 30-day field ─────────────────────


def test_every_metric_has_30_day_fields_the_record_holds_and_can_mark():
    assert set(WEARABLE_METRIC_FIELDS) == set(WEARABLE_CONCEPT_CODE)
    record = {f.name for f in PatientRecord._meta.concrete_fields}
    markable = {f.name for f in PatientRecordConfidence._meta.concrete_fields}
    for metric, fields in WEARABLE_METRIC_FIELDS.items():
        assert fields, metric
        for name, label in fields:
            assert name in record, (metric, name)
            # Owned by derivation, so a value from provisional rows can be marked.
            assert name in markable, (metric, name)
            assert label


def test_no_importer_writes_a_metric_without_a_30_day_field():
    """The parsers and readers name their metrics in different ways; all of them are held here."""
    written = set(wearable_parsers._APPLE_TYPE_MAP.values()) | {'sleep_duration'}
    written |= set(open_wearables._ANDROID_TYPE_MAP.values())
    for module in (garmin_connect, garmin_oauth, wearable_parsers):
        source = inspect.getsource(module)
        written |= set(re.findall(r"\bkeep\(\s*'([a-z0-9_]+)'", source))
        written |= set(re.findall(r"metric_key='([a-z0-9_]+)'", source))
    assert written >= {'steps', 'resting_hr', 'hrv_rmssd', 'sleep_duration', 'spo2'}
    assert written <= set(WEARABLE_METRIC_FIELDS), sorted(written - set(WEARABLE_METRIC_FIELDS))


@pytest.mark.parametrize('metric', sorted(WEARABLE_CONCEPT_CODE))
def test_a_metrics_readings_alone_produce_its_30_day_fields(metric):
    person = PatientRecordFactory().person
    concept = concept_for(metric)
    low, high = WEARABLE_ARTIFACT_BOUNDS[metric]
    today = timezone.now().date()
    with suppress_patient_record_refresh():
        for back in range(10):
            day = today - timedelta(days=back)
            value = round(low + (high - low) / 4, 2)
            # Filed under a device's code, as an import leaves it: read by concept.
            if metric in WEARABLE_OBSERVATION_METRICS:
                ObservationFactory(person=person, observation_concept=concept, observation_date=day,
                                   value_as_number=value, observation_source_value=metric)
            else:
                MeasurementFactory(person=person, measurement_concept=concept, measurement_date=day,
                                   value_as_number=value, measurement_source_value=metric)

    data = _get_wearable_data(person)

    for name, _label in WEARABLE_METRIC_FIELDS[metric]:
        assert data.get(name) is not None, (metric, name)


# ── Which code a metric arrives under ───────────────────────────────────────


def test_the_device_vocabularies_are_wearable_vocabularies_with_a_device_each():
    assert WEARABLE_DEVICE_VOCABULARIES <= WEARABLE_SOURCE_VOCABULARIES
    assert set(wearable_rows.DEVICE_VOCABULARY.values()) == set(WEARABLE_DEVICE_VOCABULARIES)


def test_each_device_names_a_metric_its_own_way():
    garmin = wearable_rows.device_codes('garmin', ['steps', 'resting_hr'])
    apple = wearable_rows.device_codes('apple', ['steps', 'sleep_duration', 'hrv_rmssd'])
    android = wearable_rows.device_codes('android', ['steps', 'hrv_rmssd', 'walking_speed'])

    assert {k: (c.vocabulary, c.source_code, c.table) for k, c in garmin.items()} == {
        'steps': ('Garmin', 'steps', 'observation'), 'resting_hr': ('Garmin', 'resting_hr', 'measurement')}
    # Apple has no code for a metric HealthKit does not export under.
    assert {k: c.source_code for k, c in apple.items()} == {
        'steps': 'HKQuantityTypeIdentifierStepCount', 'sleep_duration': 'HKCategoryTypeIdentifierSleepAnalysis'}
    # Health Connect's record types; a metric with none is filed under its own key.
    assert {k: c.source_code for k, c in android.items()} == {
        'steps': 'StepsRecord', 'hrv_rmssd': 'HeartRateVariabilityRmssdRecord',
        'walking_speed': 'walking_speed'}
    assert all(c.concept is None and c.mapping_id is None for c in {**garmin, **apple, **android}.values())
    assert wearable_rows.device_codes('fitbit', ['steps']) == {}


def test_only_an_approved_global_mapping_for_the_devices_own_code_gives_a_concept():
    approved = mapping('Garmin', 'steps', 'steps')
    mapping('Garmin', 'resting_hr', 'resting_hr', status='proposed', suggestion_confidence=0.99)
    mapping('Garmin', 'spo2', 'spo2', organization=OrganizationFactory())
    mapping('OpenWearables', 'vo2_max', 'vo2_max')                     # another vocabulary's code

    codes = wearable_rows.device_codes('garmin', ['steps', 'resting_hr', 'spo2', 'vo2_max'])

    assert codes['steps'].concept == approved.target_concept and codes['steps'].mapping_id == approved.pk
    assert codes['resting_hr'].concept is None and codes['resting_hr'].mapping_id is not None
    assert codes['spo2'].concept is None and codes['spo2'].mapping_id is None
    assert codes['vo2_max'].concept is None and codes['vo2_max'].mapping_id is None


def test_a_code_is_filed_in_its_mappings_table_and_cut_to_the_row_column():
    mapping('Garmin', 'steps', 'steps', status='proposed', omop_table='measurement')
    codes = wearable_rows.device_codes('garmin', ['steps'])
    assert codes['steps'].table == 'measurement'

    long = wearable_rows.device_codes('apple', ['walking_double_support_pct'])['walking_double_support_pct']
    assert len(long.source_code) == 54 and long.stored_code() == long.source_code[:50]


def test_health_connect_follows_an_approved_mapping_that_names_the_metric():
    concept = concept_for('steps')
    SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='HealthConnect', source_code='StepsCadenceRecord', metric_key='steps',
        status='approved', omop_table='observation', target_concept=concept)
    code = wearable_rows.device_codes('android', ['steps'])['steps']
    # The rows go under the code the approval's re-point will look for.
    assert (code.source_code, code.concept) == ('StepsCadenceRecord', concept)


def test_a_code_no_mapping_row_holds_is_queued_once():
    codes = wearable_rows.device_codes('garmin', ['body_mass'])
    assert wearable_rows.queue_unmapped(codes.values()) == 1
    assert wearable_rows.queue_unmapped(codes.values()) == 0
    queued = SourceCodeConceptMapping.objects.get(source_vocabulary_id='Garmin', source_code='body_mass')
    assert (queued.status, queued.target_concept_id, queued.omop_table, queued.metric_key,
            queued.origin_system) == ('proposed', None, 'measurement', 'body_mass', 'wearable-import')
    assert wearable_rows.queue_unmapped(wearable_rows.device_codes('garmin', ['body_mass']).values()) == 0


# ── The resolver's rules for a device's code ────────────────────────────────


@pytest.fixture()
def provisional_on(settings):
    settings.PROVISIONAL_DATA_ENABLED = True
    settings.PROVISIONAL_CONFIDENCE_FLOOR = 70


@pytest.mark.parametrize('vocabulary, score, percent', [
    ('Garmin', None, 70),        # curated, unscored: at the floor
    ('Garmin', 0.85, 85),
    ('Garmin', 0.55, None),      # a ranker's score below the floor still disqualifies
    ('OpenWearables', None, None),   # not a device's vocabulary: no rows are filed under it
    ('urn:oid:1.2.3', None, None),   # a hospital's unscored proposal never qualified, and does not now
    ('urn:oid:1.2.3', 0.85, 85),
])
def test_only_a_devices_proposal_qualifies_unscored(provisional_on, vocabulary, score, percent):
    proposal = mapping(vocabulary, 'steps', 'steps', status='proposed', suggestion_confidence=score)
    assert proposal_percent(proposal, 'observation') == percent


def test_an_unscored_proposal_never_stands_at_zero(settings):
    settings.PROVISIONAL_DATA_ENABLED, settings.PROVISIONAL_CONFIDENCE_FLOOR = True, 0
    proposal = mapping('Garmin', 'steps', 'steps', status='proposed')
    # Null is an accepted row; 0 would read as one to anything testing it for truth.
    assert proposal_percent(proposal, 'observation') == 1


def test_a_local_wearable_concept_is_a_destination_for_a_devices_proposal_only(provisional_on):
    local = concept_for('hrv_rmssd')
    assert (local.vocabulary_id, local.standard_concept) == ('HK-Wearable', None)
    device = mapping('Garmin', 'hrv_rmssd', 'hrv_rmssd', status='proposed', concept=local)
    hospital = mapping('urn:oid:1.2.3', 'HRV', 'hrv_rmssd', status='proposed', concept=local,
                       suggestion_confidence=0.95)
    assert proposal_percent(device, 'measurement') == 70
    assert proposal_percent(hospital, 'measurement') is None
    # In the wrong table's domain it is no destination for anyone.
    assert proposal_percent(device, 'observation') is None


def test_a_devices_proposal_answers_for_a_holder_and_nobody_else(provisional_on):
    garmin = mapping('Garmin', 'spo2', 'spo2', status='proposed')
    assert provisional_proposal('spo2', 'measurement', {'Garmin'}) == (garmin.target_concept, 70)
    assert provisional_proposal('spo2', 'measurement', {'urn:oid:1.2.3'}) is None
    assert provisional_proposal('spo2', 'measurement', None) is None
    assert provisional_proposal('spo2', 'measurement', {'Garmin', 'urn:oid:1.2.3'}) is None


def test_another_systems_proposal_neither_answers_for_nor_blocks_a_devices_code():
    entries = [(1, 70, 'Garmin'), (2, 95, 'urn:oid:1.2.3')]
    assert choose_proposal(entries, {'Garmin'}) == (1, 70)
    assert choose_proposal(entries, {'urn:oid:1.2.3'}) == (2, 95)
    assert choose_proposal(entries, None) == (2, 95)
    # Two devices' codes of the same spelling are still two code systems.
    assert choose_proposal([(1, 70, 'Garmin'), (2, 70, 'Apple')], {'Garmin'}) == (1, 70)
    assert choose_proposal([(1, 70, 'Garmin')], {'Garmin', 'Apple'}) is None


def test_a_row_cut_to_its_column_still_finds_its_long_code(provisional_on):
    code = 'HKQuantityTypeIdentifierWalkingDoubleSupportPercentage'
    proposal = mapping('Apple', code, 'walking_double_support_pct', status='proposed')
    assert provisional_proposal(code[:50], 'measurement', {'Apple'}) == (proposal.target_concept, 70)
    # A shorter code that is only a prefix of it is another code.
    assert provisional_proposal(code[:40], 'measurement', {'Apple'}) is None


# ── The inventory ───────────────────────────────────────────────────────────


def stored(person, code, concept_id=0, confidence=None, value=52.0):
    type_concept = ConceptFactory(concept_id=WEARABLE_TYPE_CONCEPT_ID, concept_code='TYPE-WEARABLE',
                                  vocabulary=VocabularyFactory(vocabulary_id='Type Concept'))
    concept = ConceptFactory(concept_id=concept_id, concept_code=f'C-{concept_id}') if concept_id == 0 else None
    with suppress_patient_record_refresh():
        return MeasurementFactory(
            person=person, measurement_concept=concept or ConceptFactory(), value_as_number=value,
            measurement_date=timezone.now().date(), measurement_type_concept=type_concept,
            measurement_source_value=code, confidence=confidence)


def test_the_inventory_follows_the_rows_stored_for_a_code():
    person = PatientRecordFactory().person
    code = wearable_rows.device_codes('garmin', ['resting_hr'])['resting_hr']
    units = {'resting_hr': '/min'}

    wearable_rows.record_inventory(person, [code], units)
    assert not PatientSourceCode.objects.filter(person=person).exists()     # no row, no entry

    stored(person, 'resting_hr')
    stored(person, 'resting_hr', value=60.0)
    wearable_rows.record_inventory(person, [code], units)
    entry = PatientSourceCode.objects.get(person=person)
    assert (entry.source_vocabulary_id, entry.source_value, entry.omop_table, entry.occurrence_count,
            entry.source_unit, entry.source_metadata) == (
        'Garmin', 'resting_hr', 'measurement', 2, '/min', {'device': 'Garmin', 'metric': 'resting_hr'})

    Measurement.objects.filter(person=person).delete()
    wearable_rows.prune_inventory(person)
    assert not PatientSourceCode.objects.filter(person=person).exists()


def test_pruning_leaves_a_hospitals_inventory_alone():
    person = PatientRecordFactory().person
    PatientSourceCode.objects.create(
        person=person, source_value='GLU', source_vocabulary_id='urn:oid:1.2.3', omop_table='measurement')
    wearable_rows.prune_inventory(person)
    assert PatientSourceCode.objects.filter(person=person).count() == 1


def test_only_codes_holding_a_waiting_row_are_handed_to_the_resolver():
    person = PatientRecordFactory().person
    codes = wearable_rows.device_codes('garmin', ['resting_hr', 'spo2', 'vo2_max'])
    stored(person, 'resting_hr')                                   # unresolved
    stored(person, 'spo2', concept_id=None, confidence=70)         # provisional
    stored(person, 'vo2_max', concept_id=None)                     # accepted

    waiting = wearable_rows.needs_resolving(person, codes.values())

    assert {code.metric_key for code in waiting} == {'resting_hr', 'spo2'}


# ── Migration 0371: rows stored before they carried their device code ───────

refile = importlib.import_module('omop_core.migrations.0371_wearable_rows_carry_source_codes').refile


def legacy(person, metric, value=52.0, source=None, type_id=WEARABLE_TYPE_CONCEPT_ID):
    """A row as the importer wrote it before #2835: its own concept's code as the source value."""
    concept = concept_for(metric)
    type_concept = ConceptFactory(concept_id=type_id, concept_code=f'TYPE-{type_id}',
                                  vocabulary=VocabularyFactory(vocabulary_id='Type Concept'))
    day = timezone.now().date()
    source = concept.concept_code if source is None else source
    with suppress_patient_record_refresh():
        if metric in WEARABLE_OBSERVATION_METRICS:
            return 'observation', ObservationFactory(
                person=person, observation_concept=concept, observation_date=day, value_as_number=value,
                observation_type_concept=type_concept, observation_source_value=source)
        return 'measurement', MeasurementFactory(
            person=person, measurement_concept=concept, measurement_date=day, value_as_number=value,
            measurement_type_concept=type_concept, measurement_source_value=source)


def history(person, device, *entries):
    WearableUpload.objects.create(
        person=person, device_type=device, filename='sync', sample_summary=[
            {'metric': metric, 'date': '2026-11-20', 'value': 1, 'row_id': row.pk, 'row_table': table}
            for metric, (table, row) in entries])


def test_the_migration_refiles_named_rows_under_their_device_code_and_lists_them():
    person = PatientRecordFactory().person
    steps, resting, sleep = legacy(person, 'steps'), legacy(person, 'resting_hr'), legacy(person, 'sleep_duration')
    walking = legacy(person, 'walking_double_support_pct', value=28.0)
    history(person, 'garmin', ('steps', steps), ('resting_hr', resting))
    history(person, 'apple', ('sleep_duration', sleep), ('walking_double_support_pct', walking))

    refile(apps, None)
    refile(apps, None)          # twice is once

    for _table, row in (steps, resting, sleep, walking):
        row.refresh_from_db()
    assert steps[1].observation_source_value == 'steps'
    assert resting[1].measurement_source_value == 'resting_hr'
    assert sleep[1].observation_source_value == 'HKCategoryTypeIdentifierSleepAnalysis'
    # Cut to the column; the inventory keeps the whole code.
    long = 'HKQuantityTypeIdentifierWalkingDoubleSupportPercentage'
    assert walking[1].measurement_source_value == long[:50]
    assert sorted(PatientSourceCode.objects.filter(person=person).values_list(
        'source_vocabulary_id', 'source_value', 'omop_table', 'occurrence_count', 'source_unit')) == [
        ('Apple', 'HKCategoryTypeIdentifierSleepAnalysis', 'observation', 1, 'h'),
        ('Apple', long, 'measurement', 1, '%'),
        ('Garmin', 'resting_hr', 'measurement', 1, '/min'),
        ('Garmin', 'steps', 'observation', 1, '/d'),
    ]
    # Concept and value are as they were: only where the row is filed changed.
    assert steps[1].observation_concept.concept_code == WEARABLE_CONCEPT_CODE['steps']


def test_the_migration_leaves_a_row_it_cannot_vouch_for():
    person, other = PatientRecordFactory().person, PatientRecordFactory().person
    unnamed = legacy(person, 'steps')                                   # from before uploads named rows
    corrected = legacy(person, 'resting_hr', source='RHR by hand')      # not as the importer left it
    hospital = legacy(person, 'spo2', type_id=32817)                    # not a wearable row
    theirs = legacy(other, 'vo2_max')                                   # named by another person's upload
    history(person, 'garmin', ('resting_hr', corrected), ('spo2', hospital), ('vo2_max', theirs))
    WearableUpload.objects.create(person=person, device_type='fitbit', filename='x', sample_summary=[
        {'metric': 'steps', 'row_id': unnamed[1].pk, 'row_table': 'observation'}])
    WearableUpload.objects.create(person=person, device_type='garmin', filename='odd', sample_summary=[
        {'metric': 'steps', 'row_id': 'abc', 'row_table': 'observation'}, 'junk',
        {'metric': 'steps', 'row_id': unnamed[1].pk, 'row_table': 'person'}])

    refile(apps, None)

    assert Observation.objects.get(pk=unnamed[1].pk).observation_source_value == WEARABLE_CONCEPT_CODE['steps']
    assert Measurement.objects.get(pk=corrected[1].pk).measurement_source_value == 'RHR by hand'
    assert Measurement.objects.get(pk=hospital[1].pk).measurement_source_value == WEARABLE_CONCEPT_CODE['spo2']
    assert Measurement.objects.get(pk=theirs[1].pk).measurement_source_value == WEARABLE_CONCEPT_CODE['vo2_max']
    assert not PatientSourceCode.objects.exists()


def test_the_migration_files_health_connect_under_the_approved_mappings_code():
    person = PatientRecordFactory().person
    steps, resting = legacy(person, 'steps'), legacy(person, 'resting_hr')
    history(person, 'android', ('steps', steps), ('resting_hr', resting))
    SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='HealthConnect', source_code='StepsCadenceRecord', metric_key='steps',
        status='approved', omop_table='observation', target_concept=steps[1].observation_concept)

    refile(apps, None)

    assert Observation.objects.get(pk=steps[1].pk).observation_source_value == 'StepsCadenceRecord'
    assert Measurement.objects.get(pk=resting[1].pk).measurement_source_value == 'RestingHeartRateRecord'


# ── Review regressions: a prefix must never pick between two hospital codes ─


def test_two_hospital_codes_sharing_their_first_fifty_characters_stay_two_codes(provisional_on):
    from omop_core.mapping.code_resolution import resolve_person_source_codes
    from omop_core.services.derivation_jobs import FakeDispatcher, use_dispatcher
    person = PatientRecordFactory().person
    ConceptFactory(concept_id=0, concept_code='No matching concept')
    prefix = 'SERUM FREE LIGHT CHAIN KAPPA LAMBDA RATIO PANEL XX'
    assert len(prefix) == 50
    one, other = ConceptFactory(domain=DomainFactory(domain_id='Measurement')), ConceptFactory(
        domain=DomainFactory(domain_id='Measurement'))
    for code, concept in ((prefix + ' ALPHA 1', one), (prefix + ' GAMMA', other)):
        SourceCodeConceptMapping.objects.create(
            source_vocabulary_id='urn:oid:1.2.3', source_code=code, omop_table='measurement',
            status='approved', target_concept=concept)
    PatientSourceCode.objects.create(
        person=person, source_value=prefix + ' GAMMA', source_vocabulary_id='urn:oid:1.2.3',
        omop_table='measurement')
    with suppress_patient_record_refresh():
        row = MeasurementFactory(person=person, measurement_concept_id=0, value_as_number=1.2,
                                 measurement_source_value=prefix)

    with use_dispatcher(FakeDispatcher()):
        resolve_person_source_codes(person, omop_tables=['measurement'], source_values=[prefix])

    row.refresh_from_db()
    assert row.measurement_concept_id == 0


def test_the_sweeps_index_holds_a_long_device_code_as_a_row_holds_it(provisional_on):
    from omop_core.services.provisional_sweep import build_qualifying_index
    code = 'HKQuantityTypeIdentifierWalkingDoubleSupportPercentage'
    mapping('Apple', code, 'walking_double_support_pct', status='proposed')
    entries = build_qualifying_index()['entries']['measurement']
    assert set(entries) == {code.upper(), code[:50].upper()}
    assert entries[code[:50].upper()] == entries[code.upper()]


@pytest.mark.django_db
def test_oura_files_its_codes_under_its_own_spelling_not_garmins():
    """A person holding both devices must not have one's mapping answer for the other's rows."""
    from omop_core.services import wearable_rows

    garmin = wearable_rows.device_codes('garmin', ['steps', 'hrv_rmssd'])
    oura = wearable_rows.device_codes('oura', ['steps', 'hrv_rmssd'])
    assert {c.source_code for c in garmin.values()} == {'steps', 'hrv_rmssd'}
    assert {c.source_code for c in oura.values()} == {'oura_steps', 'oura_hrv_rmssd'}
    assert {c.vocabulary for c in oura.values()} == {'Oura'}
