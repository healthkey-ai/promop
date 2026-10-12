from datetime import date

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from rest_framework.test import APIClient

from omop_core.models import (
    ConceptRelationship,
    LoincClass,
    LoincCodeClass,
    ProvenanceRecord,
    Relationship,
    SourceCodeConceptMapping,
)
from omop_core.services.mapping_unit_consistency import (
    unit_consistency_for_mapping,
    unit_consistency_for_mappings,
)
from patient_portal.models import Identity
from tests.factories import ConceptFactory, MeasurementFactory, OrganizationFactory

pytestmark = pytest.mark.django_db


def axis(source, relationship_id, name, code):
    Relationship.objects.get_or_create(
        relationship_id=relationship_id,
        defaults={
            'relationship_name': relationship_id,
            'is_hierarchical': 0,
            'defines_ancestry': 0,
            'reverse_relationship_id': '',
            'relationship_concept_id': 0,
        },
    )
    target = ConceptFactory(concept_name=name, concept_code=code)
    ConceptRelationship.objects.create(
        concept_1=source, concept_2=target, relationship_id=relationship_id,
        valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
    )


def loinc_destination(*, property_name='Mass Concentration', scale='Quantitative'):
    concept = ConceptFactory(
        concept_code='1751-7', concept_name='Albumin [Mass/volume] in Serum or Plasma',
    )
    axis(concept, 'Has property', property_name, 'LP-PROPERTY')
    axis(concept, 'Has scale type', scale, 'LP-SCALE')
    loinc_class, _ = LoincClass.objects.get_or_create(
        code='CHEM', defaults={'display_name': 'Chemistry'},
    )
    LoincCodeClass.objects.update_or_create(
        loinc_num=concept.concept_code,
        defaults={'loinc_class': loinc_class, 'example_units': 'g/dL'},
    )
    return concept


def mapping(destination, **kwargs):
    values = {
        'source_vocabulary_id': 'EPIC', 'source_code': 'ALB-1',
        'source_code_description': 'Albumin', 'domain_id': 'Measurement',
        'omop_table': 'measurement', 'target_concept': destination,
        'destination_vocabulary_id': 'LOINC', 'status': 'proposed', 'origin': 'import',
    }
    values.update(kwargs)
    return SourceCodeConceptMapping.objects.create(**values)


def measurement(destination, unit, **kwargs):
    return MeasurementFactory(
        measurement_concept=destination, measurement_source_value='ALB-1',
        unit_source_value=unit, value_as_number=4.2, **kwargs,
    )


def test_convertible_source_units_are_consistent():
    destination = loinc_destination()
    row = mapping(destination)
    measurement(destination, 'mg/dL')
    measurement(destination, 'mmol/L', is_erroneous=True)

    report = unit_consistency_for_mapping(row, destination)

    assert report['property'] == 'MCnc'
    assert report['warning'] is False
    assert report['observed_units'] == [
        {'unit': 'mg/dL', 'count': 1, 'compatible': True},
    ]


def test_imported_unit_evidence_is_used_without_double_counting_live_rows():
    destination = loinc_destination()
    row = mapping(destination, source_unit_evidence=[{
        'display': 'g/dL', 'code': 'g/dL', 'count': 20,
        'source': 'healthtree-unmapped-v2',
    }])
    measurement(destination, 'g/dL')

    report = unit_consistency_for_mapping(row, destination)

    assert report['warning'] is False
    assert report['observed_units'] == [
        {'unit': 'g/dL', 'count': 20, 'compatible': True},
    ]


def test_incompatible_units_warn_without_forbidding_the_mapping():
    destination = loinc_destination()
    row = mapping(destination)
    measurement(destination, 'mmol/L')

    report = unit_consistency_for_mapping(row, destination)

    assert report['warning'] is True
    assert report['reason'] == 'incompatible_units'
    assert report['expected_units'] == ['g/dL']
    assert report['observed_units'][0]['compatible'] is False


def test_non_quantitative_destination_warns_when_numeric_units_exist():
    destination = loinc_destination(property_name='Presence', scale='Ordinal')
    row = mapping(destination)
    measurement(destination, 'g/dL')

    report = unit_consistency_for_mapping(row, destination)

    assert report['warning'] is True
    assert report['reason'] == 'destination_not_quantitative'
    assert report['property'] == ''


