"""Stable, scoped allowed values (#1224): identity, aliases, scope, API, transfer."""
import importlib

import pytest
from django.db import IntegrityError, transaction
from rest_framework.test import APIClient

from omop_core.models import (
    DiseaseCategoryChoice, FieldChoice, FieldChoiceAlias, FieldChoiceCode, FieldChoiceDisease,
)
from omop_core.services import field_choices
from omop_core.services.field_choices import AmbiguousChoice, ChoiceError
from omop_core.services.field_curation_transfer import apply_payload, read_payload
from patient_portal.models import Identity

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def categories():
    for code in ('MM', 'FL', 'MCL', 'Other'):
        DiseaseCategoryChoice.objects.get_or_create(code=code, defaults={'label': code})


def make(display, field='stage', **kwargs):
    return FieldChoice.objects.create(field_name=field, display=display, **kwargs)


# ── Identity ───────────────────────────────────────────────────────────────

def test_code_is_made_from_the_label_and_deduplicated():
    first = make('Stage IV')
    second = make('Stage-IV!')
    third = make('Stage  iv?')
    assert (first.code, second.code, third.code) == ('stage_iv', 'stage_iv_2', 'stage_iv_3')


def test_a_label_with_nothing_to_slug_still_gets_a_code():
    assert make('???').code == 'value'


def test_code_is_scoped_by_field_and_context():
    a = make('I', context_key='iss')
    b = make('I', context_key='ann_arbor')
    c = make('I', field='ecog_status')
    assert (a.code, b.code, c.code) == ('i', 'i', 'i')


def test_rename_keeps_the_code_and_the_old_label_resolves():
    choice = make('Stage IV')
    field_choices.rename(choice, 'Stage 4')
    choice.refresh_from_db()
    assert (choice.display, choice.code) == ('Stage 4', 'stage_iv')
    assert field_choices.resolve('stage', 'Stage IV') == choice
    assert field_choices.resolve('stage', 'stage   4') == choice
    assert choice.aliases.get().source == 'label'


def test_rename_to_another_choices_label_is_refused():
    make('Stage IV')
    other = make('Stage 4')
    with pytest.raises(ChoiceError):
        field_choices.rename(other, 'stage iv')


def test_the_database_refuses_a_duplicate_code_or_label_within_a_context():
    make('Stage IV')
    with pytest.raises(IntegrityError), transaction.atomic():
        FieldChoice.objects.create(field_name='stage', display='Stage IV')
    with pytest.raises(IntegrityError), transaction.atomic():
        FieldChoice.objects.create(field_name='stage', display='Other', code='stage_iv')


def test_same_label_in_two_contexts_is_allowed():
    make('I', context_key='iss')
    make('I', context_key='ann_arbor')
    assert FieldChoice.objects.filter(field_name='stage', display='I').count() == 2


# ── Typed values ───────────────────────────────────────────────────────────

@pytest.mark.parametrize('kind,raw,clean', [
    ('integer', ' 3 ', '3'), ('decimal', '2.50', '2.5'), ('boolean', 'TRUE', 'true'),
    ('text', ' x ', 'x'), ('integer', '', ''),
])
def test_clean_value(kind, raw, clean):
    assert field_choices.clean_value(kind, raw) == clean


@pytest.mark.parametrize('kind,raw', [
    ('integer', '3.5'), ('decimal', 'abc'), ('decimal', 'NaN'), ('boolean', 'yes'),
])
def test_clean_value_refuses_what_the_type_cannot_hold(kind, raw):
    with pytest.raises(ChoiceError):
        field_choices.clean_value(kind, raw)


def test_stored_value_falls_back_to_the_label():
    assert make('Stage IV').stored_value == 'Stage IV'
    assert make('Three', value='3', value_type='integer').stored_value == '3'


# ── Aliases ────────────────────────────────────────────────────────────────

def test_aliases_are_folded_and_replace_the_set():
    choice = make('Stage IV')
    field_choices.set_aliases(choice, ['4', ' IV  ', 'iv'])
    assert sorted(choice.aliases.values_list('alias_key', flat=True)) == ['4', 'iv']
    field_choices.set_aliases(choice, ['four'])
    assert list(choice.aliases.values_list('alias_key', flat=True)) == ['four']


