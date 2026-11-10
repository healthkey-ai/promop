"""The source code list's "with notes" filter and curator-note marker (#2423)."""
import importlib

import pytest
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from omop_core.models import SourceCodeConceptMapping
from patient_portal.api.views import (
    code_mapping_detail, code_mapping_group, code_mapping_list,
)
from patient_portal.models import Identity
from tests.factories import ConceptFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def curator():
    return Identity.objects.create_user(email='notes@example.test', is_staff=True)


@pytest.fixture
def browse(curator):
    def get(**params):
        request = APIRequestFactory().get('/api/v1/code-mappings/', {'browse': '1', **params})
        force_authenticate(request, user=curator)
        return code_mapping_list(request)
    return get


def row(code, **kwargs):
    return SourceCodeConceptMapping.objects.create(source_code=code, **{
        'source_vocabulary_id': 'ICD10CM', 'status': 'proposed', 'occurrence_count': 5, **kwargs})


def noted(code, text='Waiting on the lab to confirm the specimen.', **kwargs):
    return row(code, notes=text, curator_note_at=timezone.now(), **kwargs)


def save(curator, mapping, **data):
    request = APIRequestFactory().patch(f'/api/v1/code-mappings/{mapping.pk}/', data, format='json')
    force_authenticate(request, user=curator)
    response = code_mapping_detail(request, mapping_id=mapping.pk)
    assert response.status_code == 200, response.data
    mapping.refresh_from_db()
    return response


def test_filter_is_off_by_default_and_keeps_only_curator_notes(browse):
    noted('NOTED')
    row('MACHINE', notes='high confidence: Source and candidate name the same test.')
    row('BLANK')
    # A stamp over a note that says nothing is not a note.
    row('SPACES', notes=' \n\t', curator_note_at=timezone.now())

    everything = browse(order_0='source_code').data
    assert [r['source_code'] for r in everything['results']] == ['BLANK', 'MACHINE', 'NOTED', 'SPACES']
    assert {r['source_code']: r['has_curator_note'] for r in everything['results']} == {
        'BLANK': False, 'MACHINE': False, 'NOTED': True, 'SPACES': False}

    filtered = browse(with_notes='1').data
    assert [r['source_code'] for r in filtered['results']] == ['NOTED']
    assert browse(with_notes='0').data['pages']['Unmapped']['total'] == 4
    assert browse(with_notes='yes').status_code == 400


def test_filter_narrows_every_section_and_its_totals_and_pages(browse):
    concept = ConceptFactory()
    for i in range(3):
        noted(f'OPEN-{i}')
    for i in range(30):
        row(f'QUIET-{i}')
    noted('DONE', status='approved', target_concept=concept, occurrence_count=0)
    row('DONE-QUIET', status='approved', target_concept=concept)

    data = browse(with_notes='1', seen_only='1', order_0='source_code').data
    assert data['pages']['Unmapped']['total'] == 3
    assert data['pages']['Unmapped'].get('next') is None
    # Seen > 0 still narrows only Unmapped, so the approved note is listed.
    assert sorted(r['source_code'] for r in data['results']) == ['DONE', 'OPEN-0', 'OPEN-1', 'OPEN-2']
    # The tab strip keeps counting the whole tab.
    assert data['tabs'][0]['proposed'] == 33


def test_filter_composes_with_search_provenance_and_rollup(browse, curator):
    noted('A-1', source_code_description='Glucose', origin_system='curator')
    noted('A-2', source_code_description='Glucose', origin_system='suggest v0.4')
    row('A-3', source_code_description='Glucose', origin_system='curator')

    assert [r['source_code'] for r in browse(with_notes='1', provenance='curator').data['results']] == ['A-1']
    assert sorted(r['source_code'] for r in browse(with_notes='1', search='glucose').data['results']) == ['A-1', 'A-2']

    rolled = browse(with_notes='1', rollup='1', queue_sections='1').data
    [group] = rolled['groups']['Unmapped']
    assert group['members'] == 2
    request = APIRequestFactory().get('/api/v1/code-mappings/group/', {
        'label': group['label'], 'section': 'Unmapped', 'queue_sections': '1', 'with_notes': '1'})
    force_authenticate(request, user=curator)
    members = code_mapping_group(request).data['results']
    assert sorted(r['source_code'] for r in members) == ['A-1', 'A-2']


