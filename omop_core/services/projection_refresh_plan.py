"""Bounded, signed plans for the existing PatientRecord refresh operation.

Plans contain clinical values and belong in private operator storage, never in
shared logs or source control. Preview uses a rolled-back refresh transaction;
it needs a writable database connection even though no changes are committed.
This repairs the derived read model, not the underlying clinical facts.
"""
import hashlib
import hmac
import json
from collections import Counter
from datetime import datetime, time
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction

from omop_core.models import PatientRecord, Person, RecordRevision
from patient_portal.models import AuditEvent

MAX_PEOPLE = 100
AUDIT_PATH = 'management/backfill_patient_records/plan'
VOLATILE_FIELDS = {'derived_at', 'updated_at'}


class PlanConflict(ValueError):
    """Safe operator message without clinical values or record identifiers."""


class _ExactEncoder(DjangoJSONEncoder):
    def default(self, value):
        # Django's API encoder truncates datetimes to milliseconds. Recovery
        # and drift detection must retain the database's full precision.
        if isinstance(value, (datetime, time)):
            return value.isoformat()
        return super().default(value)


def _json(value):
    return json.dumps(value, cls=_ExactEncoder, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _signature(plan):
    key = (getattr(settings, 'AUDIT_HMAC_KEY', '') or settings.SECRET_KEY).encode()
    return hmac.new(key, _json(plan).encode(), hashlib.sha256).hexdigest()


def _environment():
    # Bind local record IDs to this database without including connection secrets.
    config = connection.settings_dict
    return _digest({key: config.get(key) for key in ('ENGINE', 'HOST', 'PORT', 'NAME')})


def _definition():
    from omop_core.services.field_curation_transfer import read_payload
    from omop_core.services.patient_record_service import DERIVATION_VERSION
    from omop_core.models import Vocabulary, VocabularyRelease

    code = hashlib.sha256()
    root = Path(__file__).resolve().parent
    for path in sorted(root.glob('*.py')):
        code.update(path.name.encode())
        code.update(path.read_bytes())
    return _digest({
        'code': code.hexdigest(), 'version': DERIVATION_VERSION,
        'curation': read_payload('default', tables=('mappings', 'custom_fields', 'choices', 'formulas')),
        'releases': list(Vocabulary.objects.order_by('pk').values()),
        'release_provenance': list(VocabularyRelease.objects.order_by('pk').values()),
    })


def _state(record):
    record.refresh_from_db()
    return json.loads(_json({f.attname: getattr(record, f.attname)
                            for f in record._meta.concrete_fields}))


def _content(state):
    return {key: value for key, value in state.items() if key not in VOLATILE_FIELDS}


def _changes(before, after):
    return {key: {'before': before[key], 'after': after[key]}
            for key in sorted(before.keys() - VOLATILE_FIELDS)
            if _json(before[key]) != _json(after[key])}


def _locked_record(person_id):
    try:
        record = PatientRecord.objects.select_for_update().get(person_id=person_id)
        record.person = Person.objects.select_for_update().get(pk=person_id)
        return record
    except (PatientRecord.DoesNotExist, Person.DoesNotExist):
        raise PlanConflict('A selected record is unavailable; create a new plan.') from None


def create_plan(person_ids):
    from omop_core.services.patient_record_service import refresh_patient_record

    people = sorted(set(person_ids))
    if not people or len(people) > MAX_PEOPLE:
        raise PlanConflict(f'Select between 1 and {MAX_PEOPLE} distinct people.')
    definition = _definition()
    entries = []
    for person_id in people:
        with transaction.atomic():
            record = _locked_record(person_id)
            before = _state(record)
            after = _state(refresh_patient_record(record.person))
            entries.append({'person_id': person_id, 'before_hash': _digest(before),
                            'after_hash': _digest(_content(after)),
                            'changes': _changes(before, after)})
            transaction.set_rollback(True)
    if _definition() != definition:
        raise PlanConflict('Curation changed during preview; create a new plan.')
    plan = {'schema': 1, 'mode': 'read_model', 'id': str(uuid4()),
            'environment': _environment(), 'definition': definition, 'entries': entries}
    return {**plan, 'signature': _signature(plan)}


def validate_plan(plan):
    if not isinstance(plan, dict):
        raise PlanConflict('Invalid refresh plan.')
    unsigned = {key: value for key, value in plan.items() if key != 'signature'}
    signature = plan.get('signature')
    if not isinstance(signature, str) or not hmac.compare_digest(signature, _signature(unsigned)):
        raise PlanConflict('Plan signature does not match; use the original private plan.')
    if plan.get('schema') != 1 or plan.get('mode') != 'read_model' or plan['environment'] != _environment():
        raise PlanConflict('Plan belongs to a different database or unsupported operation.')
    return plan


def summarize(plan):
    counts = Counter(field for entry in plan['entries'] for field in entry['changes'])
    return {'records': len(plan['entries']), 'changes_by_field': dict(sorted(counts.items()))}


def _event(plan, person_id, operation):
    return AuditEvent.objects.filter(path=AUDIT_PATH, resource_id=str(person_id),
        detail__plan_id=plan['id'], detail__operation=operation).first()


def _audit(plan, record, before, after, operation):
    changes = _changes(before, after)
    for field, change in changes.items():
        RecordRevision.objects.create(patient_record=record, changed_by='system', field=field,
            old_value=_json(change['before']), new_value=_json(change['after']))
    # Recovery snapshots live in the existing protected audit trail. The
    # command prints aggregate counts only. Use save() to retain HMAC/chaining.
    AuditEvent.objects.create(event_type=AuditEvent.EVENT_ADMIN, method='COMMAND',
        path=AUDIT_PATH, user_id='system', resource_id=str(record.person_id), status_code=200,
        detail={'plan_id': plan['id'], 'definition': plan['definition'], 'operation': operation,
                'before': before, 'after': after, 'fields': sorted(changes)})


def apply_plan(plan, *, rollback=False):
    """Commit one reviewed record at a time; retries resume from audit entries.

    A later conflict leaves prior completed records audited and resumable. A
    rollback never overwrites a record changed after application, even when a
    subsequent refresh happened to produce the same clinical values.
    """
    from omop_core.services.patient_record_service import refresh_patient_record

    validate_plan(plan)
    counts = Counter()
    for entry in plan['entries']:
        with transaction.atomic():
            record = _locked_record(entry['person_id'])
            applied = _event(plan, record.person_id, 'apply')
            reverted = _event(plan, record.person_id, 'rollback')
            if reverted:
                if not rollback:
                    raise PlanConflict('This plan was rolled back; create a new plan before applying again.')
                counts['already_rolled_back'] += 1
                continue
            if rollback:
                if not applied:
                    if _digest(_state(record)) != entry['before_hash']:
                        raise PlanConflict('The original record state and recovery audit are unavailable; automatic rollback was refused.')
                    counts['not_applied'] += 1
                    continue
                if applied.signature != applied.compute_signature():
                    raise PlanConflict('Recovery audit integrity check failed.')
                before = _state(record)
                if _digest(before) != _digest(applied.detail['after']):
                    raise PlanConflict('A record changed after application; automatic rollback was refused.')
                original = applied.detail['before']
                updates = {f.attname: f.to_python(original[f.attname])
                           for f in record._meta.concrete_fields
                           if not f.primary_key and not f.is_relation}
                PatientRecord.objects.filter(pk=record.pk).update(**updates)
                _audit(plan, record, before, _state(record), 'rollback')
                counts['rolled_back'] += 1
                continue
            if applied:
                counts['already_applied'] += 1
                continue
            if _definition() != plan['definition']:
                raise PlanConflict('Code, curation or vocabulary releases changed; create a new plan.')
            before = _state(record)
            if _digest(before) != entry['before_hash']:
                raise PlanConflict('A selected record changed since preview; create a new plan.')
            after = _state(refresh_patient_record(record.person))
            if (_digest(_content(after)) != entry['after_hash']
                    or _definition() != plan['definition']):
                raise PlanConflict('Derivation changed since preview; create a new plan. This record was rolled back.')
            _audit(plan, record, before, after, 'apply')
            counts['applied'] += 1
    return dict(counts)