def test_an_alias_another_choice_answers_to_is_refused_by_name():
    a = make('Stage IV')
    b = make('Stage III')
    field_choices.set_aliases(a, ['4'])
    with pytest.raises(ChoiceError, match="already means 'Stage IV'"):
        field_choices.set_aliases(b, ['4'])
    with pytest.raises(ChoiceError, match="already means 'Stage IV'"):
        field_choices.set_aliases(b, ['stage iv'])
    with pytest.raises(ChoiceError):
        field_choices.set_aliases(b, ['STAGE_IV'])  # another choice's code


def test_the_same_alias_in_another_context_is_not_a_clash():
    a = make('I', context_key='iss')
    b = make('I', context_key='ann_arbor')
    field_choices.set_aliases(a, ['one'])
    field_choices.set_aliases(b, ['one'])


def test_earlier_labels_survive_an_alias_replacement():
    choice = make('Stage IV')
    field_choices.rename(choice, 'Stage 4')
    field_choices.set_aliases(choice, [])
    assert [a.alias for a in choice.aliases.all()] == ['Stage IV']


def test_the_database_holds_one_spelling_per_choice_scope():
    a = make('A')
    FieldChoiceAlias.objects.create(choice=a, field_name='stage', context_key='', alias='x', alias_key='x')
    b = make('B')
    with pytest.raises(IntegrityError), transaction.atomic():
        FieldChoiceAlias.objects.create(choice=b, field_name='stage', context_key='', alias='X', alias_key='x')


# ── Scope ──────────────────────────────────────────────────────────────────

def test_set_diseases_validates_against_the_categories():
    choice = make('I')
    assert field_choices.set_diseases(choice, ['MM', 'MM']) == ['MM']
    with pytest.raises(ChoiceError):
        field_choices.set_diseases(choice, ['NOPE'])
    with pytest.raises(ChoiceError):
        field_choices.set_diseases(choice, ['Other'])
    assert field_choices.set_diseases(choice, []) == []
    assert not FieldChoiceDisease.objects.exists()


def test_offered_follows_the_records_disease_and_leaves_out_retired():
    shared = make('Unknown')
    iss = make('ISS I', context_key='iss')
    field_choices.set_diseases(iss, ['MM'])
    lugano = make('Lugano I', context_key='lugano')
    field_choices.set_diseases(lugano, ['FL', 'MCL'])
    gone = make('Old')
    field_choices.set_retired(gone, True)

    assert set(field_choices.offered('stage', 'MM')) == {shared, iss}
    assert set(field_choices.offered('stage', 'FL')) == {shared, lugano}
    assert set(field_choices.offered('stage', None)) == {shared}
    assert set(field_choices.offered('stage', 'MM', context_key='iss')) == {iss}
    field_choices.set_retired(gone, False)
    assert gone in field_choices.offered('stage', 'MM')


def test_resolve_picks_by_disease_and_refuses_to_guess():
    iss = make('I', context_key='iss')
    field_choices.set_diseases(iss, ['MM'])
    arbor = make('I', context_key='ann_arbor')
    field_choices.set_diseases(arbor, ['FL'])
    assert field_choices.resolve('stage', 'I', 'MM') == iss
    assert field_choices.resolve('stage', 'I', 'FL') == arbor
    assert field_choices.resolve('stage', 'I', 'MCL') is None
    field_choices.set_diseases(arbor, ['MM', 'FL'])
    with pytest.raises(AmbiguousChoice):
        field_choices.resolve('stage', 'I', 'MM')


def test_resolve_matches_code_value_and_label_and_can_skip_retired():
    choice = make('Stage Three', value='3', value_type='integer')
    for text in ('stage three', 'stage_three', '3'):
        assert field_choices.resolve('stage', text) == choice
    field_choices.set_retired(choice, True)
    assert field_choices.resolve('stage', '3') == choice
    assert field_choices.resolve('stage', '3', include_retired=False) is None
    assert field_choices.resolve('stage', '  ') is None


# ── Migration backfill ─────────────────────────────────────────────────────

def test_backfill_leaves_existing_codes_and_fills_blanks():
    from django.apps import apps
    module = importlib.import_module('omop_core.migrations.0365_scoped_field_choices')
    kept = make('Stage IV')                       # code stage_iv
    blank = make('Stage  IV')
    FieldChoice.objects.filter(pk=blank.pk).update(code='')
    module.backfill_codes(apps, None)
    kept.refresh_from_db()
    blank.refresh_from_db()
    assert kept.code == 'stage_iv'
    assert blank.code == 'stage_iv_2'
    module.backfill_codes(apps, None)             # a replay changes nothing
    blank.refresh_from_db()
    assert blank.code == 'stage_iv_2'


