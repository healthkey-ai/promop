import json
from io import StringIO

import pytest
from django.core.management import call_command

from omop_core.models import Measurement, Observation, PatientRecord
from omop_core.services.genomics import save_variant
from omop_core.services.formula_evaluator import evaluate_formula
from tests.test_genomics_crud import setup  # noqa: F401

pytestmark = pytest.mark.django_db


def run(person, **options):
    out = StringIO()
    call_command('reconcile_tp53_cache', person_id=person.pk, stdout=out, **options)
    return json.loads(out.getvalue())


@pytest.mark.parametrize('positive', [True, False])
def test_preview_apply_and_retry_use_source_and_touch_only_tp53(setup, positive):
    person, record, _ = setup
    if positive:
        save_variant(person, {'gene': 'TP53', 'interpretation': 'Pathogenic', 'variant': 'source call'})
    PatientRecord.objects.filter(pk=record.pk).update(
        tp53_disruption=False, genetic_mutations=[], derivation_version=5,
        platelet_count=123, user_edited_fields=['platelet_count'])
    before = PatientRecord.objects.values().get(pk=record.pk)
    facts = (list(Measurement.objects.filter(person=person).values()), list(Observation.objects.filter(person=person).values()))
    preview = run(person)
    assert preview['changed'] == 1
    assert PatientRecord.objects.values().get(pk=record.pk) == before
    applied = run(person, apply=True)
    assert applied['complete'] and applied['changed'] == 1
    after = PatientRecord.objects.values().get(pk=record.pk)
    assert after.pop('tp53_disruption') is (True if positive else None)
    before.pop('tp53_disruption')
    assert before == after
    assert facts == (list(Measurement.objects.filter(person=person).values()), list(Observation.objects.filter(person=person).values()))
    assert run(person, apply=True)['changed'] == 0


@pytest.mark.parametrize('field', ['tp53_disruption', 'genetic_mutations', 'genomics_tp53'])
def test_pending_genomics_edits_are_held(setup, field):
    person, record, _ = setup
    PatientRecord.objects.filter(pk=record.pk).update(tp53_disruption=False, user_edited_fields=[field])
    report = run(person, apply=True)
    assert report['held_pending_edits'] == 1 and report['changed'] == 0
    record.refresh_from_db()
    assert record.tp53_disruption is False
    assert record.user_edited_fields == [field]


@pytest.mark.parametrize('expression', ['@not(tp53_disruption)', 'tp53_disruption == False'])
def test_unknown_does_not_satisfy_negative_formula(expression):
    assert evaluate_formula(expression, {'tp53_disruption': None}) is None
    assert evaluate_formula(expression, {'tp53_disruption': True}) is False


def test_partial_apply_reports_committed_counts_and_retry_finishes(setup, monkeypatch):
    from omop_core.management.commands import reconcile_tp53_cache as command
    from tests.factories import PatientRecordFactory
    from django.core.management import CommandError
    person, record, _ = setup
    second = PatientRecordFactory()
    PatientRecord.objects.filter(pk__in=[record.pk, second.pk]).update(tp53_disruption=False)
    original = command.list_variants
    def interrupted(current):
        if current.pk == second.person_id:
            raise RuntimeError('sensitive source must not appear in output')
        return original(current)
    monkeypatch.setattr(command, 'list_variants', interrupted)
    out = StringIO()
    with pytest.raises(CommandError, match='stopped'):
        call_command('reconcile_tp53_cache', all_records=True, apply=True, stdout=out)
    report = json.loads(out.getvalue())
    assert report['processed'] == report['changed'] == 1
    assert not report['complete']
    assert 'sensitive source' not in out.getvalue()
    record.refresh_from_db()
    second.refresh_from_db()
    assert record.tp53_disruption is None and second.tp53_disruption is False
    monkeypatch.setattr(command, 'list_variants', original)
    out = StringIO()
    call_command('reconcile_tp53_cache', all_records=True, apply=True, stdout=out)
    report = json.loads(out.getvalue())
    assert report['complete'] and report['changed'] == 1
