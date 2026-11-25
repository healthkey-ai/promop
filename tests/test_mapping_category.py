"""Category, Measure Class and Metric Key of a wearable code mapping: model,
API, sort, rollup and copy. All three describe the source metric and follow one
set of rules, so most tests run once per field."""
import re
from collections import defaultdict
from types import SimpleNamespace

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from omop_core.models import GroupAccess, Organization, SourceCodeConceptMapping
from omop_core.services.mapping_rollup import group_entries
from omop_core.services.source_vocabularies import (
    METRIC_KEY_MAX_LENGTH, WEARABLE_MEASURE_CLASSES, WEARABLE_METRIC_CATEGORIES,
    WEARABLE_SOURCE_VOCABULARIES, clean_metric_key,
)
from patient_portal.api.views import (
    code_mapping_detail, code_mapping_group, code_mapping_list, code_mapping_reference,
)
from patient_portal.models import Identity
from tests.factories import ConceptFactory

pytestmark = pytest.mark.django_db

# name: the column. values: three it accepts. refused: values that are a 400,
# and a fragment of the message that says why.
FIELDS = [
    SimpleNamespace(
        name='category', values=('Activity', 'Sleep', 'Fitness'),
        refused=('Sleeping', 'sleep', 'Activity; Sleep', 'x' * 60),
        message='Expected one of: Activity, Fitness, Heart & Cardiovascular'),
    SimpleNamespace(
        name='measure_class',
        values=('Direct measurement', 'Vendor score', 'Algorithmic estimate'),
        refused=('Sensor', 'vendor score', 'Direct measurement; Vendor score', 'x' * 60),
        message='Expected one of: Direct measurement, Aggregate over interval'),
    SimpleNamespace(
        name='metric_key', values=('steps', 'resting_hr', 'vo2_max'),
        refused=('Resting HR', 'restingHr', 'resting-hr', '9lives', '_steps', 'stéps',
                 'a' * (METRIC_KEY_MAX_LENGTH + 1)),
        message='lowercase snake case'),
]
each_field = pytest.mark.parametrize('f', FIELDS, ids=[f.name for f in FIELDS])


@pytest.fixture
def curator():
    return Identity.objects.create_user(email='category@example.test', is_staff=True)


def with_role(role):
    user = Identity.objects.create_user(email=f'category-{role}@example.test')
    org, _ = Organization.objects.get_or_create(
        slug='category-org', defaults={'name': 'Category Org'})
    GroupAccess.objects.create(identity=user, org=org, role=role)
    return user


def row(code, **kwargs):
    return SourceCodeConceptMapping.objects.create(source_code=code, **{
        'source_vocabulary_id': 'OpenWearables', 'status': 'proposed',
        'domain_id': 'Measurement', 'omop_table': 'measurement', **kwargs})


def browse(user, **params):
    request = APIRequestFactory().get(
        '/api/v1/code-mappings/', {'browse': '1', 'source': 'OpenWearables', **params})
    force_authenticate(request, user=user)
    return code_mapping_list(request)


def patch(user, mapping, **data):
    request = APIRequestFactory().patch(
        f'/api/v1/code-mappings/{mapping.pk}/', data, format='json')
    force_authenticate(request, user=user)
    return code_mapping_detail(request, mapping_id=mapping.pk)


def post(user, **data):
    request = APIRequestFactory().post('/api/v1/code-mappings/', data, format='json')
    force_authenticate(request, user=user)
    return code_mapping_list(request)


def stored(mapping, field):
    mapping.refresh_from_db()
    return getattr(mapping, field)


# ── Model ───────────────────────────────────────────────────────────────

@each_field
def test_the_field_is_blank_by_default_and_persists(f):
    assert getattr(row('steps'), f.name) == ''
    row('sleep', **{f.name: f.values[1]})
    assert getattr(SourceCodeConceptMapping.objects.get(source_code='sleep'), f.name) == f.values[1]


