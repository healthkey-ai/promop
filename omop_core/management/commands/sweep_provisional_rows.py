"""Make provisional rows for patients whose codes have a proposed mapping (#2662).

Runs the provisional sweep: every person holding an unresolved or provisional
row, or an inventory code with no row, under a proposal that is unscoped,
proposed, has a destination and a confidence at or above
``PROVISIONAL_CONFIDENCE_FLOOR`` is resolved against the current proposals. A
person already provisional at the current proposal is left as it is, so the
command is idempotent and can be re-run, or resumed with ``--after-person-id``
(the last id a previous run printed).

Does nothing when ``PROVISIONAL_DATA_ENABLED`` is off. Writes to ``DATABASE_URL``:
name the database explicitly.

``--close-stale`` is housekeeping and runs instead of a sweep: it closes, as
failed, any run row (this table is shared with the bulk resolve) still pending
or running with no progress for ``--stale-minutes`` (default two hours), which
is what a run left by a restarted worker looks like. It changes run rows only,
needs no switch, and with ``--dry-run`` lists them without closing.
"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from omop_core.models import SourceCodeConceptMapping
from omop_core.services import provisional_sweep


class Command(BaseCommand):
    help = 'Write provisional rows for patients whose codes have a proposed mapping'

    def add_arguments(self, parser):
        parser.add_argument('--mapping-id', type=int, help="Only this mapping's code.")
        parser.add_argument('--after-person-id', type=int, default=0,
                            help='Resume after this person id (the last one a previous run finished).')
        parser.add_argument('--limit', type=int, default=provisional_sweep.SWEEP_CHUNK,
                            help='Persons per chunk.')
        parser.add_argument('--dry-run', action='store_true', help='Count the candidates; write nothing.')
        parser.add_argument('--close-stale', action='store_true',
                            help='Close runs still pending or running with no recent progress, instead of sweeping.')
        parser.add_argument('--stale-minutes', type=int, default=provisional_sweep.STALE_RUN_MINUTES,
                            help='With --close-stale: minutes without progress before a run counts as lost.')
        parser.add_argument('--queue', action='store_true',
                            help='Queue the sweep on the worker (needs CELERY_BROKER_URL) instead of running here.')

    def handle(self, *args, mapping_id, after_person_id, limit, dry_run, queue,
               close_stale=False, stale_minutes=provisional_sweep.STALE_RUN_MINUTES, **options):
        if close_stale:
            if stale_minutes < 1:
                raise CommandError('--stale-minutes must be at least 1.')
            ignored = [flag for flag, given in (
                ('--limit', limit != provisional_sweep.SWEEP_CHUNK), ('--mapping-id', mapping_id is not None),
                ('--after-person-id', bool(after_person_id)), ('--queue', queue)) if given]
            if ignored:
                raise CommandError(f'--close-stale does not take {", ".join(ignored)}.')
            self._close_stale(stale_minutes, dry_run)
            return
        if not settings.PROVISIONAL_DATA_ENABLED:
            self.stdout.write('PROVISIONAL_DATA_ENABLED is off; nothing to do.')
            return
        mapping = None
        if mapping_id is not None:
            mapping = SourceCodeConceptMapping.objects.filter(pk=mapping_id).first()
            if mapping is None:
                self.stderr.write(f'No mapping {mapping_id}.')
                return
        if dry_run:
            self.stdout.write(f'{self._count(mapping, after_person_id, limit)} candidate person(s) '
                              f'after {after_person_id}.')
            return
        if queue:
            if provisional_sweep._celery():
                run = provisional_sweep.start_sweep(mapping=mapping, limit=limit)
                self.stdout.write(f'Queued sweep run {run.id}.')
                return
            self.stdout.write('CELERY_BROKER_URL is not set, so nothing can be queued; '
                              'running the sweep here instead.')
        run = provisional_sweep.start_run_record()
        after = after_person_id
        try:
            while after is not None:
                previous = after
                after = provisional_sweep.run_sweep(
                    run.id, after=after, mapping_id=mapping_id, limit=limit)
                run.refresh_from_db()
                self.stdout.write(
                    f'after {previous}: {run.done} done, {run.provisional} provisional row(s), '
                    f'{run.reverted} reverted, {run.errors} error(s)'
                    + (f'; resume with --after-person-id {after}' if after is not None else '; finished'))
        except BaseException as exc:
            # An interrupt or an unexpected error: nothing else will finish this run.
            provisional_sweep.fail_run(run.id, f'The sweep stopped: {exc!r}')
            raise

    def _close_stale(self, stale_minutes, dry_run):
        runs = provisional_sweep.close_stale_runs(timedelta(minutes=stale_minutes), dry_run=dry_run)
        verb = 'Would close' if dry_run else 'Closed'
        for run in runs:
            self.stdout.write(f'{run.id}  {run.state}  {run.done}/{run.total}  '
                              f'last progress {run.last_progress:%Y-%m-%d %H:%M}')
        self.stdout.write(f'{verb} {len(runs)} stale run(s) (no progress for {stale_minutes} minutes).')

    @staticmethod
    def _count(mapping, after, limit):
        codes = provisional_sweep.mapping_codes(mapping) if mapping else None
        total = 0
        while True:
            ids = provisional_sweep.candidate_person_ids(
                after=after, limit=limit, codes=codes, include_provisional_holders=mapping is None)
            total += len(ids)
            if len(ids) < limit:
                return total
            after = ids[-1]
