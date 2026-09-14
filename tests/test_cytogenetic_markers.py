"""PatientRecord-first cytogenetic marker authoring and refresh coverage."""

from importlib import import_module
from unittest.mock import patch

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from omop_core.models import (
    Concept, FieldChoice, FieldChoiceCode, FieldConceptMapping, Note, Observation, PatientRecord,
)
from omop_core.services.cytogenetics import (
    CANONICAL_CYTOGENETIC_MARKERS, normalise_cytogenetic_markers,
    read_cytogenetic_summary,
)
from omop_core.services.write_descriptor import KIND_COMPUTED, build_writable_field_descriptor
from tests.factories import ConceptFactory, DomainFactory, PatientRecordFactory, VocabularyFactory


pytestmark = pytest.mark.django_db


def _cytogenetic_concept():
    import_module('omop_core.migrations.0077_seed_concept_zero').seed_concept_zero(apps, None)
    return Concept.objects.get(pk=0)


def _approved_mapping(concept):
    return FieldConceptMapping.objects.create(
        field_name='cytogenetic_markers',
        concept=concept,
        vocabulary_id='',
        concept_code='',
        omop_table='observation',
        source_value='mm-cytogenetic-markers',
        value_kind='string',
        multiple=True,
        status='approved',
    )


def test_normalises_ui_and_import_spellings_without_losing_unknown_import_data():
    assert normalise_cytogenetic_markers(
        'del(17p13), 1q21 amplification, DEL17P, MYC rearrangement'
    ) == 'del17p, 1q_amp, MYC rearrangement'
    assert normalise_cytogenetic_markers('future-marker') == 'future-marker'
    with pytest.raises(ValueError, match='future-marker'):
        normalise_cytogenetic_markers('future-marker', strict=True)


def test_seed_is_explicit_under_pytest_no_migrations():
    """Exercise seed logic directly because pytest.ini uses --no-migrations."""
    _cytogenetic_concept()
    migration = import_module('omop_core.migrations.0222_cytogenetic_markers')

    migration.migrate_and_seed(apps, None)

    choices = {
        choice.display: choice
        for choice in FieldChoice.objects.filter(field_name='cytogenetic_markers')
    }
    assert set(migration.CHOICES)  # the frozen migration owns the deployed value set
    assert {'del17p', 't(4;14)', 't(11;14)', '1q_amp', 'hyperdiploidy',
            'MYC rearrangement'} <= choices.keys()
    assert not FieldChoiceCode.objects.filter(choice__field_name='cytogenetic_markers').exists()
    mapping = FieldConceptMapping.objects.get(field_name='cytogenetic_markers')
    assert mapping.status == 'approved'
    assert mapping.concept_id == 0
    assert mapping.vocabulary_id == mapping.concept_code == ''
    assert mapping.source_value == 'mm-cytogenetic-markers'
    assert mapping.multiple is True


def test_approved_mapping_cannot_reenable_legacy_summary_authoring():
    _approved_mapping(_cytogenetic_concept())
    FieldChoice.objects.create(field_name='cytogenetic_markers', display='del17p', sort_order=0)
    entry = build_writable_field_descriptor()['cytogenetic_markers']
    assert entry['kind'] == KIND_COMPUTED
    assert entry['target'] == 'genomics'
    assert not entry['writable']
    assert 'projection' not in entry


def test_interactive_summary_patch_is_rejected_and_imported_facts_still_refresh():
    from tests.factories import ObservationFactory
    _approved_mapping(_cytogenetic_concept())
    record = PatientRecordFactory(disease='multiple myeloma')
    user = get_user_model().objects.create_user(email='cytogenetics-admin@example.test', password='test', is_staff=True)
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.patch(f'/api/patient-info/{record.person_id}/',
        {'cytogenetic_markers': 'del(17p13), 1q21 amplification'}, format='json')
    assert response.status_code == 400, response.data
    assert not Observation.objects.filter(person=record.person).exists()
    observation = ObservationFactory(person=record.person,
        observation_source_value='mm-cytogenetic-markers',
        value_as_string='FGFR3/IGH translocation t(4;14), del(17p13)')
    record.refresh_from_db()
    assert record.cytogenetic_markers == 't(4;14), del17p'
    assert observation.value_as_string == 'FGFR3/IGH translocation t(4;14), del(17p13)'


def test_serializer_rejects_values_outside_the_ui_vocabulary():
    record = PatientRecordFactory(disease='multiple myeloma')
    from patient_portal.api.serializers import PatientRecordSerializer

    serializer = PatientRecordSerializer(
        record, data={'cytogenetic_markers': 'not-a-reviewed-marker'}, partial=True,
    )
    assert serializer.is_valid() is False
    assert 'cytogenetic_markers' in serializer.errors
    assert 'not-a-reviewed-marker' not in str(serializer.errors)


