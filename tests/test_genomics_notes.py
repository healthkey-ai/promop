"""Regression coverage for owned narrative text and legacy overflow safety."""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from omop_core.models import Measurement, Note, Observation
from omop_core.services.clinical_text import OwnedTextReader, note_id
from omop_core.services.genomics import (
    _read_note_text, delete_variant, list_variants, save_variant,
)
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.pk import next_pk
from tests.factories import MeasurementFactory, PersonFactory
from tests.test_genomics_crud import setup  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('value', [
    'x' * 60, 'x' * 61, 'x' * 10000,
    '[note:1]', 'literal [note:9223372036854775807]',
    '[note:99999999999999999999999999]', 'plain [note:broken]',
])
def test_text_boundaries_and_literal_references_round_trip(setup, value):
    person, _, _ = setup
    saved = save_variant(person, {
        'gene': 'TP53', 'variant': value, 'variant_description': value,
    })
    assert saved['variant'] == value
    assert saved['variant_description'] == value
    assert list_variants(person)[0]['variant'] == value
    parent = Measurement.objects.get(pk=saved['id'])
    component = Observation.objects.get(person=person, observation_source_value='genomics:variant_description')
    assert len(parent.value_as_string) <= 60
    assert len(component.value_as_string) <= 60


def test_owned_references_cannot_be_borrowed_by_another_patient_fact_or_context(setup):
    person, _, _ = setup
    saved = save_variant(person, {'gene': 'TP53', 'variant': 'secret' * 30})
    parent = Measurement.objects.get(pk=saved['id'])
    token = parent.value_as_string
    other_patient = MeasurementFactory(person=PersonFactory(), value_as_string=token)
    other_fact = MeasurementFactory(person=person, value_as_string=token)
    reader = OwnedTextReader(person.pk, [parent, other_patient, other_fact])
    assert _read_note_text(parent, parent.pk, reader) == 'secret' * 30
    assert _read_note_text(other_patient, parent.pk, reader) == token
    assert _read_note_text(other_fact, parent.pk, reader) == token
    assert _read_note_text(parent, parent.pk + 1, reader) == token
    # Overlapping IDs in the other CDM table are also a different owner.
    observation = Observation(observation_id=parent.pk, person=person, value_as_string=token)
    assert _read_note_text(observation, parent.pk, reader) == token


def test_api_input_that_copies_an_existing_reference_stays_literal(setup):
    person, _, _ = setup
    first = save_variant(person, {'gene': 'TP53', 'variant_description': 'Private report. ' * 30})
    component = Observation.objects.get(person=person, observation_source_value='genomics:variant_description')
    token = component.value_as_string
    second = save_variant(person, {'gene': 'TP53', 'variant': token, 'variant_description': token})
    assert second['variant'] == token
    assert second['variant_description'] == token
    edited = save_variant(person, {'variant_description': token}, first['id'])
    assert edited['variant_description'] == token


def test_missing_or_malformed_note_keeps_the_source_text(setup):
    person, _, _ = setup
    saved = save_variant(person, {'gene': 'TP53', 'variant': 'Long source ' * 20})
    parent = Measurement.objects.get(pk=saved['id'])
    token = parent.value_as_string
    Note.objects.filter(pk=note_id(token)).delete()
    assert list_variants(person)[0]['variant'] == token
    for value in ('[note:0]', '[note:-1]', '[note:9223372036854775808]', '[note:' + '9' * 500 + ']'):
        parent.value_as_string = value
        with CaptureQueriesContext(connection) as queries:
            reader = OwnedTextReader(person.pk, [parent])
            assert _read_note_text(parent, parent.pk, reader) == value
        assert len(queries) == 0


def test_edits_and_deletion_preserve_note_history(setup):
    person, record, _ = setup
    saved = save_variant(person, {'gene': 'TP53', 'variant_description': 'First version ' * 20})
    old = dict(Note.objects.filter(person=person).values_list('pk', 'note_text'))
    save_variant(person, {'variant_description': 'Second version ' * 20}, saved['id'])
    assert list_variants(person)[0]['variant_description'] == 'Second version ' * 20
    delete_variant(person, saved['id'])
    assert list_variants(person) == []
    record.refresh_from_db()
    assert record.genomics_tp53 == []
    assert dict(Note.objects.filter(pk__in=old).values_list('pk', 'note_text')) == old


def legacy_overflow(person, parent, row, text):
    note = Note.objects.create(
        note_id=next_pk(Note, 'note_id'), person=person,
        note_date=parent.measurement_date, note_type_concept_id=0,
        note_source_value=f'genomics:overflow:{parent.pk}', note_text=text,
    )
    token = f'[note:{note.pk}]'
    row.value_as_string = text[:60 - len(token)] + token
    row.save(update_fields=['value_as_string'])
    return note


def test_unique_legacy_overflow_round_trips_and_edit_uses_owned_notes(setup):
    person, _, _ = setup
    saved = save_variant(person, {'gene': 'TP53', 'variant_description': 'short'})
    parent = Measurement.objects.get(pk=saved['id'])
    row = Observation.objects.get(person=person, observation_source_value='genomics:variant_description')
    text = 'Legacy report, preserved. ' * 20
    old_note = legacy_overflow(person, parent, row, text)
    assert list_variants(person)[0]['variant_description'] == text
    edited = save_variant(person, {'interpretation': 'VUS'}, parent.pk)
    assert edited['variant_description'] == text
    new_row = Observation.objects.get(person=person, observation_source_value='genomics:variant_description', is_erroneous=False)
    note = Note.objects.get(pk=note_id(new_row.value_as_string))
    assert note.note_source_value == f'genomics:observation:{new_row.pk}'
    assert Note.objects.get(pk=old_note.pk).note_text == text


def test_legacy_reference_requires_patient_parent_date_prefix_and_unique_fact(setup):
    person, _, _ = setup
    saved = save_variant(person, {'gene': 'TP53', 'variant': 'short'})
    parent = Measurement.objects.get(pk=saved['id'])
    text = 'Original legacy narrative ' * 20
    legacy_overflow(person, parent, parent, text)
    token = parent.value_as_string
    reader = OwnedTextReader(person.pk, [parent])
    assert _read_note_text(parent, parent.pk, reader) == text
    assert _read_note_text(parent, parent.pk + 1, reader) == token
    other = MeasurementFactory(person=person, value_as_string=token, measurement_date=parent.measurement_date)
    ambiguous = OwnedTextReader(person.pk, [parent, other])
    assert _read_note_text(parent, parent.pk, ambiguous) == token
    assert _read_note_text(other, parent.pk, ambiguous) == token
    parent.value_as_string = 'wrong prefix' + token[token.index('[note:'):]
    assert _read_note_text(parent, parent.pk, reader) == parent.value_as_string
    parent.value_as_string = token
    parent.measurement_date = parent.measurement_date.replace(year=2020)
    assert _read_note_text(parent, parent.pk, reader) == token


def test_note_heavy_projection_has_one_note_query_per_refresh(setup):
    person, _, _ = setup
    for index in range(6):
        save_variant(person, {'gene': 'TP53', 'variant': f'{index} ' * 80,
                              'variant_description': f'Report {index} ' * 50})
    for _ in range(2):
        with CaptureQueriesContext(connection) as queries:
            record = refresh_patient_record(person)
        assert len(record.genetic_mutations) == 6
        note_queries = [q['sql'] for q in queries if 'FROM "note"' in q['sql']]
        assert len(note_queries) == 1, note_queries
        assert all(len(row['variant_description']) > 60 for row in record.genetic_mutations)
