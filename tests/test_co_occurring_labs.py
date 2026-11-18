"""Other results in the same panel or order as a source code (#2702)."""
import pytest
from django.db import OperationalError, connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from omop_core.models import (
    GroupAccess, LoincClass, LoincCodeClass, PatientSourceCode, SourceCodeConceptMapping,
)
from omop_core.services import co_occurring_labs
from patient_portal.models import Identity
from tests.factories import (
    ConceptFactory, OrganizationFactory, PatientRecordFactory, PersonFactory, VocabularyFactory,
)

pytestmark = pytest.mark.django_db

EPIC = 'urn:oid:1.2.840.114350.1.13.0.2.7.2.768282'
LOINC_SYSTEM = 'http://loinc.org'
SPEP = {'reference': 'ServiceRequest/spep-1', 'display': 'PROTEIN ELECTROPHORESIS SERUM'}


def url(mapping):
    return f'/api/v1/code-mappings/{mapping.pk}/co-occurring/'


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def staff():
    return client_for(Identity.objects.create_user(email='curator@example.test', is_staff=True))


def with_role(role):
    user = Identity.objects.create_user(email=f'{role}@example.test')
    GroupAccess.objects.create(identity=user, org=OrganizationFactory(), role=role)
    return user


def mapping(code, vocabulary=EPIC, **kwargs):
    return SourceCodeConceptMapping.objects.create(
        source_code=code, source_vocabulary_id=vocabulary, omop_table='measurement',
        **{'status': 'proposed', **kwargs})


def observation(resource_id, codings, *, based_on=None, text='', **extra):
    """An Observation as the ETL stores it. *codings* are (system, code, display)."""
    resource = {
        'resourceType': 'Observation', 'id': resource_id,
        'code': {'coding': [{'system': s, 'code': c, 'display': d} for s, c, d in codings]},
        **extra,
    }
    if text:
        resource['code']['text'] = text
    if based_on is not None:
        resource['basedOn'] = based_on
    return {'fhir': resource}


def record(person, code, metadata, vocabulary=EPIC, table='measurement', unit=''):
    return PatientSourceCode.objects.create(
        person=person, source_value=code, source_vocabulary_id=vocabulary, omop_table=table,
        source_unit=unit, source_metadata=metadata)


def loinc(code, system, method=''):
    LoincClass.objects.get_or_create(code='CHEM', defaults={'display_name': 'Chemistry'})
    LoincCodeClass.objects.create(
        loinc_num=code, loinc_class_id='CHEM', component=f'Component {code}', property='MCnc',
        time_aspect='Pt', system=system, scale_type='Qn', method_type=method)
    return ConceptFactory(concept_code=code, vocabulary=VocabularyFactory(vocabulary_id='LOINC'))


def codes(payload):
    return {row['source_code']: row for row in payload['codes']}


def test_results_in_the_same_order_are_listed_with_what_the_source_says_of_them(staff):
    opened = mapping('MSQ1')
    person, other = PersonFactory(), PersonFactory()
    record(person, 'MSQ1', observation('o-1', [(EPIC, 'MSQ1', 'M-SPIKE')], based_on=[SPEP]), unit='g/dL')
    record(person, 'ALBE', observation('o-2', [(EPIC, 'ALBE', 'ALBUMIN')], based_on=[SPEP],
                                       text='Albumin, electrophoresis'), unit='g/dL')
    # Another order of the same patient, and the same reference on another
    # patient: a reference means something within one patient only.
    record(person, 'GLU', observation('o-3', [(EPIC, 'GLU', 'GLUCOSE')],
                                      based_on=[{'reference': 'ServiceRequest/bmp-1'}]))
    record(other, 'NA', observation('o-4', [(EPIC, 'NA', 'SODIUM')], based_on=[SPEP]))

    response = staff.get(url(opened))

    assert response.status_code == 200
    data = response.json()
    assert (data['records'], data['records_read'], data['records_with_order'], data['orders']) == (1, 1, 1, 1)
    assert data['order_names'] == [{'name': 'PROTEIN ELECTROPHORESIS SERUM', 'count': 1}]
    assert data['timed_out'] is False and data['codes_total'] == 1
    assert data['codes'] == [{
        'source_code': 'ALBE', 'source_vocabulary_id': EPIC, 'omop_table': 'measurement',
        'description': 'Albumin, electrophoresis', 'units': ['g/dL'],
        'system': '', 'method': '', 'axes_basis': '', 'axes_loinc': '',
        'mapping_id': None, 'mapping_status': 'unmapped', 'orders': 1,
    }]