def test_serializer_accepts_the_legacy_write_name_but_emits_only_the_canonical_name():
    record = PatientRecordFactory(disease='multiple myeloma', cytogenetic_markers='del17p')
    from patient_portal.api.serializers import PatientRecordSerializer

    serializer = PatientRecordSerializer(
        record, data={'cytogenic_markers': 'del17p'}, partial=True,
    )
    assert serializer.is_valid(), serializer.errors
    saved = serializer.save()
    assert saved.cytogenetic_markers == 'del17p'
    representation = PatientRecordSerializer(saved).data
    assert representation['cytogenetic_markers'] == 'del17p'
    assert 'cytogenic_markers' not in representation


def test_repair_removes_wrong_codes_without_changing_1204_status_or_curated_choices():
    from tests.factories import ObservationFactory

    _cytogenetic_concept()
    status_concept = ConceptFactory(
        concept_code='69548-6', concept_name='Genetic variant assessment',
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
        domain=DomainFactory(domain_id='Measurement', domain_name='Measurement'),
    )
    summary = FieldConceptMapping.objects.create(
        field_name='cytogenetic_markers', concept=status_concept,
        vocabulary_id='LOINC', concept_code='69548-6', omop_table='observation',
        source_value='mm-cytogenetic-markers', value_kind='string', status='approved',
    )
    status = FieldConceptMapping.objects.create(
        field_name='genetic_mutations.status', concept=status_concept,
        vocabulary_id='LOINC', concept_code='69548-6', omop_table='measurement',
        source_value='genomics:status', value_kind='string', status='approved',
    )
    choice = FieldChoice.objects.create(field_name='cytogenetic_markers', display='1q_amp')
    bad = FieldChoiceCode.objects.create(choice=choice, vocabulary_id='LOINC', code='81249-5')
    curated = FieldChoiceCode.objects.create(choice=choice, vocabulary_id='Local', code='1q_amp')
    # This was written by an earlier development version of the summary seed.
    wrong_fact = ObservationFactory(
        observation_concept=status_concept, observation_source_value='mm-cytogenetic-markers',
        value_as_string='1q_amp',
    )
    separate_status = ObservationFactory(
        observation_concept=status_concept, observation_source_value='genomics:status',
        value_as_string='present',
    )
    migration = import_module('omop_core.migrations.0225_merge_cytogenetic_markers')
    migration.correct_cytogenetic_codes(apps, None)
    migration.correct_cytogenetic_codes(apps, None)
    summary.refresh_from_db()
    status.refresh_from_db()
    wrong_fact.refresh_from_db()
    separate_status.refresh_from_db()
    assert summary.concept_id == wrong_fact.observation_concept_id == 0
    assert wrong_fact.value_as_string == '1q_amp'
    assert summary.concept_code == summary.vocabulary_id == ''
    assert status.concept_id == status_concept.pk
    assert status.omop_table == 'measurement'
    assert status.concept_code == '69548-6'
    assert separate_status.observation_concept_id == status_concept.pk
    assert not FieldChoiceCode.objects.filter(pk=bad.pk).exists()
    assert FieldChoiceCode.objects.filter(pk=curated.pk).exists()


def test_seed_preserves_a_curator_supplied_summary_recipe():
    concept = ConceptFactory()
    mapping = _approved_mapping(concept)
    import_module('omop_core.migrations.0222_cytogenetic_markers').migrate_and_seed(apps, None)
    mapping.refresh_from_db()
    assert mapping.concept_id == concept.pk


def test_rename_preserves_pending_edit_protection_during_refresh():
    from omop_core.services.patient_record_service import refresh_patient_record

    _cytogenetic_concept()
    record = PatientRecordFactory(
        cytogenetic_markers='del17p',
        user_edited_fields=['cytogenic_markers', 'disease', 'cytogenetic_markers'],
    )
    migration = import_module('omop_core.migrations.0228_merge_cytogenetics_and_genomics')
    migration.repair_pending_edits(apps, None)
    migration.repair_pending_edits(apps, None)
    record.refresh_from_db()
    assert record.user_edited_fields == ['cytogenetic_markers', 'disease']
    refresh_patient_record(record.person)
    record.refresh_from_db()
    assert record.cytogenetic_markers == 'del17p'
    assert 'cytogenetic_markers' in record.user_edited_fields