# ── API ────────────────────────────────────────────────────────────────────

@pytest.fixture
def staff():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='scoped-choices@example.com', is_staff=True))
    return client


@pytest.fixture
def reader():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='scoped-reader@example.com'))
    return client


def test_create_returns_identity_scope_and_the_legacy_shape(staff):
    response = staff.post('/api/v1/field-choices/', {
        'field_name': 'stage', 'display': 'ISS I', 'sort_order': 1, 'context_key': 'iss',
        'aliases': ['Stage 1'], 'diseases': ['MM'],
    }, format='json')
    assert response.status_code == 201, response.data
    body = response.json()
    assert body['code'] == 'iss_i'
    assert body['aliases'] == ['Stage 1']
    assert body['diseases'] == ['MM']
    assert body['retired'] is False
    assert {'id', 'field_name', 'display', 'sort_order', 'codes'} <= body.keys()


def test_an_old_client_payload_still_creates_a_choice(staff):
    response = staff.post('/api/v1/field-choices/', {
        'field_name': 'stage', 'display': 'Stage V', 'sort_order': 5,
        'codes': [{'code': '1', 'vocabulary_id': 'LOCAL', 'is_primary': True}],
    }, format='json')
    assert response.status_code == 201
    assert FieldChoice.objects.get(display='Stage V').codes.count() == 1


def test_only_mapping_curators_write(reader):
    response = reader.post('/api/v1/field-choices/', {'field_name': 'stage', 'display': 'X'}, format='json')
    assert response.status_code == 403
    make('Y')
    assert reader.get('/api/v1/field-choices/?field_name=stage').status_code == 200


def test_duplicate_label_and_code_are_400_with_a_reason(staff):
    make('Stage IV')
    r = staff.post('/api/v1/field-choices/', {'field_name': 'stage', 'display': 'Stage IV'}, format='json')
    assert r.status_code == 400 and 'display' in r.json()
    r = staff.post('/api/v1/field-choices/',
                   {'field_name': 'stage', 'display': 'Other', 'code': 'stage_iv'}, format='json')
    assert r.status_code == 400 and 'code' in r.json()
    r = staff.post('/api/v1/field-choices/',
                   {'field_name': 'stage', 'display': 'Q', 'code': 'Not Valid'}, format='json')
    assert r.status_code == 400 and 'code' in r.json()


def test_bad_scope_alias_and_value_are_400_and_write_nothing(staff):
    a = make('Stage IV')
    r = staff.post('/api/v1/field-choices/', {
        'field_name': 'stage', 'display': 'Z', 'diseases': ['NOPE']}, format='json')
    assert r.status_code == 400
    r = staff.post('/api/v1/field-choices/', {
        'field_name': 'stage', 'display': 'Z', 'aliases': ['stage iv']}, format='json')
    assert r.status_code == 400
    r = staff.post('/api/v1/field-choices/', {
        'field_name': 'stage', 'display': 'Z', 'value_type': 'integer', 'value': 'x'}, format='json')
    assert r.status_code == 400
    assert FieldChoice.objects.filter(display='Z').count() == 0
    assert FieldChoice.objects.get(pk=a.pk).aliases.count() == 0


def test_patch_renames_keeping_code_and_alias_and_refuses_code_changes(staff):
    choice = make('Stage IV')
    r = staff.patch(f'/api/v1/field-choices/{choice.pk}/', {'display': 'Stage 4'}, format='json')
    assert r.status_code == 200, r.data
    assert r.json()['code'] == 'stage_iv'
    assert r.json()['aliases'] == ['Stage IV']
    r = staff.patch(f'/api/v1/field-choices/{choice.pk}/', {'code': 'other'}, format='json')
    assert r.status_code == 400
    r = staff.patch(f'/api/v1/field-choices/{choice.pk}/', {'context_key': 'x'}, format='json')
    assert r.status_code == 400
    r = staff.patch(f'/api/v1/field-choices/{choice.pk}/', {'field_name': 'ecog_status'}, format='json')
    assert r.status_code == 400


