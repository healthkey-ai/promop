from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from omop_core.models import FieldChoice, FieldValueConceptMapping, Measurement
from omop_core.services.field_values import ValueResolver, save_mapping, validate_choice
from omop_core.services.omop_projection import CLEAR_VALUE, project_single_value
from tests.factories import ConceptFactory, DomainFactory, PersonFactory

pytestmark = pytest.mark.django_db


def answer(**kwargs):
    return ConceptFactory(domain=DomainFactory(domain_id='Meas Value'), **kwargs)


def mapped_choice(field='her2_status', value='Positive', **kwargs):
    choice = FieldChoice.objects.create(field_name=field, display=value, canonical_value=value, **kwargs)
    concept = answer()
    save_mapping(choice, {'target_concept': concept, 'status': 'approved', 'outcome': 'mapped',
        'notes': 'Reviewed exact answer meaning.', 'vocabulary_release': 'Athena test release'})
    return choice, concept


def test_stable_identity_and_typed_values():
    choice = FieldChoice.objects.create(field_name='biopsy_grade', display='Grade 1', canonical_value=1)
    code = choice.code
    choice.display = 'Grade one'
    choice.save()
    choice.refresh_from_db()
    assert choice.code == code
    assert choice.canonical_value == 1


def test_coded_projection_readback_and_clear():
    choice, concept = mapped_choice(aliases=['pos'])
    question = ConceptFactory()
    person = PersonFactory()
    recipe = {'concept_id': question.pk, 'omop_table': 'measurement', 'source_value': question.concept_code, 'value_kind': 'string'}
    assert project_single_value(person, choice.field_name, 'pos', recipe)
    row = Measurement.objects.get(person=person, measurement_concept=question)
    assert row.value_as_concept_id == concept.pk
    assert row.value_source_value == 'pos'
    row.value_as_string = None
    assert ValueResolver().reverse(choice.field_name, row) == 'Positive'
    assert project_single_value(person, choice.field_name, None, recipe)
    row.refresh_from_db()
    assert row.value_as_concept_id is None
    assert row.value_source_value == CLEAR_VALUE


def test_unmapped_choice_remains_text_and_no_equivalent_can_be_reviewed():
    choice = FieldChoice.objects.create(field_name='her2_status', display='Other')
    mapping = save_mapping(choice, {'status': 'approved', 'outcome': 'no_equivalent', 'notes': 'Reviewed source term is not a clinical result.'})
    assert mapping.target_concept_id is None
    assert ValueResolver().mapping(choice) is None
    question, person = ConceptFactory(), PersonFactory()
    assert project_single_value(person, 'her2_status', 'Other', {'concept_id': question.pk,
        'omop_table': 'measurement', 'source_value': question.concept_code, 'value_kind': 'string'})
    row = Measurement.objects.get(person=person, measurement_concept=question)
    assert row.value_as_string == 'Other'
    assert row.value_as_concept_id is None


@pytest.mark.parametrize('change', [
    {'standard_concept': None}, {'invalid_reason': 'D'}, {'source': 'HealthKey'},
    {'valid_end_date': '2000-01-01'}, {'concept_id': 2100000200},
])
def test_reject_invalid_approved_targets(change):
    choice = FieldChoice.objects.create(field_name='her2_status', display='Positive')
    with pytest.raises(ValidationError):
        save_mapping(choice, {'target_concept': answer(**change), 'status': 'approved', 'outcome': 'mapped',
            'notes': 'Review', 'vocabulary_release': 'test'})
    assert not FieldValueConceptMapping.objects.filter(choice=choice).exists()


def test_answer_cannot_be_question_or_ambiguous_approval():
    choice = FieldChoice.objects.create(field_name='her2_status', display='Positive')
    with pytest.raises(ValidationError):
        save_mapping(choice, {'target_concept': ConceptFactory(), 'status': 'approved',
            'outcome': 'mapped', 'notes': 'Review', 'vocabulary_release': 'test'})
    with pytest.raises(ValidationError):
        save_mapping(choice, {'status': 'approved', 'outcome': 'ambiguous', 'notes': 'Two meanings'})