def test_coverage_counts_records_orders_and_order_names(staff):
    opened = mapping('MSQ1')
    for index, name in enumerate(['SPEP', 'SPEP', 'Monoclonal protein study', None]):
        person = PersonFactory()
        based_on = None if name is None else [{'reference': f'ServiceRequest/{index}', 'display': name}]
        record(person, 'MSQ1', observation(f'm-{index}', [(EPIC, 'MSQ1', 'M-SPIKE')], based_on=based_on))
    # A display alone names an order but cannot join two records.
    record(PersonFactory(), 'MSQ1', observation('m-9', [(EPIC, 'MSQ1', 'M-SPIKE')],
                                                based_on=[{'display': 'SPEP'}]))

    data = staff.get(url(opened)).json()

    assert (data['records'], data['records_with_order'], data['orders']) == (5, 3, 3)
    assert data['order_names'] == [
        {'name': 'SPEP', 'count': 2}, {'name': 'Monoclonal protein study', 'count': 1}]
    assert data['order_names_total'] == 2 and data['codes'] == []


def test_no_order_links_is_an_empty_answer_not_an_error(staff):
    opened = mapping('MSQ1')
    record(PersonFactory(), 'MSQ1', observation('o-1', [(EPIC, 'MSQ1', 'M-SPIKE')]))
    for index, metadata in enumerate([{}, {'fhir': None}, {'fhir': {'basedOn': 'bad'}},
                                      {'basedOn': [None, 'bad', {}, {'reference': ''}, {'reference': 7}]}]):
        record(PersonFactory(), 'MSQ1', metadata)

    data = staff.get(url(opened)).json()
    never_seen = staff.get(url(mapping('NEVER-SEEN'))).json()

    assert (data['records'], data['records_with_order'], data['orders'], data['codes']) == (5, 0, 0, [])
    assert (never_seen['records'], never_seen['codes'], never_seen['timed_out']) == (0, [], False)


def test_alternate_codings_of_one_observation_are_one_result(staff):
    """The opened record's own LOINC is not a co-occurring lab, and a result
    carrying a local code and a LOINC is listed once, under the local code."""
    opened = mapping('MSQ1')
    loinc('2871-2', 'Ser/Plas', 'Electrophoresis')
    person = PersonFactory()
    opened_resource = observation('o-1', [(EPIC, 'MSQ1', 'M-SPIKE'), (LOINC_SYSTEM, '33358-3', 'M protein')],
                                  based_on=[SPEP])
    record(person, 'MSQ1', opened_resource)
    record(person, '33358-3', opened_resource, vocabulary='LOINC')
    beta = observation('o-2', [(LOINC_SYSTEM, '2871-2', 'Beta globulin'), (EPIC, 'BGE', 'BETA GLOBULIN')],
                       based_on=[SPEP])
    record(person, 'BGE', beta, unit='g/dL')
    record(person, '2871-2', beta, vocabulary='LOINC', unit='g/dL')

    found = codes(staff.get(url(opened)).json())

    assert list(found) == ['BGE']
    # Unmapped, so the LOINC the hospital sent is the only word on its axes.
    assert (found['BGE']['system'], found['BGE']['method']) == ('Ser/Plas', 'Electrophoresis')
    assert (found['BGE']['axes_basis'], found['BGE']['axes_loinc']) == ('hospital', '2871-2')


def test_alternate_codings_are_recognised_without_resource_ids(staff):
    opened = mapping('MSQ1')
    person = PersonFactory()
    resource = {'resourceType': 'Observation', 'basedOn': [SPEP], 'code': {'coding': [
        {'system': EPIC, 'code': 'MSQ1'}, {'system': LOINC_SYSTEM, 'code': '33358-3'}]}}
    record(person, 'MSQ1', resource)
    record(person, '33358-3', resource, vocabulary='LOINC')
    record(person, 'ALBE', {'resourceType': 'Observation', 'basedOn': [SPEP],
                            'code': {'coding': [{'system': EPIC, 'code': 'ALBE'}]}})

    assert list(codes(staff.get(url(opened)).json())) == ['ALBE']