def test_patch_scope_retire_and_unrelated_edit_leaves_them(staff):
    choice = make('I', context_key='iss')
    r = staff.patch(f'/api/v1/field-choices/{choice.pk}/',
                    {'diseases': ['MM'], 'retired': True, 'aliases': ['one']}, format='json')
    assert r.status_code == 200
    assert (r.json()['diseases'], r.json()['retired']) == (['MM'], True)
    r = staff.patch(f'/api/v1/field-choices/{choice.pk}/', {'sort_order': 9}, format='json')
    assert r.json()['diseases'] == ['MM'] and r.json()['aliases'] == ['one']
    r = staff.patch(f'/api/v1/field-choices/{choice.pk}/', {'retired': False, 'diseases': []}, format='json')
    assert (r.json()['diseases'], r.json()['retired']) == ([], False)


def test_list_filters_by_the_records_disease(staff):
    shared = make('Unknown')
    iss = make('ISS I', context_key='iss')
    field_choices.set_diseases(iss, ['MM'])
    lugano = make('Lugano I', context_key='lugano')
    field_choices.set_diseases(lugano, ['FL'])
    gone = make('Old')
    field_choices.set_retired(gone, True)

    def shown(query):
        r = staff.get(f'/api/v1/field-choices/?field_name=stage{query}')
        assert r.status_code == 200
        return {c['display'] for c in r.json()}

    assert shown('') == {'Unknown', 'ISS I', 'Lugano I', 'Old'}
    assert shown('&disease=MM') == {'Unknown', 'ISS I'}
    assert shown('&disease=FL') == {'Unknown', 'Lugano I'}
    assert shown('&disease=MCL') == {'Unknown'}
    assert shared.pk


def test_the_descriptor_carries_identity_and_scope_without_dropping_the_old_keys():
    from omop_core.services.field_descriptor import get_all_field_descriptors
    iss = make('ISS I', context_key='iss', sort_order=1)
    field_choices.set_diseases(iss, ['MM'])
    descriptors = get_all_field_descriptors()
    stage = next(d for d in descriptors if d['field_name'] == 'stage')
    entry = next(c for c in stage['choices'] if c['id'] == iss.pk)
    assert {'id', 'display', 'sort_order', 'codes'} <= entry.keys()
    assert (entry['code'], entry['context_key'], entry['diseases'], entry['retired']) == (
        'iss_i', 'iss', ['MM'], False)


def test_write_options_list_an_answer_once_across_contexts():
    from omop_core.services.write_descriptor import _field_choice_options
    make('I', context_key='iss')
    make('I', context_key='ann_arbor')
    assert [d for d, _ in _field_choice_options()['stage']] == ['I']


# ── Curation transfer ──────────────────────────────────────────────────────

def _wipe():
    FieldChoice.objects.all().delete()


def test_transfer_round_trip_keeps_identity_scope_and_spellings():
    iss = make('ISS I', context_key='iss', sort_order=1, value='1', value_type='integer')
    field_choices.set_diseases(iss, ['MM'])
    field_choices.rename(iss, 'ISS stage I')
    FieldChoiceCode.objects.create(choice=iss, code='1', vocabulary_id='LOCAL', is_primary=True)
    gone = make('Gone')
    field_choices.set_retired(gone, True)
    payload = read_payload('default')

    _wipe()
    apply_payload(payload)

    back = FieldChoice.objects.get(field_name='stage', code='iss_i')
    assert (back.display, back.context_key, back.value, back.value_type) == ('ISS stage I', 'iss', '1', 'integer')
    assert [d.disease for d in back.disease_scope.all()] == ['MM']
    assert [a.alias for a in back.aliases.all()] == ['ISS I']
    assert back.codes.count() == 1
    assert FieldChoice.objects.get(display='Gone').retired_at is not None


def test_transfer_follows_a_rename_by_code_instead_of_adding_a_second_choice():
    choice = make('Stage IV')
    payload = read_payload('default')
    for row in payload['choices']:
        row['display'] = 'Stage 4'
    apply_payload(payload)
    assert FieldChoice.objects.filter(field_name='stage').count() == 1
    assert FieldChoice.objects.get(pk=choice.pk).display == 'Stage 4'


def test_transfer_reads_a_payload_from_before_scoped_choices():
    legacy = {'choices': [{
        'field_name': 'stage', 'display': 'Stage III', 'sort_order': 3,
        'codes': [{'code': '261641005', 'vocabulary_id': 'SNOMED', 'display': 'Stage 3', 'is_primary': True}],
    }]}
    apply_payload(legacy)
    apply_payload(legacy)                       # and again: it matches by display
    choice = FieldChoice.objects.get(field_name='stage')
    assert (choice.code, choice.context_key) == ('stage_iii', '')
    assert choice.codes.count() == 1


