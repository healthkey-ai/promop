"""Audit the stored ``no_pre_existing_conditions`` answers and their provenance (#1988).

The flag used to be a seeded formula, ``@count(preexisting_conditions) == 0``, so an
unassessed empty list was stored as an explicit "no conditions" answer. #1989 stops the
formula, but it cannot tell which of the already stored ``True`` values a person
authored: ``RecordRevision`` is written before formulas run, an unchanged resubmission
writes no revision, and the admin writer writes none at all. Absence of a revision is
therefore evidence in neither direction.

This command makes the ambiguity measurable instead of guessed. It only reads: every
record is classified by the evidence it carries, the summary is printed, and
``--output`` writes a manifest that the repair command must match before it may change
anything. The manifest pins the exact data state (flag, categories, revision evidence)
so a repair cannot run against a cohort that moved underneath it.

Classification, in priority order:

``contradiction``    True beside recorded categories. #1988 B: the list wins.
``authored_true``    True and the latest evidence is a write of True (revision or a
                     pending ``user_edited_fields`` entry). Keep it.
``rewritten_true``   A revision wrote False/empty and the current value is True again.
                     The formula most likely rewrote it, but that is inference.
``unattributed_true``True with no evidence who wrote it. Neither proven formula output
                     nor proven authoring -- the policy decision, not a guess.
``explicit_false``   False never came from this formula, so it is a real answer.
``unanswered``      NULL/None: nobody answered.

Exit status is 0 even when findings exist (they are expected on an unprepared database);
``--fail-on`` turns a classification into a non-zero exit for a release gate.
"""

import hashlib
import json
import os
from datetime import datetime, timezone as dt_timezone

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from omop_core.models import FieldFormula, PatientRecord, RecordRevision
from omop_core.services.pre_existing_conditions import condition_categories

FLAG_FIELD = 'no_pre_existing_conditions'
CATEGORY_FIELD = 'preexisting_conditions'
CLASSIFICATIONS = (
    'contradiction',
    'authored_true',
    'rewritten_true',
    'unattributed_true',
    'explicit_false',
    'unanswered',
)

#: Recommended next step per classification. Only the repair command acts on these.
ACTIONS = {
    'contradiction': 'clear_flag',
    'authored_true': 'keep',
    'rewritten_true': 'needs_policy',
    'unattributed_true': 'needs_policy',
    'explicit_false': 'keep',
    'unanswered': 'none',
}

_TRUE_TEXT = {'true', 't', '1', 'yes', 'y', 'on'}
_FALSE_TEXT = {'false', 'f', '0', 'no', 'n', 'off'}


def _as_bool(text):
    """Interpret a ``RecordRevision`` textual value, or None when it is blank."""
    if text is None:
        return None
    normalized = str(text).strip().casefold()
    if normalized in _TRUE_TEXT:
        return True
    if normalized in _FALSE_TEXT:
        return False
    if normalized == '':
        return None
    return None


def _category_state(value):
    if value is None:
        return 'missing'
    if not isinstance(value, (list, tuple)):
        return 'malformed'
    return 'nonempty' if condition_categories(value) else 'empty'