def test_system_and_method_come_from_the_mapping_before_the_hospitals_loinc(staff):
    opened = mapping('MSQ1')
    albumin = loinc('2862-1', 'Ser/Plas', 'Electrophoresis')
    loinc('1751-7', 'Ser/Plas')
    rejected = loinc('2885-2', 'Ser/Plas')
    approved = mapping('ALBE', target_concept=albumin, status='approved',
                       source_code_description='Albumin, electrophoresis')
    proposed = mapping('A1GE', target_concept=albumin)
    queued = mapping('GGE')
    refused = mapping('TP', target_concept=rejected, status='rejected')
    person = PersonFactory()
    record(person, 'MSQ1', observation('o-1', [(EPIC, 'MSQ1', 'M-SPIKE')], based_on=[SPEP]))
    # The hospital's own LOINC for albumin disagrees; the mapping decides.
    record(person, 'ALBE', observation('o-2', [(EPIC, 'ALBE', 'ALB'), (LOINC_SYSTEM, '1751-7', 'Albumin')],
                                       based_on=[SPEP]))
    for index, code in enumerate(['A1GE', 'GGE', 'TP', 'SPEPI']):
        record(person, code, observation(f'x-{index}', [(EPIC, code, code)], based_on=[SPEP]))

    found = codes(staff.get(url(opened)).json())

    assert {code: (row['mapping_status'], row['mapping_id'], row['axes_basis'], row['method'])
            for code, row in found.items()} == {
        'ALBE': ('approved', approved.pk, 'mapping', 'Electrophoresis'),
        'A1GE': ('proposed', proposed.pk, 'mapping', 'Electrophoresis'),
        # A queue row with no destination is unmapped, and still has a row to open.
        'GGE': ('unmapped', queued.pk, '', ''),
        'TP': ('rejected', refused.pk, '', ''),
        'SPEPI': ('unmapped', None, '', ''),
    }
    assert found['ALBE']['description'] == 'Albumin, electrophoresis'
    assert found['ALBE']['axes_loinc'] == '2862-1'


def test_a_record_with_no_order_is_joined_through_its_diagnostic_report(staff):
    opened = mapping('MSQ1')
    person = PersonFactory()
    record(person, 'MSQ1', observation('o-1', [(EPIC, 'MSQ1', 'M-SPIKE')]))
    record(person, 'ALBE', observation('o-2', [(EPIC, 'ALBE', 'ALBUMIN')]), unit='g/dL')
    record(person, 'GLU', observation('o-3', [(EPIC, 'GLU', 'GLUCOSE')]))
    record(person, 'SPEP', {'fhir': {
        'resourceType': 'DiagnosticReport', 'id': 'r-1',
        'code': {'text': 'Protein electrophoresis, serum'},
        'result': [{'reference': 'Observation/o-1'}, {'reference': 'Observation/o-2'}],
    }}, table='')

    data = staff.get(url(opened)).json()

    assert (data['records_with_order'], data['orders']) == (1, 1)
    assert data['order_names'] == [{'name': 'Protein electrophoresis, serum', 'count': 1}]
    assert list(codes(data)) == ['ALBE']


def test_a_code_seen_in_several_orders_counts_each_and_sorts_first(staff):
    opened = mapping('MSQ1')
    for index in range(3):
        person = PersonFactory()
        order = [{'reference': f'ServiceRequest/{index}'}]
        record(person, 'MSQ1', observation(f'm-{index}', [(EPIC, 'MSQ1', 'M-SPIKE')], based_on=order))
        record(person, 'ALBE', observation(f'a-{index}', [(EPIC, 'ALBE', 'ALBUMIN')], based_on=order),
               unit='g/dL' if index else 'gm/dL')
        if index == 0:
            record(person, 'IGA', observation('i-0', [(EPIC, 'IGA', 'IGA')], based_on=order))

    data = staff.get(url(opened)).json()

    assert [(row['source_code'], row['orders']) for row in data['codes']] == [('ALBE', 3), ('IGA', 1)]
    assert data['codes'][0]['units'] == ['g/dL', 'gm/dL']
    assert data['orders'] == 3 and data['order_names'] == []