def test_mapping_revision_history_and_runtime_revalidation():
    choice, concept = mapped_choice()
    mapping = save_mapping(choice, {'status': 'rejected', 'notes': 'Wrong answer in this context'})
    assert list(mapping.revisions.order_by('revision').values_list('decision__status', flat=True)) == ['approved', 'rejected']
    assert ValueResolver().mapping(ValueResolver().resolve(choice.field_name, 'Positive')) is None
    save_mapping(choice, {'status': 'approved', 'notes': 'Re-reviewed'})
    concept.valid_end_date = timezone.localdate() - timedelta(days=1)
    concept.save()
    resolver = ValueResolver()
    assert resolver.mapping(resolver.resolve(choice.field_name, 'Positive')) is None


def test_context_and_alias_collisions():
    mapped_choice(context_key='BC:clinical', aliases=['pos'])
    mapped_choice(context_key='BC:pathological', aliases=['pos'])
    resolver = ValueResolver()
    assert resolver.resolve('her2_status', 'pos') is None
    assert resolver.resolve('her2_status', 'pos', 'BC:clinical')
    with pytest.raises(ValidationError):
        validate_choice(FieldChoice(field_name='her2_status', context_key='BC:clinical', display='Another', aliases=['POS']))


def test_shared_concept_reverse_requires_unambiguous_choice():
    first, concept = mapped_choice()
    second = FieldChoice.objects.create(field_name=first.field_name, display='Low positive')
    save_mapping(second, {'target_concept': concept, 'status': 'approved', 'outcome': 'mapped',
        'notes': 'Source distinguishes level.', 'vocabulary_release': 'test'})
    row = Measurement(value_as_concept=concept, value_as_string='raw ambiguous', value_as_number=None)
    assert ValueResolver().reverse(first.field_name, row) == 'raw ambiguous'


def test_mapping_api_permissions_and_audit():
    from rest_framework.test import APIClient
    from patient_portal.models import Identity
    choice = FieldChoice.objects.create(field_name='her2_status', display='Unknown')
    user = Identity.objects.create_user(email='value@example.test', password='test', is_staff=True)
    client = APIClient()
    url = f'/api/v1/field-choices/{choice.pk}/mapping/'
    assert client.get(url).status_code in (401, 403)
    client.force_authenticate(user)
    response = client.patch(url, {'status': 'approved', 'outcome': 'no_equivalent', 'notes': 'Reviewed unmapped source meaning.'}, format='json')
    assert response.status_code == 200, response.data
    assert response.data['reviewer_id'] == user.pk
    assert len(client.get(url).data['history']) == 1
    user.is_staff = False
    user.save()
    assert client.patch(url, {'status': 'rejected'}, format='json').status_code == 403


def test_choice_rename_keeps_alias_and_rejects_identity_change():
    from rest_framework.test import APIClient
    from patient_portal.models import Identity
    user = Identity.objects.create_user(email='choice@example.test', password='test', is_staff=True)
    client = APIClient()
    client.force_authenticate(user)
    response = client.post('/api/v1/field-choices/', {'field_name': 'her2_status', 'display': 'Positive'}, format='json')
    assert response.status_code == 201, response.data
    url = f"/api/v1/field-choices/{response.data['id']}/"
    code = response.data['code']
    updated = client.patch(url, {'display': 'Detected'}, format='json')
    assert updated.status_code == 200, updated.data
    assert updated.data['code'] == code
    assert updated.data['canonical_value'] == 'Positive'
    assert 'Positive' in updated.data['aliases']
    assert client.patch(url, {'code': 'replacement'}, format='json').status_code == 400


def test_stale_choice_update_keeps_concurrent_rename_and_retirement():
    from patient_portal.api.serializers import FieldChoiceSerializer
    choice = FieldChoice.objects.create(field_name='her2_status', display='Positive')
    stale = FieldChoice.objects.get(pk=choice.pk)
    first = FieldChoiceSerializer(choice, data={'display': 'Detected', 'retired': True}, partial=True)
    first.is_valid(raise_exception=True)
    first.save()
    second = FieldChoiceSerializer(stale, data={'sort_order': 9}, partial=True)
    second.is_valid(raise_exception=True)
    second.save()
    choice.refresh_from_db()
    assert choice.display == 'Detected'
    assert choice.retired
    assert choice.aliases == ['Positive']
    assert choice.sort_order == 9


