from datetime import date

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.models import (
    ConceptRelationship, LoincClass, LoincCodeClass, LoincRelease,
    Relationship, VocabularyRelease,
)
from omop_core.services.loinc_metadata import loinc_metadata_for_concepts
from patient_portal.models import Identity
from tests.factories import (
    ConceptClassFactory, ConceptFactory, DomainFactory, VocabularyFactory,
)


def _link(source, relationship_id, target, invalid_reason=None):
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
    return ConceptRelationship.objects.create(
        concept_1=source,
        concept_2=target,
        relationship_id=relationship_id,
        valid_start_date=date(1970, 1, 1),
        valid_end_date=date(2099, 12, 31),
        invalid_reason=invalid_reason,
    )


@pytest.fixture
def catalog(db):
    client = APIClient()
    staff = Identity.objects.create_user(
        email='measurement-catalog@example.org', password='test', is_staff=True,
    )
    client.force_authenticate(staff)

    loinc = ConceptFactory(
        concept_id=8_800_001,
        concept_code='1751-7',
        concept_name='Albumin [Mass/volume] in Serum or Plasma',
        standard_concept='S',
    )
    nonstandard = ConceptFactory(
        concept_id=8_800_002,
        concept_code='NON-STANDARD',
        concept_name='Non-standard LOINC measurement',
        standard_concept=None,
    )
    snomed = ConceptFactory(
        concept_id=8_800_003,
        concept_code='SNOMED-MEASUREMENT',
        concept_name='SNOMED standard measurement',
        vocabulary=VocabularyFactory(vocabulary_id='SNOMED', vocabulary_name='SNOMED'),
        standard_concept='S',
    )
    retired = ConceptFactory(
        concept_id=8_800_004,
        concept_code='RETIRED',
        concept_name='Retired standard measurement',
        standard_concept='S',
        invalid_reason='D',
    )
    condition = ConceptFactory(
        concept_id=8_800_005,
        concept_code='CONDITION',
        concept_name='Standard condition',
        domain=DomainFactory(domain_id='Condition', domain_name='Condition'),
        standard_concept='S',
    )

    metadata_domain = DomainFactory(domain_id='Metadata', domain_name='Metadata')
    property_target = ConceptFactory(
        concept_id=8_800_101,
        concept_code='LP6827-2',
        concept_name='Mass Concentration',
        domain=metadata_domain,
        standard_concept=None,
    )
    scale_target = ConceptFactory(
        concept_id=8_800_102,
        concept_code='LP7753-9',
        concept_name='Quantitative',
        domain=metadata_domain,
        standard_concept=None,
    )
    class_target = ConceptFactory(
        concept_id=8_800_103,
        concept_code='CHEM',
        concept_name='Chemistry - non-challenge',
        domain=metadata_domain,
        concept_class=ConceptClassFactory(
            concept_class_id='LOINC Class', concept_class_name='LOINC Class',
        ),
        standard_concept=None,
    )
    _link(loinc, 'Has property', property_target)
    _link(loinc, 'Has scale type', scale_target)
    _link(loinc, 'Is a', class_target)

    LoincClass.objects.create(code='SIDE', display_name='Side-table class')
    LoincCodeClass.objects.create(
        loinc_num=loinc.concept_code,
        loinc_class_id='SIDE',
        example_units='g/dL; mg/dL;g/dL',
        property='SCnc',
        scale_type='Ord',
    )
    vocab_release = VocabularyRelease.objects.create(
        build_timestamp=timezone.now(),
        status='published',
        published_at=timezone.now(),
        athena_version='athena-test',
        vocab_versions={'LOINC': '2.80', 'SNOMED': 'test'},
    )
    LoincRelease.objects.create(
        release_version='2.83',
        release_url='https://example.org/loinc-2.83.zip',
    )
    return {
        'client': client,
        'loinc': loinc,
        'nonstandard': nonstandard,
        'snomed': snomed,
        'retired': retired,
        'condition': condition,
        'vocab_release': vocab_release,
    }


