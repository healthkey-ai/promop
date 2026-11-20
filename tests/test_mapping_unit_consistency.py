from datetime import date

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from rest_framework.test import APIClient

from omop_core.models import (
    ConceptRelationship,
    LoincClass,
    LoincCodeClass,
    PatientSourceCode,
    ProvenanceRecord,
    Relationship,
    SourceCodeConceptMapping,
)
from omop_core.services.mapping_unit_consistency import (
    unit_consistency_for_mapping,
    unit_consistency_for_mappings,
)
from patient_portal.models import Identity
from tests.factories import ConceptFactory, MeasurementFactory, OrganizationFactory, PersonFactory

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


def measurement(destination, unit, *, source_code='ALB-1', vocab='EPIC', **kwargs):
    m = MeasurementFactory(
        measurement_concept=destination, measurement_source_value=source_code,
        unit_source_value=unit, value_as_number=4.2, **kwargs,
    )
    if not kwargs.get('is_erroneous'):
        PatientSourceCode.objects.get_or_create(
            person=m.person, source_value=source_code,
            source_vocabulary_id=vocab, omop_table='measurement',
            defaults={'source_unit': unit, 'occurrence_count': 1},
        )
    return m


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


def test_hospital_mapping_only_uses_units_from_its_own_hospital_code():
    """With PSC-based evidence, scoping is by (source_vocabulary_id, source_code),
    not by ProvenanceRecord attribution on Measurement rows."""
    destination = loinc_destination()
    first_org, second_org = OrganizationFactory(), OrganizationFactory()
    # Hospital A's mapping with g/dL evidence.
    row = mapping(destination, source_vocabulary_id='HOSP-A', organization=first_org)
    person = PersonFactory()
    PatientSourceCode.objects.create(
        person=person, source_value='ALB-1', source_vocabulary_id='HOSP-A',
        omop_table='measurement', source_unit='g/dL', occurrence_count=1)
    # Hospital B has mmol/L — a different source vocabulary, so it must not
    # leak into Hospital A's evidence.
    PatientSourceCode.objects.create(
        person=person, source_value='ALB-1', source_vocabulary_id='HOSP-B',
        omop_table='measurement', source_unit='mmol/L', occurrence_count=1)

    report = unit_consistency_for_mapping(row, destination)

    assert report['warning'] is False
    assert report['observed_units'] == [
        {'unit': 'g/dL', 'count': 1, 'compatible': True},
    ]


def test_batch_check_keeps_query_count_flat(django_assert_num_queries):
    destination = loinc_destination()
    rows = [mapping(destination, source_code=f'ALB-{index}') for index in range(3)]
    person = PersonFactory()
    for row in rows:
        PatientSourceCode.objects.create(
            person=person, source_value=row.source_code,
            source_vocabulary_id='EPIC', omop_table='measurement',
            source_unit='g/dL', occurrence_count=1)

    # axes, metadata, preference, and one PSC query for the batch
    with django_assert_num_queries(4):
        reports = unit_consistency_for_mappings(rows, destination=destination)

    assert len(reports) == 3
    assert not any(report['warning'] for report in reports)


def test_approval_saves_without_a_unit_confirmation():
    """Stored units no longer gate approval: loading normalizes values (#1952)."""
    destination = loinc_destination()
    row = mapping(destination)
    measurement(destination, 'mmol/L')
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='unit-curator@example.test', is_staff=True,
    ))

    approved = client.patch(f'/api/v1/code-mappings/{row.pk}/', {'status': 'approved'}, format='json')

    assert approved.status_code == 200, approved.data
    row.refresh_from_db()
    assert row.status == 'approved'


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