def test_long_source_alias_is_preserved_and_retry_reuses_its_note():
    from omop_core.models import Note
    raw = 'Positive (' + 'full laboratory source wording ' * 10 + ')'
    choice, concept = mapped_choice(aliases=[raw])
    question, person = ConceptFactory(), PersonFactory()
    recipe = {'concept_id': question.pk, 'source_value': question.concept_code,
              'omop_table': 'measurement', 'value_kind': 'string'}
    assert project_single_value(person, choice.field_name, raw, recipe)
    row = Measurement.objects.get(person=person)
    note = Note.objects.get(person=person, note_source_value=f'field-answer:measurement:{row.pk}')
    assert row.value_as_concept_id == concept.pk
    assert row.value_source_value == f'[note:{note.pk}]'
    assert note.note_text == raw
    assert project_single_value(person, choice.field_name, raw, recipe, acknowledge_existing=True)
    assert Note.objects.filter(person=person).count() == 1


def test_question_override_is_cleared_with_the_field():
    choice, _ = mapped_choice()
    question = ConceptFactory(domain=DomainFactory(domain_id='Measurement'))
    save_mapping(choice, {'question_concept': question})
    base = ConceptFactory()
    person = PersonFactory()
    recipe = {'concept_id': base.pk, 'source_value': base.concept_code, 'omop_table': 'measurement', 'value_kind': 'string'}
    assert project_single_value(person, choice.field_name, 'Positive', recipe)
    assert Measurement.objects.get(person=person).measurement_concept_id == question.pk
    assert project_single_value(person, choice.field_name, None, recipe)
    assert not Measurement.objects.filter(person=person, value_as_concept__isnull=False).exists()
    assert set(Measurement.objects.filter(person=person).values_list('value_source_value', flat=True)) == {CLEAR_VALUE}


def test_histology_question_override_survives_builtin_reader_selection():
    from datetime import date
    from omop_core.models import FieldConceptMapping
    from omop_core.services.patient_record_service import refresh_patient_record
    from tests.factories import MeasurementFactory, ObservationFactory
    choice, concept = mapped_choice(field='histologic_type', value='Reviewed histology')
    default = ConceptFactory(vocabulary__vocabulary_id='LOINC', concept_code='59847-4')
    override = ConceptFactory(domain=DomainFactory(domain_id='Measurement'))
    save_mapping(choice, {'question_concept': override})
    FieldConceptMapping.objects.update_or_create(field_name='histologic_type', defaults={
        'concept': default, 'vocabulary_id': 'LOINC', 'concept_code': default.concept_code,
        'source_value': default.concept_code, 'status': 'approved', 'omop_table': 'measurement', 'value_kind': 'string'})
    person = PersonFactory()
    ObservationFactory(person=person, observation_source_value=default.concept_code,
        observation_date=date(2020, 1, 1), value_as_string='Older legacy histology')
    MeasurementFactory(person=person, measurement_concept=override,
        measurement_source_value=override.concept_code, measurement_date=date(2021, 1, 1),
        value_as_concept=concept, value_as_string=None)
    assert refresh_patient_record(person).histologic_type == 'Reviewed histology'


@pytest.mark.parametrize('failing_question', ['base', 'override'])
def test_clear_rolls_back_every_question_on_partial_failure(monkeypatch, failing_question):
    choice, concept = mapped_choice()
    override = ConceptFactory(domain=DomainFactory(domain_id='Measurement'))
    base = ConceptFactory(domain=DomainFactory(domain_id='Measurement'))
    save_mapping(choice, {'question_concept': override})
    person = PersonFactory()
    recipe = {'concept_id': base.pk, 'source_value': base.concept_code,
              'omop_table': 'measurement', 'value_kind': 'string'}
    assert project_single_value(person, choice.field_name, 'Positive', recipe)
    failed_id = base.pk if failing_question == 'base' else override.pk
    original_save = Measurement.save

    def fail_save(instance, *args, **kwargs):
        if instance.measurement_concept_id == failed_id:
            raise ValueError('Simulated unavailable destination')
        return original_save(instance, *args, **kwargs)

    monkeypatch.setattr(Measurement, 'save', fail_save)
    assert not project_single_value(person, choice.field_name, None, recipe)
    row = Measurement.objects.get(person=person)
    assert row.measurement_concept_id == override.pk
    assert row.value_as_concept_id == concept.pk
    assert row.value_as_string == 'Positive'