@pytest.mark.django_db
def test_catalog_returns_only_active_standard_measurements(catalog):
    response = catalog['client'].get('/api/v1/concepts/measurements/', {'page_size': 1000})

    assert response.status_code == 200
    payload = response.json()
    by_id = {item['concept_id']: item for item in payload['results']}
    assert catalog['loinc'].pk in by_id
    assert catalog['snomed'].pk in by_id
    assert catalog['nonstandard'].pk not in by_id
    assert catalog['retired'].pk not in by_id
    assert catalog['condition'].pk not in by_id
    assert by_id[catalog['snomed'].pk]['loinc'] is None
    assert by_id[catalog['loinc'].pk]['loinc'] == {
        'class_code': 'CHEM',
        'class_display': 'Chemistry - non-challenge',
        'property': 'MCnc',
        'property_display': 'Mass Concentration',
        'scale_type': 'Qn',
        'scale_type_display': 'Quantitative',
        'example_units': ['g/dL', 'mg/dL'],
    }
    assert payload['snapshot'] == {
        'vocabulary_release_id': catalog['vocab_release'].pk,
        'athena_version': 'athena-test',
        'loinc_release_version': '2.83',
    }


@pytest.mark.django_db
def test_catalog_uses_cursor_pagination(catalog):
    first = catalog['client'].get('/api/v1/concepts/measurements/', {'page_size': 1})
    assert first.status_code == 200
    assert first.json()['next']
    assert 'cursor=' in first.json()['next']

    second = catalog['client'].get(first.json()['next'])
    assert second.status_code == 200
    assert (
        second.json()['results'][0]['concept_id']
        > first.json()['results'][0]['concept_id']
    )
    assert second.json()['snapshot'] == first.json()['snapshot']


@pytest.mark.django_db
def test_detail_and_catalog_share_loinc_representation(catalog):
    catalog_response = catalog['client'].get(
        '/api/v1/concepts/measurements/', {'page_size': 1000},
    ).json()
    catalog_item = next(
        item for item in catalog_response['results']
        if item['concept_id'] == catalog['loinc'].pk
    )
    detail = catalog['client'].get(
        f"/api/v1/concepts/{catalog['loinc'].pk}/",
    )

    assert detail.status_code == 200
    assert detail.json()['loinc'] == catalog_item['loinc']
    nonstandard_detail = catalog['client'].get(
        f"/api/v1/concepts/{catalog['nonstandard'].pk}/",
    )
    assert nonstandard_detail.status_code == 200
    assert nonstandard_detail.json()['loinc']['example_units'] == []


@pytest.mark.django_db
def test_loinc_detail_etag_covers_loinc_release(catalog):
    url = f"/api/v1/concepts/{catalog['loinc'].pk}/"
    response = catalog['client'].get(url)
    etag = response['ETag']
    assert catalog['client'].get(url, HTTP_IF_NONE_MATCH=etag).status_code == 304

    LoincRelease.objects.create(
        release_version='2.84',
        release_url='https://example.org/loinc-2.84.zip',
    )
    changed = catalog['client'].get(url, HTTP_IF_NONE_MATCH=etag)
    assert changed.status_code == 200
    assert changed['ETag'] != etag


@pytest.mark.django_db
def test_missing_loinc_release_does_not_hide_catalog(catalog):
    LoincRelease.objects.all().delete()
    response = catalog['client'].get('/api/v1/concepts/measurements/', {'page_size': 1000})

    assert response.status_code == 200
    assert response.json()['snapshot']['loinc_release_version'] is None
    assert catalog['loinc'].pk in {
        item['concept_id'] for item in response.json()['results']
    }