def test_transfer_ignores_a_disease_this_instance_lacks():
    payload = {'choices': [{
        'field_name': 'stage', 'display': 'X', 'sort_order': 1, 'codes': [],
        'diseases': ['MM', 'NOT_HERE'],
    }]}
    apply_payload(payload)
    assert [d.disease for d in FieldChoice.objects.get(display='X').disease_scope.all()] == ['MM']


# ── Review findings ────────────────────────────────────────────────────────

def test_transfer_rename_and_reused_label_in_one_payload():
    make('Foo')                                   # code foo
    payload = {'choices': [
        {'field_name': 'stage', 'display': 'Bar', 'sort_order': 1, 'codes': [], 'code': 'foo'},
        {'field_name': 'stage', 'display': 'Foo', 'sort_order': 2, 'codes': [], 'code': 'c'},
    ]}
    apply_payload(payload)
    rows = {c.code: c.display for c in FieldChoice.objects.filter(field_name='stage')}
    assert rows == {'foo': 'Bar', 'c': 'Foo'}


def test_transfer_swapping_two_labels_does_not_trip_the_label_rule():
    x, y = make('X'), make('Y')
    payload = {'choices': [
        {'field_name': 'stage', 'display': 'Y', 'sort_order': 1, 'codes': [], 'code': x.code},
        {'field_name': 'stage', 'display': 'X', 'sort_order': 2, 'codes': [], 'code': y.code},
    ]}
    apply_payload(payload)
    assert FieldChoice.objects.get(pk=x.pk).display == 'Y'
    assert FieldChoice.objects.get(pk=y.pk).display == 'X'


def test_transfer_adopts_a_choice_whose_code_this_instance_numbered_differently():
    local = make('Stage IV')
    FieldChoice.objects.filter(pk=local.pk).update(code='stage_iv_2')
    apply_payload({'choices': [
        {'field_name': 'stage', 'display': 'Stage IV', 'sort_order': 1, 'codes': [], 'code': 'stage_iv'},
    ]})
    assert FieldChoice.objects.filter(field_name='stage').count() == 1
    assert FieldChoice.objects.get(pk=local.pk).code == 'stage_iv'


def test_transfer_keeps_when_a_choice_was_retired():
    from datetime import timedelta
    from django.utils import timezone
    choice = make('Old')
    field_choices.set_retired(choice, True)
    long_ago = timezone.now() - timedelta(days=30)
    FieldChoice.objects.filter(pk=choice.pk).update(retired_at=long_ago)
    apply_payload(read_payload('default'))
    assert FieldChoice.objects.get(pk=choice.pk).retired_at == long_ago


def test_transfer_reports_a_value_its_type_cannot_hold():
    stats = apply_payload({'choices': [{
        'field_name': 'stage', 'display': 'Bad', 'sort_order': 1, 'codes': [],
        'value_type': 'integer', 'value': 'abc'}]})
    assert any('Bad' in w for w in stats.warnings)
    assert FieldChoice.objects.get(display='Bad').value == ''


def test_create_refuses_a_label_that_is_another_choices_earlier_label(staff):
    choice = make('Stage IV')
    field_choices.rename(choice, 'Stage 4')
    r = staff.post('/api/v1/field-choices/', {'field_name': 'stage', 'display': 'stage iv'}, format='json')
    assert r.status_code == 400 and 'display' in r.json()
    assert field_choices.resolve('stage', 'Stage IV') == choice


def test_unknown_disease_filter_is_a_400(staff):
    assert staff.get('/api/v1/field-choices/?field_name=stage&disease=NOPE').status_code == 400
    assert staff.get('/api/v1/field-choices/?field_name=stage&disease=Other').status_code == 200


def test_a_save_that_loses_a_race_is_a_409_not_a_500(staff, monkeypatch):
    from patient_portal.api import serializers

    def lose(self, validated_data):
        raise IntegrityError('duplicate key value violates unique constraint')
    monkeypatch.setattr(serializers.FieldChoiceSerializer, 'create', lose)
    r = staff.post('/api/v1/field-choices/', {'field_name': 'stage', 'display': 'New'}, format='json')
    assert r.status_code == 409
