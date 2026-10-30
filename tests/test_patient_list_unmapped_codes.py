"""Unmapped source entries are counted before patient-list pagination."""
import pytest
from rest_framework.test import APIClient

from omop_core.models import PatientRecord, PatientSourceCode, SourceCodeConceptMapping
from omop_core.services.patient_list_context import annotate_context, order_context
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.api.serializers import PatientListSerializer
from patient_portal.models import Identity
from tests.factories import ConceptFactory, MeasurementFactory, ObservationFactory, OrganizationFactory, PatientRecordFactory

pytestmark = pytest.mark.django_db


def source(record, code, vocabulary='LOINC', table='measurement', **kwargs):
    return PatientSourceCode.objects.create(person=record.person, source_value=code,
        source_vocabulary_id=vocabulary, omop_table=table, **kwargs)


def mapping(code, target=None, vocabulary='LOINC', table='measurement', **kwargs):
    return SourceCodeConceptMapping.objects.create(source_code=code, target_concept=target,
        source_vocabulary_id=vocabulary, omop_table=table, **kwargs)


def count(record):
    return annotate_context(PatientRecord.objects.filter(pk=record.pk)).get().unmapped_source_code_count


def test_inventory_counts_entries_not_occurrences_and_live_mapping_destinations():
    record = PatientRecordFactory()
    source(record, 'missing', occurrence_count=100)
    source(record, 'no-destination')
    source(record, 'proposed')
    source(record, 'approved')
    mapping('no-destination')
    mapping('proposed', ConceptFactory(), status='proposed')
    mapping('approved', ConceptFactory(), status='approved')
    assert count(record) == 2
    row = SourceCodeConceptMapping.objects.get(source_code='no-destination')
    row.target_concept = ConceptFactory()
    row.save()
    assert count(record) == 1
    row.target_concept = None
    row.save()
    assert count(record) == 2


def test_vocabulary_table_alias_and_hospital_are_part_of_the_mapping_key():
    org = OrganizationFactory()
    record = PatientRecordFactory(organization=org)
    target = ConceptFactory()
    source(record, 'same', vocabulary='A')
    source(record, 'same', vocabulary='B')
    source(record, 'same', vocabulary='A', table='observation')
    mapping('same', target, vocabulary='A')
    source(record, 'alias', vocabulary='urn:oid:2.16.840.1.113883.6.96')
    mapping('alias', target, vocabulary='SNOMED')
    source(record, 'local', vocabulary='EPIC')
    mapping('local', target, vocabulary='EPIC', organization=OrganizationFactory())
    assert count(record) == 3
    mapping('local', target, vocabulary='EPIC', organization=org)
    assert count(record) == 2


def test_inventory_is_authoritative_and_legacy_fallback_deduplicates():
    inventory = PatientRecordFactory()
    legacy = PatientRecordFactory()
    source(inventory, 'mapped')
    mapping('mapped', ConceptFactory())
    with suppress_patient_record_refresh():
        for record in (inventory, legacy):
            for _ in range(3):
                MeasurementFactory(person=record.person, measurement_source_value='unmapped')
            ObservationFactory(person=record.person, observation_source_value='unmapped')
            MeasurementFactory(person=record.person, measurement_source_value='mapped')
            MeasurementFactory(person=record.person, measurement_source_value='')
    assert count(inventory) == 0
    assert count(legacy) == 2
    assert count(PatientRecordFactory()) == 0


def test_count_serialization_is_one_query_independent_of_patient_count(django_assert_num_queries):
    for total in (0, 2, 5):
        record = PatientRecordFactory()
        for i in range(total):
            source(record, str(i))
    with django_assert_num_queries(1):
        data = PatientListSerializer(annotate_context(PatientRecord.objects.select_related(
            'person', 'organization')).order_by('person_id'), many=True).data
    assert [row['unmapped_source_code_count'] for row in data] == [0, 2, 5]


def test_server_sort_is_numeric_stable_and_precedes_pagination():
    low = PatientRecordFactory()
    high = PatientRecordFactory()
    zero = PatientRecordFactory()
    for record, total in ((low, 2), (high, 11)):
        for i in range(total):
            source(record, str(i))
    actor = Identity.objects.create_user(email='unmapped-list@example.test', is_staff=True)
    client = APIClient()
    client.force_authenticate(actor)
    for ordering, first in (('unmapped_source_codes', zero), ('-unmapped_source_codes', high)):
        response = client.get('/api/v1/patient-records/', {'page': 1, 'page_size': 1, 'ordering': ordering})
        assert response.status_code == 200, response.data
        assert response.data['count'] == 3
        assert response.data['results'][0]['person_id'] == first.person_id
    assert list(order_context(annotate_context(PatientRecord.objects.all()),
        'unmapped_source_codes').values_list('pk', flat=True)) == [zero.pk, low.pk, high.pk]


def test_sort_keeps_organization_patient_scope():
    from omop_core.models import GroupAccess
    org = OrganizationFactory()
    visible = PatientRecordFactory(organization=org)
    hidden = PatientRecordFactory(organization=OrganizationFactory())
    source(visible, 'visible')
    for i in range(4):
        source(hidden, f'hidden-{i}')
    actor = Identity.objects.create_user(email='unmapped-org@example.test')
    GroupAccess.objects.create(identity=actor, org=org, role='org_admin')
    client = APIClient()
    client.force_authenticate(actor)
    response = client.get('/api/v1/patient-records/', {'page': 1, 'ordering': '-unmapped_source_codes'})
    assert response.status_code == 200, response.data
    assert response.data['count'] == 1
    assert [(row['person_id'], row['unmapped_source_code_count']) for row in response.data['results']] == [(visible.person_id, 1)]