def test_the_allowed_lists_are_the_ones_agreed_and_fit_their_columns():
    assert WEARABLE_METRIC_CATEGORIES == (
        'Activity', 'Fitness', 'Heart & Cardiovascular', 'Blood & Respiratory',
        'Body Composition', 'Sleep', 'Mobility & Gait', 'Nutrition', 'Symptoms',
        'Reproductive Health', 'Mental Wellbeing', 'Self-care', 'Hearing',
        'Medication & Device Use', 'Safety', 'Self-reported', 'Stress & Autonomic',
        'Environmental', 'Location', 'Garmin-specific',
    )
    assert len(set(WEARABLE_METRIC_CATEGORIES)) == 20
    assert WEARABLE_MEASURE_CLASSES == (
        'Direct measurement', 'Aggregate over interval', 'Algorithmic estimate',
        'Vendor score', 'Environmental context', 'User-entered or device log',
    )
    for field, allowed in (('category', WEARABLE_METRIC_CATEGORIES),
                           ('measure_class', WEARABLE_MEASURE_CLASSES)):
        width = SourceCodeConceptMapping._meta.get_field(field).max_length
        assert max(len(value) for value in allowed) <= width, field


def test_the_metric_key_column_is_as_wide_as_the_rule():
    column = SourceCodeConceptMapping._meta.get_field('metric_key')
    assert column.max_length == METRIC_KEY_MAX_LENGTH == 64
    # No index until a query looks rows up by key: a CharField index is two
    # (btree and varchar_pattern_ops) over a column that is '' on nearly every row.
    assert not column.db_index


def test_metric_key_shape():
    for key in ('steps', 'resting_hr', 'vo2_max', 'a', 'forced_expiratory_volume_1',
                'a' * METRIC_KEY_MAX_LENGTH):
        assert clean_metric_key(key) == key
    assert clean_metric_key(None) == clean_metric_key('  ') == ''
    assert clean_metric_key(' steps ') == 'steps'
    for key in FIELDS[2].refused + ('steps count', 'steps\ncount', 'Steps'):
        with pytest.raises(ValueError, match='lowercase snake case'):
            clean_metric_key(key)


# ── Seeds: every seeded code has a category, a measure class and a key ──

def _seed_lists():
    """Everything the three seeds write, one entry per source vocabulary."""
    from omop_core.management.commands import (
        seed_hl7_phr_mappings as hl7, seed_openwearables_mappings as ow,
        seed_wearable_device_mappings as device,
    )
    phr = {vocabulary: lists for vocabulary, *lists in hl7.SEED_SETS}
    apple_metrics, apple_categories, apple_classes, apple_keys = phr['Apple']
    # Apple is seeded by two commands: the device seed's approved codes and the
    # HealthKit identifiers of the HL7 PHR table. They share no code.
    assert not set(device.APPLE_METRIC_KEYS) & set(apple_keys)
    return (
        ('OpenWearables', ow.OPENWEARABLES_METRICS, ow.OPENWEARABLES_CATEGORIES,
         ow.OPENWEARABLES_MEASURE_CLASSES, ow.OPENWEARABLES_METRIC_KEYS),
        ('HL7-PHR', *phr['HL7-PHR']),
        ('Apple', device.APPLE_METRICS + apple_metrics,
         {**device.APPLE_CATEGORIES, **apple_categories},
         {**device.APPLE_MEASURE_CLASSES, **apple_classes},
         {**device.APPLE_METRIC_KEYS, **apple_keys}),
        ('Garmin', device.GARMIN_METRICS, device.GARMIN_CATEGORIES,
         device.GARMIN_MEASURE_CLASSES, device.GARMIN_METRIC_KEYS),
        ('HealthConnect', *phr['HealthConnect']),
    )


def test_every_seeded_metric_has_an_allowed_category_measure_class_and_key():
    for label, metrics, categories, measure_classes, metric_keys in _seed_lists():
        codes = {metric[0] for metric in metrics}
        # No metric without a value, and no value for a code that is not seeded.
        assert set(categories) == codes, label
        assert set(measure_classes) == codes, label
        assert set(metric_keys) == codes, label
        # Every value a seed assigns is an allowed one. The allowed lists may
        # hold more: five categories are valid before any seeded metric uses them.
        assert set(categories.values()) <= set(WEARABLE_METRIC_CATEGORIES), label
        assert set(measure_classes.values()) <= set(WEARABLE_MEASURE_CLASSES), label
        for code, key in metric_keys.items():
            assert key and clean_metric_key(key) == key, (label, code)
        # One code per metric within a vocabulary.
        assert len(set(metric_keys.values())) == len(metric_keys), label


