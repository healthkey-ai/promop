"""Audit legacy pregnancy/tobacco defaults that look like explicit answers (#2432).

Both columns were ``null=False, default=True`` and were written by a derivation:

* ``no_pregnancy_or_lactation_status`` by ``@not(pregnancy_test_result)``, whose
  input also defaulted to False;
* ``no_tobacco_use_status`` by the tobacco observations, with no observation
  needed for the column's own default.

So every legacy row says "not pregnant" / "does not use tobacco" whether or not
anyone answered. They are now nullable self-assessments (0323, 0329). This
command measures which stored values have evidence and which are pure defaults.
It only reads; the repair command is a separate, guarded action.

Classification, in priority order:

``unanswered``        None: nobody answered.
``explicit_false``    False. The legacy default never produced it; a person or a
                      recorded source fact answered.
``authored_true``     ``RecordRevision`` or ``user_edited_fields`` says a person
                      set the field.
``source_backed_true``A tobacco observation / pregnancy test is recorded. The
                      derivation wrote True from a fact, not from the default.
``rewritten_true``    The last authored value was False (or a clear), then the
                      current value became True again. The old formula is the
                      likely writer. This needs policy review, not an automatic
                      clear: the authored False may be clinically important.
``source_conflict``   A recorded tobacco/pregnancy fact says the opposite of the
                      stored True (current smoker, positive test).
``unattributed_true`` True with no evidence at all: the legacy default.

Only ``unattributed_true`` is recommended for clearing. Everything else is kept
or left for policy review. The pre-apply review, including the revision coverage
this audit publishes, is in ``docs/legacy_pregnancy_tobacco_repair_plan.md``.
"""

import hashlib
import json
import re
from datetime import date, datetime, timezone as dt_timezone

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Count, Min, Q

from omop_core.management.guarded_repair import (
    approval_digest, as_bool, make_transaction_read_only, refuse_existing_manifest,
    write_manifest,
)
from omop_core.models import Measurement, Observation, PatientRecord, RecordRevision

FIELDS = (
    'no_pregnancy_or_lactation_status',
    'no_tobacco_use_status',
)
CLASSIFICATIONS = (
    'unanswered',
    'explicit_false',
    'authored_true',
    'source_backed_true',
    'rewritten_true',
    'source_conflict',
    'source_unknown',
    'unattributed_true',
)
ACTIONS = {
    'unanswered': 'none',
    'explicit_false': 'keep',
    'authored_true': 'keep',
    'source_backed_true': 'keep',
    'rewritten_true': 'needs_policy',
    'source_conflict': 'needs_policy',
    'source_unknown': 'needs_policy',
    'unattributed_true': 'clear',
}

_TOBACCO_CODES = ('72166-2', '266919005', '8517006', '77176002')
_PREGNANCY_CODES = ('2106-3',)

def _revisions(field_name, ids=None):
    """The field's authored history, grouped by record, oldest first.

    Repair/rollback revisions are operator actions, not authorship: a restored
    default must keep classifying as a default.

    ``ids`` narrows the scan to a cohort the repair re-checks under its row
    locks; the audit itself passes none and reads the whole history.
    """
    history = {}
    history_rows = RecordRevision.objects.filter(field=field_name)
    if ids is not None:
        history_rows = history_rows.filter(patient_record_id__in=list(ids))
    rows = (history_rows
            .exclude(changed_by__startswith='repair:2432:')
            .exclude(changed_by__startswith='rollback:2432:')
            .order_by('patient_record_id', 'changed_at', 'id')
            .values_list('patient_record_id', 'changed_at', 'old_value', 'new_value')
            .iterator(chunk_size=2000))
    for record_id, changed_at, old_value, new_value in rows:
        history.setdefault(record_id, []).append({
            'changed_at': changed_at.isoformat() if changed_at else None,
            'old': old_value,
            'new': new_value,
        })
    return history


def _latest_by_person(rows):
    """The newest fact per person, by (date, id), from value dicts."""
    best = {}
    for row in rows:
        key = (row['date'] or date.min, row['id'])
        current = best.get(row['person_id'])
        if current is None or key > current[0]:
            best[row['person_id']] = (key, row)
    return {person: row for person, (_key, row) in best.items()}


_TOBACCO_CODE_STATE = {
    '266919005': True,     # Never smoker
    '8517006': False,      # Former smoker
    '77176002': False,     # Current smoker
}
_TOBACCO_ANSWER_STATE = {
    'LA18978-9': True,     # Never smoker
    'LA15920-4': False,    # Former smoker
    'LA18976-3': False,    # Current smoker
}