def test_a_hospitals_mapping_reads_only_that_hospitals_patients(staff):
    hospital, elsewhere = OrganizationFactory(), OrganizationFactory()
    opened = mapping('MSQ1', organization=hospital)
    for organization, code in ((hospital, 'ALBE'), (elsewhere, 'GLU')):
        person = PatientRecordFactory(organization=organization).person
        record(person, 'MSQ1', observation(f'm-{code}', [(EPIC, 'MSQ1', 'M-SPIKE')], based_on=[SPEP]))
        record(person, code, observation(f'x-{code}', [(EPIC, code, code)], based_on=[SPEP]))

    data = staff.get(url(opened)).json()

    assert data['records'] == 1 and list(codes(data)) == ['ALBE']


def test_only_the_bounded_sample_of_records_is_walked(staff, monkeypatch):
    monkeypatch.setattr(co_occurring_labs, 'MAX_RECORDS', 2)
    monkeypatch.setattr(co_occurring_labs, 'MAX_CODES', 1)
    opened = mapping('MSQ1')
    # Records naming an order are read first, whatever the patient's id.
    record(PersonFactory(), 'MSQ1', observation('m-none', [(EPIC, 'MSQ1', 'M-SPIKE')]))
    for index in range(3):
        person = PersonFactory()
        order = [{'reference': f'ServiceRequest/{index}'}]
        record(person, 'MSQ1', observation(f'm-{index}', [(EPIC, 'MSQ1', 'M-SPIKE')], based_on=order))
        record(person, 'ALBE', observation(f'a-{index}', [(EPIC, 'ALBE', 'ALBUMIN')], based_on=order))
        record(person, f'ONCE-{index}', observation(f'b-{index}', [(EPIC, f'ONCE-{index}', 'X')], based_on=order))

    data = staff.get(url(opened)).json()

    assert (data['records'], data['records_read'], data['records_with_order'], data['orders']) == (4, 2, 3, 2)
    assert [row['source_code'] for row in data['codes']] == ['ALBE'] and data['codes_total'] == 3


def test_the_walk_costs_the_same_number_of_queries_for_more_patients(staff):
    counts = []
    for patients, code in ((2, 'FEW'), (8, 'MANY')):
        opened = mapping(code)
        for index in range(patients):
            person = PersonFactory()
            order = [{'reference': f'ServiceRequest/{index}'}]
            record(person, code, observation(f'm-{index}', [(EPIC, code, 'M-SPIKE')], based_on=order))
            other = f'{code}-{index}'
            mapping(other)
            record(person, other, observation(f'c-{index}', [(EPIC, other, 'X')], based_on=order))
        with CaptureQueriesContext(connection) as queries:
            assert staff.get(url(opened)).json()['codes_total'] == patients
        counts.append(len(queries))

    assert counts[0] == counts[1]


def test_a_walk_that_runs_out_of_time_says_so(staff, monkeypatch):
    def too_slow(*args):
        raise OperationalError('canceling statement due to statement timeout')
    monkeypatch.setattr(co_occurring_labs, '_collect', too_slow)

    data = staff.get(url(mapping('MSQ1'))).json()

    assert data['timed_out'] is True and data['codes'] == [] and data['records'] == 0
    # The request's own transaction is still usable afterwards.
    assert SourceCodeConceptMapping.objects.filter(source_code='MSQ1').exists()


@pytest.mark.parametrize('role', ['org_admin', 'doctor', 'analyst'])
def test_every_mapping_reader_sees_it(role):
    assert client_for(with_role(role)).get(url(mapping('MSQ1'))).status_code == 200


def test_it_is_not_for_patients_machines_or_the_signed_out(settings):
    opened = mapping('MSQ1')
    secret = 'co-occurring-service-secret-0000000000000'
    settings.SERVICE_AUTH_TOKENS = {'dbt': {'token': secret, 'scopes': 'system/*.read'}}
    settings.SERVICE_AUTH_TOKEN = ''
    machine = APIClient()
    machine.credentials(HTTP_AUTHORIZATION=f'Bearer {secret}')

    assert machine.get(f'/api/v1/code-mappings/{opened.pk}/').status_code == 200, 'the token works'
    assert machine.get(url(opened)).status_code == 403
    assert client_for(with_role('patient')).get(url(opened)).status_code == 403
    assert APIClient().get(url(opened)).status_code in (401, 403)


def test_an_unknown_mapping_is_not_found_and_nothing_is_written(staff):
    assert staff.get('/api/v1/code-mappings/999999/co-occurring/').status_code == 404
    assert staff.post(url(mapping('MSQ1'))).status_code == 405