def test_codes_for_one_metric_share_its_key_category_and_measure_class():
    """A key names one row of the proposal, whose codes in every vocabulary
    carry that row's category and measure class."""
    by_key = defaultdict(list)
    for label, _metrics, categories, measure_classes, metric_keys in _seed_lists():
        for code, key in metric_keys.items():
            by_key[key].append((label, code, categories[code], measure_classes[code]))
    for key, members in by_key.items():
        assert len({(category, measure_class) for *_code, category, measure_class in members}) == 1, key

    def sources(key):
        return {(label, code) for label, code, *_described in by_key[key]}

    # The key is what ties the vocabularies together.
    assert sources('steps') == {
        ('OpenWearables', 'steps'), ('Apple', 'HKQuantityTypeIdentifierStepCount'),
        ('Garmin', 'steps'), ('HL7-PHR', 'stepCount'), ('HealthConnect', 'StepsRecord')}
    assert sources('resting_hr') == {
        ('OpenWearables', 'resting_heart_rate'),
        ('Apple', 'HKQuantityTypeIdentifierRestingHeartRate'), ('Garmin', 'resting_hr'),
        ('HL7-PHR', 'restingHeartRate'), ('HealthConnect', 'RestingHeartRateRecord')}
    assert sources('body_mass') == {
        ('OpenWearables', 'weight'), ('Apple', 'HKQuantityTypeIdentifierBodyMass'),
        ('HL7-PHR', 'bodyMass'), ('HealthConnect', 'WeightRecord')}
    assert sources('sleep_duration') == {
        ('Apple', 'HKCategoryTypeIdentifierSleepAnalysis'), ('Garmin', 'sleep_duration'),
        ('HL7-PHR', 'sleepAnalysis'), ('HealthConnect', 'SleepSessionRecord')}
    # A metric the proposal gives no key takes its OpenWearables code as the key
    # of every vocabulary's code for it.
    assert sources('heart_rate') == {
        ('OpenWearables', 'heart_rate'), ('Apple', 'HKQuantityTypeIdentifierHeartRate'),
        ('HL7-PHR', 'heartRate'), ('HealthConnect', 'HeartRateRecord')}
    assert sources('power') == {
        ('OpenWearables', 'power'), ('HL7-PHR', 'power'), ('HealthConnect', 'PowerRecord')}
    # HL7's one glucose code is blood glucose's; CGM glucose is OpenWearables' alone.
    assert ('HL7-PHR', 'bloodGlucose') in sources('blood_glucose')
    assert sources('cgm_glucose') == {('OpenWearables', 'cgm_glucose')}
    assert sources('rr_interval') == {('HL7-PHR', 'rrInterval')}
    assert sources('total_energy_burned') == {
        ('HL7-PHR', 'totalEnergyBurned'), ('HealthConnect', 'TotalCaloriesBurnedRecord')}
    # HL7's generic 'distance' is not the walking + running metric keyed 'distance'.
    assert ('HL7-PHR', 'distance') in sources('distance_other')
    assert ('HL7-PHR', 'distanceWalkingRunning') in sources('distance')


def test_the_hl7_seed_runs_over_its_own_lists():
    from omop_core.management.commands import (
        seed_hl7_phr_mappings as hl7, seed_openwearables_mappings as ow,
    )
    assert ow.Command.categories is ow.OPENWEARABLES_CATEGORIES
    assert ow.Command.measure_classes is ow.OPENWEARABLES_MEASURE_CLASSES
    assert ow.Command.metric_keys is ow.OPENWEARABLES_METRIC_KEYS
    assert ow.Command.origin_system == 'open-wearables-seed'
    assert not ow.Command.require_standard_target
    # One provenance for the three vocabularies it seeds, none of them
    # OpenWearables, and the first list of HL7 PHR codes unchanged within it.
    assert hl7.Command.origin_system == 'HL7 PHR'
    assert hl7.Command.require_standard_target
    assert [vocabulary for vocabulary, *_lists in hl7.SEED_SETS] == [
        'HL7-PHR', 'Apple', 'HealthConnect']
    _vocabulary, metrics, categories, measure_classes, metric_keys = hl7.SEED_SETS[0]
    assert metrics[:len(hl7.HL7_PHR_METRICS)] == hl7.HL7_PHR_METRICS
    for first, seeded in ((hl7.HL7_PHR_CATEGORIES, categories),
                          (hl7.HL7_PHR_MEASURE_CLASSES, measure_classes),
                          (hl7.HL7_PHR_METRIC_KEYS, metric_keys)):
        assert {code: seeded[code] for code in first} == first