@pytest.mark.parametrize('table', ['observation', 'measurement'])
def test_all_markers_roundtrip_through_note_and_same_day_edits_and_clear(table):
    from django.utils import timezone
    from omop_core.models import Measurement
    from omop_core.services.patient_record_service import refresh_patient_record

    _cytogenetic_concept()
    mapping = _approved_mapping(Concept.objects.get(pk=0))
    mapping.omop_table = table
    mapping.save(update_fields=['omop_table'])
    record = PatientRecordFactory(disease='multiple myeloma')
    full = normalise_cytogenetic_markers(CANONICAL_CYTOGENETIC_MARKERS, strict=True)
    assert len(full) > 60
    # Reference length must not assume NOTE IDs have only a few digits.
    Note.objects.create(
        note_id=10**15, person=record.person, note_date=timezone.localdate(),
        note_type_concept_id=0, note_text='unrelated note', note_source_value='unrelated',
    )
    model = Observation if table == 'observation' else Measurement
    note_id = None
    # The second long value changes only NOTE text, not the reference string.
    from omop_core.services.omop_projection import project_single_value
    projection = {'omop_table': table, 'concept_id': 0, 'type_concept_id': 32817,
                  'source_value': 'mm-cytogenetic-markers', 'value_kind': 'string'}
    for value in (full, ', '.join(reversed(CANONICAL_CYTOGENETIC_MARKERS)), 'del17p', '', full):
        assert project_single_value(record.person, 'cytogenetic_markers', value, projection)
        fact = model.objects.get(person=record.person, **{f'{table}_source_value': 'mm-cytogenetic-markers'})
        assert len(fact.value_as_string or '') <= 60
        if len(value) > 60:
            assert read_cytogenetic_summary(fact) == value
            note = Note.objects.get(person=record.person, note_source_value=f'cytogenetics:{table}:{fact.pk}')
            assert note.note_text == value
            assert note.pk == note_id if note_id is not None else note.pk > 10**15
            note_id = note.pk
        refresh_patient_record(record.person)
        record.refresh_from_db()
        assert record.cytogenetic_markers == (value or None)


def test_overflow_note_history_survives_later_day_projection():
    from datetime import date
    from omop_core.services.omop_projection import project_single_value
    from omop_core.services.patient_record_service import refresh_patient_record

    _cytogenetic_concept()
    _approved_mapping(Concept.objects.get(pk=0))
    record = PatientRecordFactory(disease='multiple myeloma')
    projection = {'omop_table': 'observation', 'concept_id': 0, 'type_concept_id': 32817,
                  'source_value': 'mm-cytogenetic-markers', 'value_kind': 'string'}
    full = normalise_cytogenetic_markers(CANONICAL_CYTOGENETIC_MARKERS)
    for day, value in [(date(2026, 1, 1), full), (date(2026, 1, 2), 'del17p')]:
        with patch('omop_core.services.omop_projection.timezone.localdate', return_value=day):
            assert project_single_value(record.person, 'cytogenetic_markers', value, projection)
    older = Observation.objects.get(person=record.person, observation_date=date(2026, 1, 1))
    assert read_cytogenetic_summary(older) == full
    refresh_patient_record(record.person)
    record.refresh_from_db()
    assert record.cytogenetic_markers == 'del17p'


def test_note_reference_cannot_read_another_patient_or_fact():
    from django.utils import timezone
    from tests.factories import ObservationFactory

    _cytogenetic_concept()
    own = PatientRecordFactory()
    other = PatientRecordFactory()
    fact = ObservationFactory(person=own.person, observation_source_value='mm-cytogenetic-markers')
    note = Note.objects.create(
        note_id=99, person=other.person, note_date=timezone.localdate(),
        note_type_concept_id=0, note_source_value=f'cytogenetics:observation:{fact.pk}',
        note_text='another patient private text',
    )
    fact.value_as_string = f'[note:{note.pk}]'
    assert read_cytogenetic_summary(fact) == fact.value_as_string
    note.person = own.person
    note.note_source_value = f'cytogenetics:measurement:{fact.pk}'
    note.save()
    del fact._cytogenetic_summary_text
    assert read_cytogenetic_summary(fact) == fact.value_as_string


def test_failed_fact_write_rolls_back_new_overflow_note():
    from django.db import DatabaseError
    from omop_core.services.omop_projection import project_single_value

    _cytogenetic_concept()
    _approved_mapping(Concept.objects.get(pk=0))
    record = PatientRecordFactory()
    projection = {'omop_table': 'observation', 'concept_id': 0, 'type_concept_id': 32817,
                  'source_value': 'mm-cytogenetic-markers', 'value_kind': 'string'}
    full = normalise_cytogenetic_markers(CANONICAL_CYTOGENETIC_MARKERS)
    with patch.object(Observation, 'save', side_effect=DatabaseError('write failed')):
        assert not project_single_value(record.person, 'cytogenetic_markers', full, projection)
    assert not Note.objects.filter(person=record.person).exists()
