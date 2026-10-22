"""The field mapper's Suggest search for Genomics form fields (#1705).

Suggest used to seed the concept search with the field name, so every Genomics
field searched for "genomics ..." and matched no concept.
"""
import pytest
from rest_framework.test import APIClient

from omop_core.services.field_descriptor import (
    _GENOMICS_SUGGEST_QUERIES,
    _suggest_query,
    get_all_field_descriptors,
)
from omop_core.services.genomics_catalog import markers
from patient_portal.models import Identity
from tests.factories import ConceptFactory


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('field_name, query', [
    ('genomics_brca1', 'BRCA1 gene'),
    ('genomics_palb2', 'PALB2 gene'),
    ('genomics_fam46c', 'TENT5C gene'),
    ('genomics_myc', 'MYC rearrangement'),
    ('genomics_t414', 't(4;14)'),
    ('genomics_del17p', 'del(17p)'),
    ('genomics_gain1q', '1q21'),
    ('genomics_trisomy12', 'Trisomy 12'),
    ('genomics_atm_atr', 'ATM gene'),
    ('genetic_mutations.allelic_frequency', 'allelic frequency'),
    ('genomics_not_in_catalog', 'not in catalog'),
    ('smoking_status', 'smoking status'),
])
def test_suggest_query(field_name, query):
    assert _suggest_query(field_name) == query


def test_every_genomics_descriptor_searches_without_its_prefix():
    genomics = [d for d in get_all_field_descriptors() if d['tab'] == 'genomics'
                and d['field_name'] != 'genetic_mutations']
    assert len(genomics) > len(markers())
    for descriptor in genomics:
        query = descriptor['suggest_query'].lower()
        assert 'genomics' not in query and 'genetic mutations' not in query, descriptor['field_name']
        # The concept search rejects anything shorter.
        assert len(query) >= 3, descriptor['field_name']


def test_overrides_name_catalog_markers():
    assert set(_GENOMICS_SUGGEST_QUERIES) <= {m['key'] for m in markers()}


@pytest.mark.parametrize('field_name, concept_name', [
    ('genomics_brca1', 'BRCA1 gene mutation detected'),
    ('genomics_t414', 't(4;14) measurement'),
    ('genomics_del13q', 'Chromosome region 13q14 deletion in Bone marrow by FISH'),
])
def test_suggest_query_finds_the_concept(field_name, concept_name):
    concept = ConceptFactory(concept_name=concept_name, domain_id='Measurement', vocabulary_id='LOINC')
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='curator@example.com', password='x', is_staff=True))
    response = client.get('/api/v1/concepts/search/', {'q': _suggest_query(field_name)})
    assert response.status_code == 200
    assert concept.concept_id in {c['concept_id'] for c in response.json()['results']}