# The proposal's categories that are not wearable metrics, and the two keys
# left out of its remaining categories.
NOT_WEARABLE_METRICS = {
    'Nutrition', 'Symptoms', 'Reproductive Health', 'Self-care', 'Mental Wellbeing', 'Hearing'}


def test_the_phr_source_table_is_one_row_per_wearable_metric():
    from omop_core.management.commands import (
        seed_hl7_phr_mappings as hl7, seed_openwearables_mappings as ow,
        seed_wearable_device_mappings as device,
    )
    from omop_core.services.source_vocabularies import DOMAIN_TO_TABLE
    rows = hl7.PHR_SOURCE_METRICS
    keys = [row[0] for row in rows]
    assert len(keys) == len(set(keys))
    assert set(hl7.PHR_SOURCE_CATEGORIES) == set(hl7.PHR_SOURCE_MEASURE_CLASSES) == set(keys)
    # Wearable metrics only.
    assert not set(hl7.PHR_SOURCE_CATEGORIES.values()) & NOT_WEARABLE_METRICS
    assert not {'food', 'medication_adherence'} & set(keys)
    for column, shape in ((6, r'[a-z][A-Za-z0-9]*'), (7, r'HK[A-Za-z0-9]+'),
                          (8, r'[A-Z][A-Za-z0-9]*Record')):
        codes = [row[column] for row in rows if row[column]]
        # One code names one metric, and is a single code value. Duplicate
        # detection on the tab compares codes case-insensitively.
        assert len(codes) == len({code.upper() for code in codes}), column
        for code in codes:
            assert re.fullmatch(shape, code), code
    for key, display, unit, domain, target_vocab, target_code, *codes in rows:
        assert clean_metric_key(key) == key
        assert display and any(codes), key
        assert domain in ('Measurement', 'Observation') and DOMAIN_TO_TABLE[domain], key
        assert bool(target_vocab) == bool(target_code), key
        # Standard UCUM only: a unit the seed cannot name in UCUM is not listed.
        assert not unit or unit in ow.UCUM_BY_UNIT, key
    # A code another list seeds belongs to the same metric there.
    for key, *_described, hl7_code, healthkit, _health_connect in rows:
        if hl7_code in hl7.HL7_PHR_METRIC_KEYS:
            assert hl7.HL7_PHR_METRIC_KEYS[hl7_code] == key
        if healthkit in device.APPLE_METRIC_KEYS:
            assert device.APPLE_METRIC_KEYS[healthkit] == key
    # All 17 of the device seed's HealthKit codes are in the table, and none of
    # them is seeded by this command.
    assert {row[7] for row in rows} >= set(device.APPLE_METRIC_KEYS)
    assert len(device.APPLE_METRIC_KEYS) == 17
    for vocabulary, metrics, *_maps in hl7.SEED_SETS:
        seeded = [metric[0].upper() for metric in metrics]
        if vocabulary == 'Apple':
            seeded += [code.upper() for code in device.APPLE_METRIC_KEYS]
        assert len(seeded) == len(set(seeded)), vocabulary
    counts = {vocabulary: len(metrics) for vocabulary, metrics, *_maps in hl7.SEED_SETS}
    assert counts == {'HL7-PHR': 122, 'Apple': 60, 'HealthConnect': 35}


def test_a_metric_has_one_target_domain_and_unit_in_every_vocabulary():
    """What the proposal says of a metric is said of each of its codes, so the
    seeds agree wherever two of them carry the same metric key."""
    from omop_core.management.commands import seed_hl7_phr_mappings as hl7
    by_key = defaultdict(set)
    for vocabulary, metrics, _categories, _classes, metric_keys in hl7.SEED_SETS:
        for code, _display, unit, domain, target_vocab, target_code in metrics:
            by_key[metric_keys[code]].add((unit, domain, target_vocab, target_code))
    assert {key: found for key, found in by_key.items() if len(found) > 1} == {}


