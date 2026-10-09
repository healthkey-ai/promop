from patient_portal.api.serializers import PatientListSerializer
from tests.factories import PatientRecordFactory
import pytest

pytestmark = pytest.mark.django_db


def test_summary_includes_variants_status_and_num_lines_without_extra_queries(django_assert_num_queries):
    record = PatientRecordFactory(genetic_mutations=[
        {'gene': 'brca1', 'variant': 'c.68_69delAG', 'status': 'present'},
        {'gene': 'BRCA1', 'variant': 'c.68_69delAG', 'status': 'present'},
        {'gene': 'TP53', 'status': 'absent'},
    ], therapy_lines_count=3)
    with django_assert_num_queries(0):
        data = PatientListSerializer(record).data
    assert data['genomics_summary'] == 'BRCA1 c.68_69delAG (present); TP53 (absent)'
    assert data['therapy_lines_count'] == 3


def test_empty_genomics_and_zero_lines():
    data = PatientListSerializer(PatientRecordFactory(genetic_mutations=[], therapy_lines_count=0)).data
    assert data['genomics_summary'] == ''
    assert data['therapy_lines_count'] == 0


def test_chromosome_summary_uses_features_for_new_and_legacy_findings(django_assert_num_queries):
    record = PatientRecordFactory(genetic_mutations=[
        {'gene': 'TP53', 'variant': 'del17p', 'status': 'present'},
        {'genomic_feature': 'Chromosome 12', 'feature_type': 'Chromosome(s)', 'variant_name': 'Trisomy 12', 'status': 'present'},
    ])
    with django_assert_num_queries(0):
        data = PatientListSerializer(record).data
    assert data['genomics_summary'] == '17p del17p (present); Chromosome 12 Trisomy 12 (present)'
