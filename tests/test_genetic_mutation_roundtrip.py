"""Regression coverage for the clinician mutation editor's OMOP round trip."""

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from omop_core.models import Concept, Measurement, PatientRecord
from patient_portal.api.views import PatientRecordViewSet
from patient_portal.models import Identity
from tests.factories import ConceptFactory, PatientRecordFactory, PersonFactory


pytestmark = pytest.mark.django_db


def test_patient_patch_writes_gene_mutation_to_omop_and_returns_projection():
    """UI payload → Measurement → PatientRecord → the response consumed by UI."""
    person = PersonFactory()
    PatientRecordFactory(person=person)
    ConceptFactory(vocabulary__vocabulary_id='CDM', concept_code='measurement.measurement_id')
    variant_concept = ConceptFactory(concept_code='81252-9')
    from django.core.management import call_command
    call_command('seed_genomics_catalog')
    ConceptFactory(
        concept_id=45876022,
        concept_code='36908-2',
        concept_name='Gene mutations tested for',
    )
    ConceptFactory(
        concept_id=32865,
        concept_code='32865',
        concept_name='Patient self-report',
    )
    ConceptFactory(concept_id=255395001, concept_code='255395001', concept_name='Germline')
    ConceptFactory(concept_id=30166007, concept_code='30166007', concept_name='Pathogenic')
    staff = Identity.objects.create_user(
        email='mutation-editor@example.test', password='pw', is_staff=True,
    )
    request = APIRequestFactory().patch(
        f'/api/v1/persons/{person.person_id}/',
        {'genetic_mutations': [{
            'gene': 'BRCA1', 'mutation': 'c.68_69delAG', 'test_date': '2024-01-15',
            'origin': 'germline', 'interpretation': 'pathogenic',
        }]},
        format='json',
    )
    force_authenticate(request, user=staff)

    response = PatientRecordViewSet.as_view({'patch': 'partial_update'})(
        request, pk=person.person_id,
    )

    assert response.status_code == 200
    row = Measurement.objects.get(person=person, measurement_source_value='genomics:brca1')
    assert row.measurement_concept_id == variant_concept.pk
    assert row.qualifier_source_value == 'BRCA1'
    assert row.value_as_string == 'c.68_69delAG'
    assert row.qualifier_concept_id is None
    assert row.value_as_concept_id is None
    record = PatientRecord.objects.get(person=person)
    assert record.genetic_mutations == [{
        'id': row.pk, 'marker_key': 'brca1', 'gene': 'BRCA1', 'variant': 'c.68_69delAG', 'test_date': '2024-01-15',
        'origin': 'germline', 'interpretation': 'pathogenic', 'status': 'present', 'provenance': 'asserted',
        'genomic_feature': 'BRCA1', 'feature_type': 'Gene',
    }]
    assert response.data['genetic_mutations'] == record.genetic_mutations