def test_no_code_is_listed_under_two_values():
    """by_code would keep the last silently; the lists must not rely on that."""
    import ast
    import inspect
    from omop_core.management.commands import (
        seed_hl7_phr_mappings as hl7, seed_openwearables_mappings as ow,
        seed_wearable_device_mappings as device,
    )
    checked = 0
    for module in (ow, hl7, device):
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if (isinstance(node, ast.Call) and getattr(node.func, 'id', '') == 'by_code'
                    and node.args and isinstance(node.args[0], ast.Dict)):
                codes = [code for group in ast.literal_eval(node.args[0]).values()
                         for code in group]
                assert len(codes) == len(set(codes)), module.__name__
                checked += 1
    # A category and a measure class list for each of six metric lists.
    assert checked == 12


# ── API: read ───────────────────────────────────────────────────────────

@each_field
def test_list_detail_and_group_members_return_the_field(curator, f):
    mapped = row('sleep_duration', target_concept=ConceptFactory(), **{f.name: f.values[1]})
    unmapped = row('steps', source_code_description='Steps', **{f.name: f.values[0]})
    row('blank')

    rows = {r['source_code']: r for r in browse(curator).data['results']}
    assert rows['sleep_duration'][f.name] == f.values[1]  # the branch with a destination
    assert rows['steps'][f.name] == f.values[0]           # and the one without
    assert rows['blank'][f.name] == ''

    request = APIRequestFactory().get(f'/api/v1/code-mappings/{mapped.pk}/')
    force_authenticate(request, user=curator)
    assert code_mapping_detail(request, mapping_id=mapped.pk).data[f.name] == f.values[1]

    request = APIRequestFactory().get('/api/v1/code-mappings/group/', {
        'source': 'OpenWearables', 'label': unmapped.source_label_norm, 'section': 'Unmapped'})
    force_authenticate(request, user=curator)
    members = code_mapping_group(request).data['results']
    assert [(m['source_code'], m[f.name]) for m in members] == [('steps', f.values[0])]


def test_reference_carries_the_allowed_lists_and_their_vocabularies(curator):
    request = APIRequestFactory().get('/api/v1/code-mappings/reference/')
    force_authenticate(request, user=curator)
    data = code_mapping_reference(request).data
    assert data['wearable_metric_categories'] == list(WEARABLE_METRIC_CATEGORIES)
    assert data['wearable_measure_classes'] == list(WEARABLE_MEASURE_CLASSES)
    assert data['wearable_source_vocabularies'] == sorted(WEARABLE_SOURCE_VOCABULARIES)
    assert 'HL7-PHR' in data['wearable_source_vocabularies']
    assert 'HealthConnect' in data['wearable_source_vocabularies']


# ── API: write ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('category', [
    'Symptoms', 'Reproductive Health', 'Mental Wellbeing', 'Self-care', 'Hearing'])
def test_a_category_no_seed_uses_yet_is_still_a_valid_value(curator, category):
    assert category not in {
        value for _l, _m, categories, _mc, _k in _seed_lists() for value in categories.values()}
    mapping = row('steps')
    assert patch(curator, mapping, category=category).data['category'] == category


@each_field
def test_a_curator_sets_changes_and_clears_the_field(curator, f):
    mapping = row('steps')
    # As the dialog sends it: the field, and the tab the save came from.
    response = patch(curator, mapping, decision_surface='code2concept', **{f.name: f.values[0]})
    assert response.status_code == 200, response.data
    assert response.data[f.name] == f.values[0]
    mapping.refresh_from_db()
    assert getattr(mapping, f.name) == f.values[0]
    # A save of this field alone is a curator save like any other.
    assert mapping.updated_by_id == curator.pk
    assert mapping.decision_surface == 'code2concept'

    assert patch(curator, mapping, **{f.name: f' {f.values[2]} '}).data[f.name] == f.values[2]
    assert patch(curator, mapping, **{f.name: ''}).data[f.name] == ''
    patch(curator, mapping, **{f.name: f.values[1]})
    assert patch(curator, mapping, **{f.name: None}).data[f.name] == ''


@each_field
def test_a_save_without_the_key_leaves_the_field(curator, f):
    mapping = row('steps', **{f.name: f.values[0]})
    assert patch(curator, mapping, notes='Checked against the proposal.').status_code == 200
    assert stored(mapping, f.name) == f.values[0]


