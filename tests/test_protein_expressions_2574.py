"""protein_expressions is a record-only multi-answer field (#2574).

The seeded curation row projected the comma-joined answer into one
``Observation.value_as_string`` (60 characters), which a realistic selection
exceeds. The field is a record-only list now; imported markers are mapped to
the picker's reviewed labels and stay removable.
"""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from omop_core.models import FieldChoice, Measurement, Observation, PatientRecord
from omop_core.services.patient_record_service import refresh_patient_record
from patient_portal.models import Identity
from tests.factories import (
    ConceptFactory, MeasurementFactory, PatientRecordFactory, PersonFactory,
)

pytestmark = pytest.mark.django_db

OPTIONS = [
    f'{marker} {sign}'
    for marker in ('CD38', 'ZAP-70', 'CD49d', 'CD19', 'CD5', 'CD20', 'CD23',
                   'Kappa light chain', 'Lambda light chain')
    for sign in ('+ve', '-ve')
]


@pytest.fixture(autouse=True)
def _protein_choices(db):
    """The option rows come from migration 0217, which pytest skips."""
    for order, display in enumerate(OPTIONS):
        FieldChoice.objects.get_or_create(
            field_name='protein_expressions', display=display,
            defaults={'sort_order': order})

SEVEN = ['CD38 +ve', 'ZAP-70 +ve', 'CD49d +ve', 'CD19 +ve', 'CD5 +ve',
         'CD20 +ve', 'CD23 +ve']


def _client():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='protein-2574@example.test', is_staff=True))
    return client


def _record(**fields):
    person = PersonFactory()
    return PatientRecordFactory(person=person, **fields)


def _marker(person, name, value):
    return MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(
            concept_name=name, concept_code=f'X-2574-{name}'),
        measurement_source_value=name,
        value_as_string=value,
        value_as_number=None,
    )


def test_the_descriptor_reports_a_multi_answer_field():
    record = _record()
    resp = _client().get(
        '/api/v1/patient-records/writable-fields/',
        {'person_id': record.person_id}, HTTP_X_PATIENT_FORM='1')

    entry = resp.data['protein_expressions']
    assert entry['writable'] is True
    assert entry['multiple'] is True
    assert 'CD5 +ve' in [option['value'] for option in entry['options']]


def test_a_list_patch_round_trips():
    record = _record()
    resp = _client().patch(
        f'/api/v1/patient-records/{record.person_id}/',
        {'protein_expressions': ['CD5 +ve', 'CD23 -ve']},
        format='json', HTTP_X_PATIENT_FORM='1')

    assert resp.status_code == 200, resp.data.get('write')
    record.refresh_from_db()
    assert record.protein_expressions == 'CD5 +ve, CD23 -ve'


def test_the_seven_marker_selection_saves():
    record = _record()
    resp = _client().patch(
        f'/api/v1/patient-records/{record.person_id}/',
        {'protein_expressions': SEVEN}, format='json', HTTP_X_PATIENT_FORM='1')

    assert resp.status_code == 200, resp.data.get('write')
    record.refresh_from_db()
    assert record.protein_expressions == ', '.join(SEVEN)


def test_an_unknown_new_answer_is_refused():
    record = _record(protein_expressions='CD5 +ve')
    resp = _client().patch(
        f'/api/v1/patient-records/{record.person_id}/',
        {'protein_expressions': ['CD5 +ve', 'CD999 +ve']},
        format='json', HTTP_X_PATIENT_FORM='1')

    assert resp.status_code == 400, resp.data
    record.refresh_from_db()
    assert record.protein_expressions == 'CD5 +ve'


def test_a_native_patch_accepts_the_list_too():
    record = _record()
    resp = _client().patch(
        f'/api/v1/patient-records/{record.person_id}/',
        {'protein_expressions': ['CD19 +ve', 'CD20 -ve']}, format='json')

    assert resp.status_code == 200, resp.data
    record.refresh_from_db()
    assert record.protein_expressions == 'CD19 +ve, CD20 -ve'


def test_imported_markers_are_mapped_to_the_picker_labels():
    record = _record()
    _marker(record.person, 'CD5', 'positive')
    _marker(record.person, 'CD19', 'negative')
    _marker(record.person, 'ZAP70', 'positive')

    refresh_patient_record(record.person)
    record.refresh_from_db()

    assert set(record.protein_expressions.split(', ')) == {
        'CD5 +ve', 'CD19 -ve', 'ZAP-70 +ve'}
    assert not Observation.objects.filter(person=record.person).exists()


def test_an_unmapped_imported_marker_can_be_removed():
    record = _record()
    _marker(record.person, 'CD5', 'equivocal')
    refresh_patient_record(record.person)
    record.refresh_from_db()
    assert 'CD5: equivocal' in record.protein_expressions

    resp = _client().patch(
        f'/api/v1/patient-records/{record.person_id}/',
        {'protein_expressions': ['CD23 -ve']}, format='json', HTTP_X_PATIENT_FORM='1')
    assert resp.status_code == 200, resp.data.get('write')

    refresh_patient_record(record.person)
    record.refresh_from_db()
    assert record.protein_expressions == 'CD23 -ve'


def test_marker_matching_is_bounded_and_unambiguous():
    from omop_core.services.patient_record_service import _protein_marker_label

    assert _protein_marker_label('CD5') == 'CD5'
    assert _protein_marker_label('CD-5') == 'CD5'
    assert _protein_marker_label('CD50') is None          # not CD5
    assert _protein_marker_label('CD49d') == 'CD49d'      # not CD4
    assert _protein_marker_label('ZAP70') == 'ZAP-70'
    assert _protein_marker_label('Kappa/Lambda ratio') is None
    assert _protein_marker_label('CD19/CD3 panel') is None
    assert _protein_marker_label('Kappa free light chains') is None
    assert _protein_marker_label('Kappa light chain') == 'Kappa light chain'