def test_clear_noop_is_distinct_from_failure():
    question, person = ConceptFactory(), PersonFactory()
    recipe = {'concept_id': question.pk, 'source_value': question.concept_code,
              'omop_table': 'measurement', 'value_kind': 'string'}
    assert project_single_value(person, 'her2_status', None, recipe)
    assert not project_single_value(person, 'her2_status', None, recipe)
    assert project_single_value(person, 'her2_status', None, recipe, acknowledge_existing=True)
    assert Measurement.objects.filter(person=person).count() == 1


def test_clear_includes_replaced_question_on_retired_choice():
    choice, _ = mapped_choice()
    first = ConceptFactory(domain=DomainFactory(domain_id='Measurement'))
    replacement = ConceptFactory(domain=DomainFactory(domain_id='Measurement'))
    base, person = ConceptFactory(), PersonFactory()
    recipe = {'concept_id': base.pk, 'source_value': base.concept_code,
              'omop_table': 'measurement', 'value_kind': 'string'}
    save_mapping(choice, {'question_concept': first})
    assert project_single_value(person, choice.field_name, 'Positive', recipe)
    save_mapping(choice, {'question_concept': replacement})
    choice.retired = True
    choice.save()
    assert project_single_value(person, choice.field_name, None, recipe)
    row = Measurement.objects.get(person=person, measurement_concept=first)
    assert row.value_source_value == CLEAR_VALUE
    assert row.value_as_concept_id is None


def test_transfer_re_resolves_review_and_preserves_identity_and_history():
    from omop_core.services.field_curation_transfer import apply_payload, read_payload
    choice, concept = mapped_choice(context_key='BC:clinical', aliases=['pos'])
    payload = read_payload('default', tables=('choices',))
    exported = next(r for r in payload['choices'] if r['code'] == choice.code)
    exported['value_mapping']['target_concept']['concept_id'] = 999999999
    exported['value_mapping']['notes'] = 'Transferred evidence.'
    apply_payload(payload, tables=('choices',))
    choice.refresh_from_db()
    assert choice.value_mapping.target_concept_id == concept.pk
    assert choice.value_mapping.revisions.last().decision['origin']['history']
    revision = choice.value_mapping.revision
    apply_payload(payload, tables=('choices',))
    choice.refresh_from_db()
    assert choice.value_mapping.revision == revision
    apply_payload({'choices': []}, tables=('choices',), prune=True)
    choice.refresh_from_db()
    assert choice.retired


def test_seed_is_dry_by_default_and_does_not_overwrite_reviewed_decisions():
    from django.core.management import call_command
    from io import StringIO
    choice, concept = mapped_choice()
    before = FieldChoice.objects.count()
    call_command('seed_field_value_mappings', stdout=StringIO())
    assert FieldChoice.objects.count() == before
    call_command('seed_field_value_mappings', apply=True, stdout=StringIO())
    choice.refresh_from_db()
    assert choice.value_mapping.target_concept_id == concept.pk
    revisions = sum(FieldValueConceptMapping.objects.values_list('revision', flat=True))
    call_command('seed_field_value_mappings', apply=True, stdout=StringIO())
    assert sum(FieldValueConceptMapping.objects.values_list('revision', flat=True)) == revisions


def test_read_only_inventory_includes_unmapped_catalog_options():
    from omop_core.management.commands.audit_field_value_mappings import audit
    from omop_core.models import TherapyRegimen
    TherapyRegimen.objects.create(code='reference-only', title='Unmapped regimen')
    FieldChoice.objects.create(field_name='her2_status', display='Unresolved answer')
    result = audit()
    assert any(row['title'] == 'Unmapped regimen' and row['candidate'] is None for row in result['reference_catalogs']['therapy_regimen'])
    assert any(row['display'] == 'Unresolved answer' for row in result['field_choices'])
    assert result['totals']['field_choices'] == len(result['field_choices'])
    assert result['cancerbot_live_options'] is None