def test_the_three_fields_are_saved_independently(curator):
    mapping = row('steps', category='Activity', measure_class='Aggregate over interval',
                  metric_key='steps')

    def now():
        mapping.refresh_from_db()
        return mapping.category, mapping.measure_class, mapping.metric_key

    assert patch(curator, mapping, category='Fitness').status_code == 200
    assert now() == ('Fitness', 'Aggregate over interval', 'steps')
    assert patch(curator, mapping, measure_class='Vendor score').status_code == 200
    assert now() == ('Fitness', 'Vendor score', 'steps')
    assert patch(curator, mapping, metric_key='step_count').status_code == 200
    assert now() == ('Fitness', 'Vendor score', 'step_count')
    # A value of one list is not a value of the other.
    assert patch(curator, mapping, category='Vendor score').status_code == 400
    assert patch(curator, mapping, measure_class='Fitness').status_code == 400
    # One save may carry all three; one bad value refuses the whole save.
    response = patch(curator, mapping, category='Sleep', measure_class='Direct measurement',
                     metric_key='Not A Key')
    assert response.status_code == 400 and 'metric_key' in response.data
    assert now() == ('Fitness', 'Vendor score', 'step_count')


@each_field
def test_a_value_the_field_does_not_take_is_a_400(curator, f):
    mapping = row('steps', **{f.name: f.values[0]})
    for value in f.refused:
        response = patch(curator, mapping, **{f.name: value})
        assert response.status_code == 400, value
        assert f.message in str(response.data[f.name]), value
        assert stored(mapping, f.name) == f.values[0]


@each_field
def test_create_takes_the_field_and_refuses_a_bad_value(curator, f):
    concept = ConceptFactory()
    payload = {'source_vocabulary_id': 'OpenWearables', 'domain_id': 'Measurement',
               'destination_concept_id': concept.concept_id}
    response = post(curator, source_code='new_metric', **{f.name: f.values[2]}, **payload)
    assert response.status_code == 201, response.data
    assert response.data[f.name] == f.values[2]
    created = SourceCodeConceptMapping.objects.get(source_code='new_metric')
    assert getattr(created, f.name) == f.values[2]

    refused = post(curator, source_code='other_metric', **{f.name: f.refused[0]}, **payload)
    assert refused.status_code == 400
    assert not SourceCodeConceptMapping.objects.filter(source_code='other_metric').exists()


@each_field
def test_an_analyst_writes_the_field_and_a_doctor_only_reads_it(f):
    mapping = row('steps', **{f.name: f.values[0]})
    doctor = with_role('doctor')
    response = patch(doctor, mapping, **{f.name: f.values[1]})
    assert response.status_code == 403
    assert response.data['detail'] == 'Mapping curator access required.'
    assert stored(mapping, f.name) == f.values[0]
    # Reading is every professional's.
    assert browse(doctor).data['results'][0][f.name] == f.values[0]

    analyst = with_role('analyst')
    assert patch(analyst, mapping, **{f.name: f.values[1]}).status_code == 200
    assert stored(mapping, f.name) == f.values[1]


@each_field
def test_an_unauthenticated_request_cannot_write_the_field(f):
    mapping = row('steps', **{f.name: f.values[0]})
    request = APIRequestFactory().patch(
        f'/api/v1/code-mappings/{mapping.pk}/', {f.name: f.values[1]}, format='json')
    assert code_mapping_detail(request, mapping_id=mapping.pk).status_code in (401, 403)
    assert stored(mapping, f.name) == f.values[0]


def test_an_approved_mapping_stays_with_those_who_may_approve(curator):
    """The rule predates these fields: a non-approver changes nothing on an
    approved row, the three metric fields included."""
    mapping = row('steps', status='approved', target_concept=ConceptFactory(),
                  category='Activity', measure_class='Aggregate over interval', metric_key='steps')
    analyst = with_role('analyst')
    for payload in ({'category': 'Fitness'}, {'measure_class': 'Vendor score'},
                    {'metric_key': 'step_count'}):
        response = patch(analyst, mapping, **payload)
        assert response.status_code == 400, payload
        assert 'Only org admins and staff' in str(response.data['status'])
    mapping.refresh_from_db()
    assert (mapping.category, mapping.measure_class, mapping.metric_key, mapping.status) == (
        'Activity', 'Aggregate over interval', 'steps', 'approved')

    for approver in (curator, with_role('org_admin')):
        response = patch(approver, mapping, category='Fitness', status='approved')
        assert response.status_code == 200, response.data
    assert patch(curator, mapping, metric_key='step_count').status_code == 200
    mapping.refresh_from_db()
    assert (mapping.category, mapping.metric_key, mapping.status) == (
        'Fitness', 'step_count', 'approved')