def _flag_revisions():
    """The flag history for every record, grouped by record, oldest first.

    Filtering by the cohort's ids would need a large ``IN`` clause and the whole
    cohort in memory; the flag history is small (one row per authored change).
    """
    history = {}
    rows = (RecordRevision.objects
            .filter(field=FLAG_FIELD)
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


def classify(flag, categories, revisions=(), pending_edit=False):
    """Classify one record from its stored answer, categories and evidence."""
    if flag is True and condition_categories(categories):
        return 'contradiction'
    if flag is False:
        # The formula only ever produced True or None, so False is a real answer.
        return 'explicit_false'
    if flag is None:
        return 'unanswered'
    if pending_edit:
        return 'authored_true'
    if revisions:
        latest = _as_bool(revisions[-1]['new'])
        if latest is True:
            return 'authored_true'
        if latest is False:
            # Something wrote "not none" and the current value is True again: the most
            # likely writer is the formula, but it is an inference, not a fact.
            return 'rewritten_true'
    return 'unattributed_true'


def _record_line(pk, flag, categories, revisions, pending_edit):
    """A canonical, hashable description of one record's data and evidence."""
    return '|'.join((
        str(pk),
        '' if flag is None else str(flag).lower(),
        ','.join(sorted(categories)),
        str(len(revisions)),
        '' if not revisions else str(revisions[-1]['new'] or ''),
        '1' if pending_edit else '0',
    ))


class Command(BaseCommand):
    help = ('Report stored "no pre-existing conditions" answers with their provenance; '
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
            '--quiet', action='store_true', help='Print only the manifest hash',
        )

    def handle(self, *args, **options):
        if options['output'] and os.path.exists(options['output']):
            raise CommandError(
                f'{options["output"]} already exists; refusing to overwrite an audit '
                'manifest (choose a new path)')

        # Inside a caller's transaction (pytest, tests) ``SET TRANSACTION READ ONLY``
        # is illegal because statements already ran; the command's own reads are the
        # only operations either way, so the guard is best-effort by design.
        owns_transaction = not connection.in_atomic_block
        with transaction.atomic():
            if owns_transaction:
                self._make_transaction_read_only()
            payload = self._collect(summary_only=options['summary_only'])

        manifest = payload['manifest_sha256']
        if options['quiet']:
            self.stdout.write(manifest)
        else:
            self._print_summary(payload)

        if options['output']:
            directory = os.path.dirname(os.path.abspath(options['output']))
            os.makedirs(directory, exist_ok=True)
            with open(options['output'], 'w', encoding='utf-8') as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write('\n')
            self.stdout.write(self.style.SUCCESS(
                f'manifest written to {options["output"]} ({manifest})'))

        failing = set(options['fail_on'])
        if failing:
            found = sorted(
                name for name in failing
                if payload['totals']['by_classification'].get(name))
            if found:
                raise CommandError(
                    f'finding gate: records classified as {", ".join(found)}')

    def _make_transaction_read_only(self):
        """Ask Postgres to reject writes for this transaction, as a second guard."""
        if connection.vendor != 'postgresql':
            return
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')

    def _collect(self, summary_only):
        queryset = (
            PatientRecord.objects
            .order_by('pk')
            .values_list('pk', 'organization_id', 'no_pre_existing_conditions',
                         CATEGORY_FIELD, 'user_edited_fields', 'derivation_version',
                         'derived_at')
        )
        history = _flag_revisions()

        records = []
        totals = 0
        by_classification = {name: 0 for name in CLASSIFICATIONS}
        by_category_state = {}
        by_organization = {}
        by_derivation = {}
        actions = {}
        hashed = hashlib.sha256()

        for (pk, organization_id, flag, categories_raw, edited, derivation_version,
             derived_at) in queryset.iterator(chunk_size=2000):
            categories = condition_categories(categories_raw)
            revisions = history.get(pk, [])
            pending_edit = isinstance(edited, list) and FLAG_FIELD in edited
            classification = classify(flag, categories_raw, revisions, pending_edit)
            totals += 1

            hashed.update(
                (_record_line(pk, flag, categories, revisions, pending_edit) + '\n')
                .encode('utf-8'))

            state = _category_state(categories_raw)
            by_classification[classification] += 1
            by_category_state[state] = by_category_state.get(state, 0) + 1
            by_organization[str(organization_id)] = \
                by_organization.get(str(organization_id), 0) + 1
            by_derivation[str(derivation_version)] = \
                by_derivation.get(str(derivation_version), 0) + 1
            action = ACTIONS[classification]
            actions[action] = actions.get(action, 0) + 1

            if not summary_only:
                records.append({
                    'id': pk,
                    'organization_id': organization_id,
                    'flag': flag,
                    'categories': categories,
                    'category_state': state,
                    'classification': classification,
                    'recommended_action': action,
                    'derivation_version': derivation_version,
                    'derived_at': derived_at.isoformat() if derived_at else None,
                    'flag_revisions': len(revisions),
                    'pending_edit': pending_edit,
                })

        return {
            'generated_at': datetime.now(dt_timezone.utc).isoformat(),
            'database': {
                'name': connection.settings_dict.get('NAME'),
                'host': connection.settings_dict.get('HOST'),
            },
            'formula': self._formula_state(),
            'totals': {
                'records': totals,
                'by_classification': by_classification,
                'by_category_state': by_category_state,
                'by_organization': by_organization,
                'by_derivation_version': by_derivation,
                'by_recommended_action': actions,
            },
            'records': records,
            'manifest_sha256': hashed.hexdigest(),
            'manifest_scope': ('per record: pk|flag|categories|flag_revision_count|'
                               'latest_flag_revision_new_value|pending_edit'),
        }

    def _formula_state(self):
        formulas = list(FieldFormula.objects
                        .filter(field_name=FLAG_FIELD)
                        .values('id', 'is_active'))
        applied = set()
        if connection.vendor == 'postgresql':
            if 'django_migrations' in connection.introspection.table_names():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT name FROM django_migrations WHERE app = 'omop_core' "
                        "AND name LIKE '03%'")
                    applied = {row[0] for row in cursor.fetchall()}
        return {
            'definitions': formulas,
            'active': any(f['is_active'] for f in formulas),
            'migration_0304_applied': any(n.startswith('0304') for n in applied),
            'migration_0305_applied': any(n.startswith('0305') for n in applied),
        }

    def _print_summary(self, payload):
        totals = payload['totals']
        formula = payload['formula']
        self.stdout.write(f'database: {payload["database"]["name"]} '
                          f'({payload["database"]["host"]})')
        self.stdout.write(f'records: {totals["records"]}')
        self.stdout.write(
            'formula: '
            + ('ACTIVE -- it can rewrite answers, deploy #1989 first'
               if formula['active'] else 'inactive')
            + f' (0304 applied: {formula["migration_0304_applied"]}, '
              f'0305 applied: {formula["migration_0305_applied"]})')
        self.stdout.write('')
        self.stdout.write('classification:')
        for name in CLASSIFICATIONS:
            count = totals['by_classification'][name]
            self.stdout.write(f'  {name:<18} {count:>8}  -> {ACTIONS[name]}')
        self.stdout.write('')
        self.stdout.write('category state:')
        for name, count in sorted(totals['by_category_state'].items()):
            self.stdout.write(f'  {name:<18} {count:>8}')
        self.stdout.write('')
        self.stdout.write('by organization:')
        for name, count in sorted(totals['by_organization'].items()):
            self.stdout.write(f'  {name:<18} {count:>8}')
        self.stdout.write('')
        self.stdout.write(f'manifest: {payload["manifest_sha256"]}')