def test_hospital_mapping_only_uses_units_attributed_to_its_organization():
    destination = loinc_destination()
    first, second = OrganizationFactory(), OrganizationFactory()
    row = mapping(destination, organization=first)
    in_scope = measurement(destination, 'g/dL')
    out_of_scope = measurement(destination, 'mmol/L')
    content_type = ContentType.objects.get_for_model(type(in_scope))
    for clinical_row, organization in ((in_scope, first), (out_of_scope, second)):
        ProvenanceRecord.objects.create(
            source='EHR_SYNC', source_user_id=f'org-{organization.pk}',
            organization=organization, content_type=content_type,
            object_id=clinical_row.pk,
        )

    report = unit_consistency_for_mapping(row, destination)

    assert report['warning'] is False
    assert report['observed_units'] == [
        {'unit': 'g/dL', 'count': 1, 'compatible': True},
    ]


def test_batch_check_keeps_measurement_query_count_flat(django_assert_num_queries):
    destination = loinc_destination()
    rows = [mapping(destination, source_code=f'ALB-{index}') for index in range(3)]
    for index, row in enumerate(rows):
        MeasurementFactory(
            measurement_concept=destination,
            measurement_source_value=row.source_code,
            unit_source_value='g/dL', value_as_number=index,
        )

    # axes, metadata, preference, and one aggregate Measurement query
    with django_assert_num_queries(4):
        reports = unit_consistency_for_mappings(rows, destination=destination)

    assert len(reports) == 3
    assert not any(report['warning'] for report in reports)


def test_approval_requires_explicit_confirmation_for_a_mismatch():
    destination = loinc_destination()
    row = mapping(destination)
    measurement(destination, 'mmol/L')
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='unit-curator@example.test', is_staff=True,
    ))
    url = f'/api/v1/code-mappings/{row.pk}/'

    warning = client.patch(url, {'status': 'approved'}, format='json')

    assert warning.status_code == 409
    assert warning.data['code'] == 'unit_mismatch_confirmation_required'
    row.refresh_from_db()
    assert row.status == 'proposed'

    approved = client.patch(url, {
        'status': 'approved', 'confirm_unit_mismatch': True,
    }, format='json')
    assert approved.status_code == 200
    row.refresh_from_db()
    assert row.status == 'approved'


def test_concept_first_approval_uses_the_same_confirmation_contract():
    destination = loinc_destination()
    row = mapping(destination)
    measurement(destination, 'mmol/L')
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='reverse-unit-curator@example.test', is_staff=True,
    ))
    url = f'/api/v1/concept-to-code/{destination.pk}/mappings/{row.pk}/'
    payload = {'status': 'approved', 'expected_updated_at': row.updated_at.isoformat()}

    warning = client.post(url, payload, format='json')

    assert warning.status_code == 409
    assert warning.data['code'] == 'unit_mismatch_confirmation_required'
    approved = client.post(url, {
        **payload, 'confirm_unit_mismatch': True,
    }, format='json')
    assert approved.status_code == 200
    row.refresh_from_db()
    assert row.status == 'approved'


def test_group_approval_reports_all_mismatching_members_before_queueing():
    destination = loinc_destination()
    first = mapping(destination, source_code='ALB-A')
    second = mapping(destination, source_code='ALB-B')
    for row in (first, second):
        MeasurementFactory(
            measurement_concept=destination,
            measurement_source_value=row.source_code,
            unit_source_value='mmol/L', value_as_number=1,
        )
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='group-unit-curator@example.test', is_staff=True,
    ))

    response = client.post('/api/v1/code-mappings/group/action/', {
        'label': 'albumin', 'source': 'EPIC', 'action': 'approve',
        'destination_concept_id': destination.pk, 'seen_only': '0',
    }, format='json')

    assert response.status_code == 409, response.data
    assert response.data['unit_consistency']['warning_count'] == 2
    assert response.data['unit_consistency']['observed_units'] == [
        {'unit': 'mmol/L', 'count': 2},
    ]
    assert response.data['unit_consistency']['observed_unit_count_kind'] == 'mappings'
    assert not response.data['unit_consistency']['mapping_ids_truncated']
    assert not SourceCodeConceptMapping.objects.filter(status='approved').exists()


def test_audit_command_exits_nonzero_and_prints_evidence(capsys):
    destination = loinc_destination()
    row = mapping(destination, status='approved')
    measurement(destination, 'mmol/L')

    with pytest.raises(SystemExit) as exc:
        call_command('audit_mapping_unit_consistency')

    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert f'EPIC:{row.source_code}' in output
    assert 'mmol/L (1)' in output