# ── Blank means "not yet described" ─────────────────────────────────────

def test_a_cleared_field_is_refilled_by_the_seed_and_a_changed_one_is_kept(curator):
    """Blank is not a curator's answer the seed respects: it means the metric
    has not been described yet, and the next fill describes it again. A value
    a curator changed is theirs and is never replaced."""
    from django.core.management import call_command
    call_command('seed_openwearables_mappings', verbosity=0)
    cleared = SourceCodeConceptMapping.objects.get(
        source_vocabulary_id='OpenWearables', source_code='resting_heart_rate')
    changed = SourceCodeConceptMapping.objects.get(
        source_vocabulary_id='OpenWearables', source_code='steps')
    assert patch(curator, cleared, category='', measure_class='', metric_key='').status_code == 200
    assert patch(curator, changed, category='Fitness', measure_class='Vendor score',
                 metric_key='step_count').status_code == 200
    cleared.refresh_from_db()
    assert (cleared.category, cleared.measure_class, cleared.metric_key) == ('', '', '')

    # A plain re-run leaves both alone; --update-seeded fills only the blanks.
    call_command('seed_openwearables_mappings', verbosity=0)
    cleared.refresh_from_db()
    assert (cleared.category, cleared.measure_class, cleared.metric_key) == ('', '', '')
    call_command('seed_openwearables_mappings', '--update-seeded', verbosity=0)
    cleared.refresh_from_db()
    changed.refresh_from_db()
    assert (cleared.category, cleared.measure_class, cleared.metric_key) == (
        'Heart & Cardiovascular', 'Aggregate over interval', 'resting_hr')
    assert (changed.category, changed.measure_class, changed.metric_key) == (
        'Fitness', 'Vendor score', 'step_count')


# ── Sort ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(('field', 'values'), [
    ('category', ('Activity', 'Nutrition', 'Sleep')),
    ('metric_key', ('active_minutes', 'resting_hr', 'steps')),
])
def test_the_flat_list_sorts_by_category_and_by_metric_key(curator, field, values):
    row('b', **{field: values[2]})
    row('a', **{field: values[0]})
    row('c', **{field: values[1]})
    row('d')

    def order(value):
        data = browse(curator, order_0=value).data
        return [r['source_code'] for r in data['results']]

    # Blank sorts first ascending, as an empty string does.
    assert order(field) == ['d', 'a', 'c', 'b']
    assert order(f'-{field}') == ['b', 'c', 'a', 'd']


def test_measure_class_has_no_column_and_is_not_a_sort(curator):
    row('a', measure_class='Vendor score')
    assert browse(curator, order_0='measure_class').status_code == 400


@each_field
def test_grouped_entries_name_a_value_only_when_every_member_agrees(curator, f):
    row('A', source_code_description='Sleep duration', **{f.name: f.values[1]})
    row('B', source_code_description='SLEEP DURATION', **{f.name: f.values[1]})
    row('C', source_code_description='Steps', **{f.name: f.values[0]})
    row('D', source_code_description='steps')              # blank against a value
    row('E', source_code_description='Heart rate', **{f.name: f.values[2]})
    row('F', source_code_description='heart rate', **{f.name: f.values[1]})
    row('G', source_code_description='Hydration')

    found = group_entries(SourceCodeConceptMapping.objects.all(), 1, 100,
                          order='source_code_description')
    assert {entry['label']: entry[f.name] for entry in found} == {
        'sleepduration': f.values[1], 'steps': '', 'heartrate': '', 'hydration': '',
    }
    rolled = browse(curator, rollup='1').data['groups']['Unmapped']
    assert {entry['label']: entry[f.name] for entry in rolled}['sleepduration'] == f.values[1]


def test_grouped_rows_of_another_tab_carry_the_three_keys_blank(curator):
    for code in ('E11.9', 'E11.8'):
        row(code, source_vocabulary_id='ICD10CM', source_code_description='Type 2 diabetes',
            domain_id='Condition', omop_table='condition_occurrence')
    row('I10', source_vocabulary_id='ICD10CM', source_code_description='Hypertension',
        domain_id='Condition', omop_table='condition_occurrence')
    groups = browse(curator, source='ICD10CM', rollup='1').data['groups']['Unmapped']
    assert sorted(entry['members'] for entry in groups) == [1, 2]
    for entry in groups:
        assert (entry['category'], entry['measure_class'], entry['metric_key']) == ('', '', '')
    # And it may still be sorted by them, as every tab's endpoint accepts.
    assert browse(curator, source='ICD10CM', rollup='1', order_0='category').status_code == 200