@pytest.mark.django_db
def test_catalog_etag_covers_both_releases(catalog):
    response = catalog['client'].get('/api/v1/concepts/measurements/')
    etag = response['ETag']
    unchanged = catalog['client'].get(
        '/api/v1/concepts/measurements/', HTTP_IF_NONE_MATCH=etag,
    )
    assert unchanged.status_code == 304

    LoincRelease.objects.create(
        release_version='2.84',
        release_url='https://example.org/loinc-2.84.zip',
    )
    changed = catalog['client'].get(
        '/api/v1/concepts/measurements/', HTTP_IF_NONE_MATCH=etag,
    )
    assert changed.status_code == 200
    assert changed['ETag'] != etag


@pytest.mark.django_db
def test_catalog_requires_authentication():
    response = APIClient().get('/api/v1/concepts/measurements/')
    assert response.status_code in (401, 403)


@pytest.mark.django_db
def test_metadata_queries_are_flat_in_page_size(django_assert_num_queries):
    concepts = [
        ConceptFactory(concept_code=f'QUERY-{index}', standard_concept='S')
        for index in range(3)
    ]
    target = ConceptFactory(
        concept_code='LP-QUERY',
        concept_name='Mass Concentration',
        domain=DomainFactory(domain_id='Metadata', domain_name='Metadata'),
        standard_concept=None,
    )
    for concept in concepts:
        _link(concept, 'Has property', target)

    with django_assert_num_queries(2):
        loinc_metadata_for_concepts(concepts[:1])
    with django_assert_num_queries(2):
        loinc_metadata_for_concepts(concepts)


@pytest.mark.django_db
def test_catalog_query_count_is_flat_in_page_size(catalog):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from patient_portal.api import views

    extra = ConceptFactory(
        concept_id=8_799_999,
        concept_code='EXTRA-LOINC',
        concept_name='Extra standard LOINC measurement',
        standard_concept='S',
    )
    property_target = ConceptRelationship.objects.get(
        concept_1=catalog['loinc'], relationship_id='Has property',
    ).concept_2
    _link(extra, 'Has property', property_target)

    # Warm the process-local vocabulary-version cache so both measured calls
    # have the same release lookup shape.
    catalog['client'].get('/api/v1/concepts/measurements/', {'page_size': 1})
    assert views._vocab_version_cache['map'] is not None

    with CaptureQueriesContext(connection) as one:
        one_response = catalog['client'].get(
            '/api/v1/concepts/measurements/', {'page_size': 1},
        )
    with CaptureQueriesContext(connection) as two:
        two_response = catalog['client'].get(
            '/api/v1/concepts/measurements/', {'page_size': 2},
        )

    assert one_response.status_code == two_response.status_code == 200
    assert len(one_response.json()['results']) == 1
    assert len(two_response.json()['results']) == 2
    assert len(two) == len(one)


@pytest.mark.django_db
def test_unknown_and_retired_relationships_do_not_use_side_table_axes():
    concept = ConceptFactory(concept_code='UNKNOWN-AXIS', standard_concept='S')
    metadata_domain = DomainFactory(domain_id='Metadata', domain_name='Metadata')
    unknown = ConceptFactory(
        concept_code='LP-UNKNOWN', concept_name='Unknown property',
        domain=metadata_domain, standard_concept=None,
    )
    retired_scale = ConceptFactory(
        concept_code='LP-RETIRED-SCALE', concept_name='Ordinal',
        domain=metadata_domain, standard_concept=None,
    )
    _link(concept, 'Has property', unknown)
    _link(concept, 'Has scale type', retired_scale, invalid_reason='D')
    LoincClass.objects.create(code='SIDE2', display_name='Side table')
    LoincCodeClass.objects.create(
        loinc_num=concept.concept_code,
        loinc_class_id='SIDE2',
        example_units='',
        property='MCnc',
        scale_type='Qn',
    )

    metadata = loinc_metadata_for_concepts([concept])[concept.pk]
    assert metadata['property'] is None
    assert metadata['property_display'] == 'Unknown property'
    assert metadata['scale_type'] is None
    assert metadata['scale_type_display'] is None
    assert metadata['class_code'] is None