def test_a_curator_save_marks_a_note_only_when_the_text_changes(curator):
    machine = 'medium confidence: Source text is generic.'
    mapping = row('SAVE', notes=machine)

    # The dialog sends the note back untouched on every save.
    save(curator, mapping, notes=machine, source_code_description='Renamed')
    assert mapping.curator_note_at is None

    response = save(curator, mapping, notes='  Ask Samar about the specimen.  ')
    assert mapping.notes == 'Ask Samar about the specimen.'
    assert mapping.curator_note_at is not None
    assert response.data['has_curator_note'] is True
    stamped = mapping.curator_note_at

    save(curator, mapping, notes='Ask Samar about the specimen.')
    assert mapping.curator_note_at == stamped
    # A save that does not carry the note leaves it and its mark alone.
    save(curator, mapping, source_code_description='Renamed again')
    assert (mapping.notes, mapping.curator_note_at) == ('Ask Samar about the specimen.', stamped)

    response = save(curator, mapping, notes='   ')
    assert (mapping.notes, mapping.curator_note_at) == ('', None)
    assert response.data['has_curator_note'] is False


def test_a_new_mapping_with_a_typed_note_is_marked(curator):
    concept = ConceptFactory()
    request = APIRequestFactory().post('/api/v1/code-mappings/', {
        'source_vocabulary_id': 'ICD10CM', 'source_code': 'NEW-1', 'notes': 'Seen only at one site.',
        'destination_concept_id': concept.concept_id,
    }, format='json')
    force_authenticate(request, user=curator)
    response = code_mapping_list(request)
    assert response.status_code == 201, response.data
    assert SourceCodeConceptMapping.objects.get(source_code='NEW-1').curator_note_at is not None

    request = APIRequestFactory().post('/api/v1/code-mappings/', {
        'source_vocabulary_id': 'ICD10CM', 'source_code': 'NEW-2',
        'destination_concept_id': concept.concept_id,
    }, format='json')
    force_authenticate(request, user=curator)
    assert code_mapping_list(request).status_code == 201
    assert SourceCodeConceptMapping.objects.get(source_code='NEW-2').curator_note_at is None


def test_a_machine_write_through_the_curator_writer_is_not_a_curator_note(curator):
    from patient_portal.api.views import _upsert_source_code_mapping

    mapping, _ = _upsert_source_code_mapping(ConceptFactory(), {
        'source_vocabulary_id': 'ICD10CM', 'source_code': 'AUTO',
        'notes': 'Source vocabulary label: Glucose',
    }, curator, record_decision=False)
    assert mapping.notes and mapping.curator_note_at is None


@pytest.mark.parametrize(('note', 'by_curator', 'marked'), [
    ('Reviewed three patient examples for this code.', True, True),
    ('The SPEP is not mentioned literally in the report.', True, True),
    ('High confidence: Source and candidate agree.', True, False),
    ('medium confidence (Jev 71%): close match', True, False),
    ('Jev wins (90% vs Anthropic declined). Exact name.', True, False),
    ('Best-match fallback (score 0.41).', True, False),
    ('No candidate concept found by any enabled strategy.', True, False),
    ('Athena recovery snapshot 2026-05-01', True, False),
    ('Unambiguous extractor alias for albumin.', True, False),
    ('  \n ', True, False),
    # Nobody but a machine has ever written this row.
    ('Check with the lab.', False, False),
])
def test_backfill_marks_only_notes_a_curator_wrote(curator, note, by_curator, marked):
    mapping = row('OLD', notes=note, updated_by=curator if by_curator else None)
    migration = importlib.import_module('omop_core.migrations.0334_sccm_curator_note')

    class Editor:
        pass
    Editor.connection = connection
    migration.mark_curator_notes(None, Editor)

    mapping.refresh_from_db()
    assert (mapping.curator_note_at is not None) is marked
    if marked:
        assert mapping.curator_note_at == mapping.updated_at


def test_concept_to_code_source_list_filters_by_curator_note_before_it_counts(curator):
    from rest_framework.test import APIClient

    concept = ConceptFactory()
    noted('C-NOTED', target_concept=concept)
    noted('C-NOTED-UNSEEN', target_concept=concept, occurrence_count=0)
    noted('C-APPROVED', target_concept=concept, status='approved')
    row('C-MACHINE', target_concept=concept, notes='low confidence: No candidate fits.')
    client = APIClient()
    client.force_authenticate(curator)
    url = f'/api/v1/concept-to-code/{concept.pk}/'

    everything = client.get(url, {'seen_only': '1'}).data
    assert everything['total'] == 3
    assert {r['source_code']: r['curator_note'] for r in everything['results']} == {
        'C-NOTED': 'Waiting on the lab to confirm the specimen.',
        'C-APPROVED': 'Waiting on the lab to confirm the specimen.',
        'C-MACHINE': '',
    }

    filtered = client.get(url, {'seen_only': '1', 'with_notes': '1'}).data
    assert sorted(r['source_code'] for r in filtered['results']) == ['C-APPROVED', 'C-NOTED']
    # The counts describe what the filter leaves, not the whole concept.
    assert (filtered['total'], filtered['zero_seen']) == (2, 1)
    assert filtered['status_counts'] == {'proposed': 1, 'approved': 1, 'rejected': 0}
    assert client.get(url, {'with_notes': 'on'}).status_code == 400