def _tobacco_supports_true(row):
    """True/False for what the recorded tobacco fact says, or None if unknown.

    The normalized answer code wins. Otherwise the concept code wins over the
    source code; a recognised conflict is not evidence of True (it becomes a
    source conflict).
    """
    answer = _TOBACCO_ANSWER_STATE.get(row['answer_code'])
    if answer is not None:
        return answer
    states = {
        _TOBACCO_CODE_STATE[code]
        for code in (row['concept_code'], row['source_value'])
        if code in _TOBACCO_CODE_STATE
    }
    if len(states) == 1:
        return states.pop()
    if len(states) > 1:
        return False
    value = row['string'] or row['answer_name'] or row['source_value']
    if not value or not str(value).strip():
        return None
    first = str(value).strip().splitlines()[0].strip().casefold()
    if (first == 'never' or first in ('non-smoker', 'nonsmoker')
            or re.match(r'^never[- ](smoker|smoked|used|tried|tobacco)\b', first)):
        return True
    if first.startswith(('former', 'current', 'smoker', 'yes')):
        return False
    return None


def _pregnancy_supports_true(row):
    """True/False for what the recorded pregnancy test says, or None if unknown.

    The derivation keeps the first line of ``pregnancy_test_result_value`` and
    reads value_as_string before the coded answer (#2250). A leading "not
    negative" is the positive answer; "NEG" is the common negative spelling.
    """
    value = row['string'] or row['answer_name'] or row['answer_code'] or row['source_value']
    if not value or not str(value).strip():
        return None
    first = str(value).strip().splitlines()[0].strip().casefold()
    if not first:
        return None
    if first.startswith('not negative') or first.startswith('not neg'):
        return False
    if first.startswith('non-reactive') or first.startswith('nonreactive'):
        return True
    if first.startswith('negative') or first.startswith('neg') or first.startswith('not detected'):
        return True
    if first.startswith('positive') or first.startswith('pos') or first.startswith('detected'):
        return False
    if first.startswith('reactive'):
        return False
    return None


def _source_state(ids=None):
    """Per person: whether the newest recorded tobacco/pregnancy fact supports True.

    ``ids`` narrows to a set of person ids, which the repair uses to re-derive
    the evidence of its candidates under the row locks; the audit passes none.
    """
    tobacco_rows = (
        Observation.objects
        .filter(is_erroneous=False)
        .filter(
            Q(observation_concept__concept_code__in=_TOBACCO_CODES)
            | Q(observation_source_value__in=_TOBACCO_CODES)
        )
    )
    if ids is not None:
        tobacco_rows = tobacco_rows.filter(person_id__in=list(ids))
    tobacco_rows = list(
        tobacco_rows.values(
            'person_id', 'observation_date', 'observation_id',
            'observation_concept__concept_code', 'observation_source_value',
            'value_as_concept__concept_code', 'value_as_concept__concept_name',
            'value_as_string',
        )
    )
    tobacco = {
        person: _tobacco_supports_true({
            'concept_code': row['observation_concept__concept_code'],
            'source_value': row['observation_source_value'],
            'answer_code': row['value_as_concept__concept_code'],
            'answer_name': row['value_as_concept__concept_name'],
            'string': row['value_as_string'],
        })
        for person, row in _latest_by_person([
            {**row, 'date': row['observation_date'], 'id': row['observation_id']}
            for row in tobacco_rows
        ]).items()
    }
    tobacco = {person: ('unknown' if state is None else state)
               for person, state in tobacco.items()}

    pregnancy_rows = []
    measurement_rows = Measurement.objects.filter(is_erroneous=False).filter(
        Q(measurement_concept__concept_code__in=_PREGNANCY_CODES)
        | Q(measurement_source_value__in=_PREGNANCY_CODES))
    observation_rows = Observation.objects.filter(is_erroneous=False).filter(
        Q(observation_concept__concept_code__in=_PREGNANCY_CODES)
        | Q(observation_source_value__in=_PREGNANCY_CODES))
    if ids is not None:
        measurement_rows = measurement_rows.filter(person_id__in=list(ids))
        observation_rows = observation_rows.filter(person_id__in=list(ids))
    for row in measurement_rows.values(
            'person_id', 'measurement_date', 'measurement_id',
            'measurement_concept__concept_code', 'measurement_source_value',
            'value_as_concept__concept_code', 'value_as_concept__concept_name',
            'value_as_string', 'value_source_value'):
        pregnancy_rows.append({
            **row, 'date': row['measurement_date'], 'id': row['measurement_id'],
        })
    for row in observation_rows.values(
            'person_id', 'observation_date', 'observation_id',
            'observation_concept__concept_code', 'observation_source_value',
            'value_as_concept__concept_code', 'value_as_concept__concept_name',
            'value_as_string', 'value_source_value'):
        pregnancy_rows.append({
            **row, 'date': row['observation_date'], 'id': row['observation_id'],
        })
    pregnancy = {
        person: _pregnancy_supports_true({
            'string': row['value_as_string'],
            'source_value': row['value_source_value'],
            'answer_code': row['value_as_concept__concept_code'],
            'answer_name': row['value_as_concept__concept_name'],
        })
        for person, row in _latest_by_person(pregnancy_rows).items()
    }
    pregnancy = {person: ('unknown' if state is None else state)
                 for person, state in pregnancy.items()}
    return {
        'no_tobacco_use_status': tobacco,
        'no_pregnancy_or_lactation_status': pregnancy,
    }