def test_the_grouped_page_agrees_without_counting_distinct_values(curator):
    """COUNT(DISTINCT) on the three columns cost every tab's grouped page a
    quarter more; the members agree exactly when Min equals Max."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    row('A', source_code_description='Steps', category='Activity')
    row('B', source_code_description='steps', category='Activity')
    with CaptureQueriesContext(connection) as queries:
        found = group_entries(SourceCodeConceptMapping.objects.all(), 1, 100)
    assert found[0]['category'] == 'Activity'
    [aggregate] = [q['sql'] for q in queries if 'GROUP BY' in q['sql']]
    for column in ('category', 'measure_class', 'metric_key'):
        assert f'COUNT(DISTINCT "source_code_concept_mapping"."{column}")' not in aggregate
        assert f'MIN("source_code_concept_mapping"."{column}")' in aggregate


@pytest.mark.parametrize(('field', 'values'), [
    ('category', ('Activity', 'Nutrition', 'Sleep')),
    ('metric_key', ('active_minutes', 'resting_hr', 'steps')),
])
def test_grouped_entries_sort_by_their_unanimous_value(curator, field, values):
    row('A', source_code_description='Sleep duration', **{field: values[2]})
    row('B', source_code_description='SLEEP DURATION', **{field: values[2]})
    row('C', source_code_description='Steps', **{field: values[0]})
    row('D', source_code_description='steps')
    row('E', source_code_description='Hydration', **{field: values[1]})

    def order(value):
        groups = browse(curator, rollup='1', order_0=value).data['groups']['Unmapped']
        return [(entry['label'], entry[field]) for entry in groups]

    # The mixed group sorts as the blank it shows.
    assert order(field) == [
        ('steps', ''), ('hydration', values[1]), ('sleepduration', values[2])]
    assert order(f'-{field}') == [
        ('sleepduration', values[2]), ('hydration', values[1]), ('steps', '')]


# ── copy_reference_data ─────────────────────────────────────────────────

@each_field
def test_the_field_travels_with_a_copied_mapping(f):
    from omop_core.services.field_curation_transfer import apply_payload, read_payload

    mapping = row('steps', **{f.name: f.values[0]})
    payload = read_payload('default')
    [entry] = [e for e in payload['code_mappings'] if e['source_code'] == 'steps']
    assert entry[f.name] == f.values[0]

    # Overwrites what the target holds ...
    SourceCodeConceptMapping.objects.filter(pk=mapping.pk).update(**{f.name: f.values[1]})
    apply_payload(payload)
    assert stored(mapping, f.name) == f.values[0]
    # ... including with a blank the source curated ...
    entry[f.name] = ''
    apply_payload(payload)
    assert stored(mapping, f.name) == ''
    # ... and arrives on a row the copy creates.
    entry[f.name] = f.values[2]
    SourceCodeConceptMapping.objects.all().delete()
    apply_payload(payload)
    copied = SourceCodeConceptMapping.objects.get(source_code='steps')
    assert getattr(copied, f.name) == f.values[2]


@each_field
def test_a_payload_written_before_the_column_leaves_the_field_alone(f):
    from omop_core.services.field_curation_transfer import apply_payload, read_payload

    mapping = row('steps', notes='source note', **{f.name: f.values[0]})
    payload = read_payload('default')
    [entry] = [e for e in payload['code_mappings'] if e['source_code'] == 'steps']
    del entry[f.name]

    SourceCodeConceptMapping.objects.filter(pk=mapping.pk).update(
        notes='', **{f.name: f.values[1]})
    apply_payload(payload)
    mapping.refresh_from_db()
    # The rest of the row is still overwritten from the payload.
    assert (getattr(mapping, f.name), mapping.notes) == (f.values[1], 'source note')

    SourceCodeConceptMapping.objects.all().delete()
    apply_payload(payload)
    assert getattr(SourceCodeConceptMapping.objects.get(source_code='steps'), f.name) == ''