def test_an_already_normalised_value_keeps_its_polarity():
    record = _record()
    _marker(record.person, 'CD5', '+ve')
    _marker(record.person, 'CD19', '-ve')

    refresh_patient_record(record.person)
    record.refresh_from_db()

    assert set(record.protein_expressions.split(', ')) == {'CD5 +ve', 'CD19 -ve'}


def test_a_surviving_mapping_does_not_restore_the_projection():
    """An approved projection row must not bring the scalar Observation back
    (#2574): the descriptor entry wins, and a long list writes no Observation."""
    from omop_core.models import FieldConceptMapping

    record = _record()
    concept = ConceptFactory(concept_code='371511004', concept_name='Protein expression')
    FieldConceptMapping.objects.create(
        field_name='protein_expressions', concept=concept, vocabulary_id='SNOMED',
        concept_code='371511004', status='approved', omop_table='Observation',
        source_value='', value_kind='', multiple=False,
    )
    resp = _client().patch(
        f'/api/v1/patient-records/{record.person_id}/',
        {'protein_expressions': SEVEN}, format='json', HTTP_X_PATIENT_FORM='1')

    assert resp.status_code == 200, resp.data.get('write')
    record.refresh_from_db()
    assert record.protein_expressions == ', '.join(SEVEN)
    assert not Observation.objects.filter(person=record.person).exists()


def test_the_native_patch_validates_and_normalises_the_list():
    record = _record()
    resp = _client().patch(
        f'/api/v1/patient-records/{record.person_id}/',
        {'protein_expressions': ['CD5 +ve', ' CD5 +ve ', 'CD23 -ve']}, format='json')

    assert resp.status_code == 200, resp.data
    record.refresh_from_db()
    assert record.protein_expressions == 'CD5 +ve, CD23 -ve'

    bad = _client().patch(
        f'/api/v1/patient-records/{record.person_id}/',
        {'protein_expressions': ['CD999 +ve']}, format='json')
    assert bad.status_code == 400, bad.data


def test_a_coded_polarity_answer_contributes_a_marker():
    record = _record()
    positive = ConceptFactory(concept_code='POS-2574', concept_name='Positive')
    MeasurementFactory(
        person=record.person,
        measurement_concept=ConceptFactory(concept_name='CD23', concept_code='CD23-2574'),
        measurement_source_value='CD23', value_as_concept=positive,
        value_as_number=None, value_as_string=None)

    refresh_patient_record(record.person)
    record.refresh_from_db()

    assert record.protein_expressions == 'CD23 +ve'


def _load_migration():
    import importlib
    from types import SimpleNamespace
    from django.apps import apps as django_apps
    module = importlib.import_module(
        'omop_core.migrations.0346_protein_expressions_record_only')
    schema_editor = SimpleNamespace(connection=SimpleNamespace(alias='default'))
    return module, django_apps, schema_editor


def _seed_mapping():
    from omop_core.models import FieldConceptMapping
    concept = ConceptFactory(concept_code='371511004', concept_name='Protein expression')
    return FieldConceptMapping.objects.create(
        field_name='protein_expressions', concept=concept, vocabulary_id='SNOMED',
        concept_code='371511004', status='approved', omop_table='Observation',
        source_value='', value_kind='', multiple=False,
        provenance='system_generated', tab='',
        notes='Auto-proposed from field suggestion.',
    )


def test_the_migration_relabels_stored_answers_without_freezing_the_field():
    from omop_core.models import FieldConceptMapping, RecordRevision

    record = _record(protein_expressions='ZAP70: positive, Kappa (κ) light chain -ve')
    _seed_mapping()

    module, django_apps, schema_editor = _load_migration()
    module.remove_seeded_projection(django_apps, schema_editor)

    record.refresh_from_db()
    assert record.protein_expressions == 'ZAP-70 +ve, Kappa light chain -ve'
    assert 'protein_expressions' not in (record.user_edited_fields or [])
    assert RecordRevision.objects.filter(
        patient_record_id=record.pk, field='protein_expressions',
        changed_by='system').exists()
    assert not FieldConceptMapping.objects.filter(
        field_name='protein_expressions').exists()

    _marker(record.person, 'CD23', 'positive')
    refresh_patient_record(record.person)
    record.refresh_from_db()
    assert 'CD23 +ve' in record.protein_expressions


def test_the_migration_keeps_an_acknowledged_answer_pending():
    """The retired projection acknowledged the answer, so it must not be
    replaced by an older imported marker after the upgrade (#2574 review)."""
    from tests.factories import ObservationFactory

    record = _record()
    _marker(record.person, 'CD5', 'positive')
    mapping = _seed_mapping()
    ObservationFactory(
        person=record.person, observation_concept=mapping.concept,
        observation_date='2026-01-01', value_as_string='CD23 -ve')
    # The clinician's answer lands after the historical projection.
    record.protein_expressions = 'CD23 -ve'
    record.save(update_fields=['protein_expressions'])

    module, django_apps, schema_editor = _load_migration()
    module.remove_seeded_projection(django_apps, schema_editor)

    record.refresh_from_db()
    assert 'protein_expressions' in (record.user_edited_fields or [])
    refresh_patient_record(record.person)
    record.refresh_from_db()
    assert record.protein_expressions == 'CD23 -ve'