def _revision_value(text):
    """A revision's new value: True/False, the 'clear' sentinel, or None."""
    if text is None or str(text).strip().casefold() in ('', 'none', 'null'):
        return 'clear'
    return as_bool(text)


def classify(value, revisions=(), pending_edit=False, source_state=None):
    """Classify one stored field from its value and the evidence behind it."""
    if value is None:
        return 'unanswered'
    if value is False:
        return 'explicit_false'
    latest = None
    for revision in reversed(revisions):
        parsed = _revision_value(revision['new'])
        if parsed is not None:
            latest = parsed
            break
    if latest is True:
        return 'authored_true'
    if latest is False or latest == 'clear':
        # The last authored value was not True; the old formula is the likely
        # writer. A pending marker does not override a recorded contradiction.
        return 'rewritten_true'
    if pending_edit:
        return 'authored_true'
    if source_state is True:
        return 'source_backed_true'
    if source_state is False:
        return 'source_conflict'
    if source_state == 'unknown':
        return 'source_unknown'
    return 'unattributed_true'


class Command(BaseCommand):
    help = ('Report legacy pregnancy/tobacco defaults with their provenance; '
            'read-only, writes an optional manifest for the repair command')

    def add_arguments(self, parser):
        parser.add_argument(
            '--output', metavar='PATH',
            help='Write the full manifest (summary + per-record classification) as JSON',
        )
        parser.add_argument(
            '--summary-only', action='store_true',
            help='Omit per-record rows from the output file (the manifest changes)',
        )
        parser.add_argument(
            '--fail-on', action='append', choices=CLASSIFICATIONS, default=[],
            help='Exit 1 when any record falls into this classification (repeatable)',
        )
        parser.add_argument(
            '--quiet', action='store_true', help='Print only the full-document approval hash',
        )

    def handle(self, *args, **options):
        refuse_existing_manifest(options['output'])

        owns_transaction = not connection.in_atomic_block
        with transaction.atomic():
            if owns_transaction:
                make_transaction_read_only()
            payload = self._collect(summary_only=options['summary_only'])

        if options['quiet']:
            self.stdout.write(payload['approval_sha256'])
        else:
            self._print_summary(payload)

        if options['output']:
            write_manifest(options['output'], payload)
            self.stdout.write(self.style.SUCCESS(
                f'manifest written to {options["output"]} ({payload["manifest_sha256"]})'))

        failing = set(options['fail_on'])
        if failing:
            found = sorted(
                name for name in failing
                if any(payload['totals']['by_field'][field]['by_classification'].get(name)
                       for field in FIELDS))
            if found:
                raise CommandError(
                    f'finding gate: records classified as {", ".join(found)}')

    def _collect(self, summary_only):
        histories = {field: _revisions(field) for field in FIELDS}
        source_states = _source_state()
        authored = RecordRevision.objects.exclude(
            Q(changed_by__isnull=True)
            | Q(changed_by='')
            | Q(changed_by='system')
            | Q(changed_by__startswith='migration:')
            | Q(changed_by__startswith='repair:')
            | Q(changed_by__startswith='rollback:'))
        revision_totals = authored.aggregate(
            total=Count('pk'), first=Min('changed_at'))
        revision_total = revision_totals['total']
        earliest_revision = revision_totals['first']
        # Coverage counts person-authored revisions only: migrations (0321) and
        # the service's 'system' writes record rows for every legacy patient, so
        # counting them would report full coverage and hide the risk entirely.
        revised_records = set(
            authored.order_by().values_list('patient_record_id', flat=True).distinct())
        queryset = (
            PatientRecord.objects
            .order_by('pk')
            .values_list('pk', 'person_id', 'organization_id', *FIELDS,
                         'user_edited_fields', 'derivation_version', 'derived_at')
        )

        records = []
        totals = 0
        records_with_any_revision = 0
        clear_candidates = 0
        clear_candidates_in_revised_records = 0
        by_field = {
            field: {
                'by_classification': {name: 0 for name in CLASSIFICATIONS},
                'by_action': {},
            }
            for field in FIELDS
        }
        hashed = hashlib.sha256()

        for row in queryset.iterator(chunk_size=2000):
            pk = row[0]
            person_id = row[1]
            organization_id = row[2]
            values = dict(zip(FIELDS, row[3:5]))
            edited = row[5] if isinstance(row[5], list) else []
            derivation_version, derived_at = row[6], row[7]
            has_any_revision = pk in revised_records
            if has_any_revision:
                records_with_any_revision += 1
            fields = {}
            for field in FIELDS:
                revisions = histories[field].get(pk, [])
                pending_edit = field in edited
                source_state = source_states[field].get(person_id)
                classification = classify(
                    values[field], revisions, pending_edit, source_state)
                action = ACTIONS[classification]
                if action == 'clear':
                    clear_candidates += 1
                    if has_any_revision:
                        clear_candidates_in_revised_records += 1
                by_field[field]['by_classification'][classification] += 1
                by_field[field]['by_action'][action] = (
                    by_field[field]['by_action'].get(action, 0) + 1)
                fields[field] = {
                    'value': values[field],
                    'classification': classification,
                    'recommended_action': action,
                    'revision_count': len(revisions),
                    'pending_edit': pending_edit,
                    'source_state': source_state,
                    'last_authored': next(
                        (parsed for parsed in (
                            _revision_value(revision['new']) for revision in reversed(revisions))
                         if parsed is not None), None),
                }
            totals += 1
            line = json.dumps({
                'id': pk, 'fields': fields,
            }, sort_keys=True, separators=(',', ':'))
            hashed.update((line + '\n').encode('utf-8'))
            if not summary_only:
                records.append({
                    'id': pk,
                    'person_id': person_id,
                    'organization_id': organization_id,
                    'has_any_revision': has_any_revision,
                    'fields': fields,
                    'derivation_version': derivation_version,
                    'derived_at': derived_at.isoformat() if derived_at else None,
                })

        payload = {
            'generated_at': datetime.now(dt_timezone.utc).isoformat(),
            'database': {
                'name': connection.settings_dict.get('NAME'),
                'host': connection.settings_dict.get('HOST'),
                'port': str(connection.settings_dict.get('PORT') or '5432'),
            },
            'fields': list(FIELDS),
            'totals': {
                'records': totals,
                'by_field': by_field,
                # A genuine answer entered before RecordRevision existed (0125),
                # or by a path that wrote neither a revision nor a pending
                # marker, cannot be told apart from the legacy default. The
                # operator judges that from these counts and the manifest, so
                # they are published with the audit rather than left to SQL.
                'evidence': {
                    'authored_revisions_total': revision_total,
                    'earliest_authored_revision_at': (
                        earliest_revision.isoformat() if earliest_revision else None),
                    'records_with_any_authored_revision': records_with_any_revision,
                    'records_without_any_authored_revision': (
                        totals - records_with_any_revision),
                    'clear_candidates': clear_candidates,
                    'clear_candidates_in_authored_records': (
                        clear_candidates_in_revised_records),
                },
            },
            'records': records,
            'manifest_sha256': hashed.hexdigest(),
            'manifest_version': 2,
            'manifest_scope': 'canonical JSON per record: id, per-field value, classification, '
                              'revision count, pending edit, source-backed evidence; totals '
                              'carry the revision-history coverage the clear review relies on',
        }
        payload['approval_sha256'] = approval_digest(payload)
        return payload

    def _print_summary(self, payload):
        self.stdout.write(f'database: {payload["database"]["name"]} '
                          f'({payload["database"]["host"]}:{payload["database"]["port"]})')
        self.stdout.write(f'records: {payload["totals"]["records"]}')
        for field in FIELDS:
            counts = payload['totals']['by_field'][field]['by_classification']
            actions = payload['totals']['by_field'][field]['by_action']
            self.stdout.write(f'  {field}:')
            self.stdout.write('    ' + ', '.join(
                f'{name}={counts[name]}' for name in CLASSIFICATIONS
                if counts[name]) if any(counts.values()) else '    all unanswered')
            if actions:
                self.stdout.write('    actions: ' + ', '.join(
                    f'{name}={count}' for name, count in sorted(actions.items())))
        evidence = payload['totals']['evidence']
        self.stdout.write(
            'person-authored revision history: '
            f'{evidence["authored_revisions_total"]} row(s), earliest '
            f'{evidence["earliest_authored_revision_at"] or "never"}; '
            f'{evidence["records_with_any_authored_revision"]} of '
            f'{payload["totals"]["records"]} record(s) carry any')
        without_history = (evidence['clear_candidates']
                           - evidence['clear_candidates_in_authored_records'])
        if without_history:
            self.stdout.write(self.style.WARNING(
                f'{without_history} of {evidence["clear_candidates"]} clear candidate(s) '
                'belong to records with no person-authored revision history: an answer entered '
                'before RecordRevision existed (0125), or by a path that recorded neither revision '
                'nor pending marker, is indistinguishable from the legacy default. Review '
                'the manifest before --apply.'))
        self.stdout.write(f'approval_sha256: {payload["approval_sha256"]}')
