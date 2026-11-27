"""
Management command: audit_hkl_duplicates

Read-only audit of the HK-Labs (``hkl:``) concepts that migration 0380 removes
because each duplicates a standard serum/plasma LOINC test (``hkl:psa`` and
LOINC 2857-1, ...). For every one present on this instance it lists

* every reference to it: each foreign key to Concept on every model (found by
  looking at the models, not listed), the columns that hold a concept id or
  code as plain text, the kind of data each is (instance_data.py's reference,
  patient or system), and what the migration does with it;
* the standard concept it would be repointed to, and whether that concept
  exists here, is active and is standard;
* anything the migration will refuse to touch, with the reason.

It runs the same planner as the migration, so it says what the migration will
do. Nothing is written.

Exit status 1 when a concept present here will NOT be removed: the standard
concept is missing or inactive, a referencing row looks like urine, a 24 hour
collection, saliva or a per-mass figure such as ng/mg (the migration then moves
only the serum rows and keeps the concept), or a reference exists that the
migration does not rewrite. Exit 0 when every concept present will be removed, and when none is
present.

Usage:
    # Staging (settings also need the deployment's SECRET_KEY)
    DATABASE_URL="${STAGING_DATABASE_URL:?}" python manage.py audit_hkl_duplicates

    # Production: .env's own DATABASE_URL, read-only
    python manage.py audit_hkl_duplicates --json
"""
import json
import sys

from django.apps import apps
from django.core.management.base import BaseCommand

from omop_core.services import hkl_duplicates as hkl

_OUTCOME_TEXT = {
    'delete': 'will be removed',
    'skip_target': 'KEPT: standard concept unusable',
    'partial_non_serum': 'KEPT: serum rows move, the rest stay',
    'skip_unhandled': 'KEPT: reference not rewritten',
}


class Command(BaseCommand):
    help = ('Read-only audit of the HK-Labs duplicates of standard LOINC tests that migration '
            '0380 removes. Exits 1 when one will be kept.')

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true', help='Print the plan as JSON.')

    def handle(self, *args, **options):
        plans = hkl.build_plan(apps)
        kept = [p for p in plans if not p.deletable]
        if options['json']:
            self.stdout.write(json.dumps({
                'present': len(plans),
                'retired_table': len(hkl.RETIRED_HKL_DUPLICATES),
                'removable': len(plans) - len(kept),
                'kept': len(kept),
                'concepts': [self._as_dict(p) for p in plans],
            }, indent=2))
        else:
            self._print(plans, kept)
        if kept:
            sys.exit(1)

    @staticmethod
    def _as_dict(plan):
        return {
            'hkl_code': plan.hkl_code, 'concept_id': plan.concept_id, 'name': plan.concept_name,
            'target': {'vocabulary_id': hkl.STANDARD_VOCABULARY, 'code': plan.target_code,
                       'concept_id': plan.target_id, 'name': plan.target_name,
                       'state': plan.target_state},
            'outcome': plan.outcome, 'detail': plan.detail,
            'references': [vars(r) for r in plan.references],
            'non_serum_rows': plan.non_serum, 'non_serum_total': plan.non_serum_total,
            'blockers': plan.blockers,
        }

    def _print(self, plans, kept):
        out = self.stdout.write
        out(f'HK-Labs duplicates of standard LOINC tests: {len(plans)} of '
            f'{len(hkl.RETIRED_HKL_DUPLICATES)} listed concepts present on this instance.')
        if not plans:
            out(self.style.SUCCESS('Nothing to remove.'))
            return
        for plan in plans:
            target = (f'LOINC {plan.target_code} (concept {plan.target_id}, "{plan.target_name[:60]}")'
                      if plan.target_id else f'LOINC {plan.target_code}')
            out('')
            out(f'{plan.hkl_code}  concept {plan.concept_id}  "{plan.concept_name}"')
            out(f'  -> {target}: {plan.target_state.replace("_", " ")}')
            if not plan.references:
                out('  no references')
            for ref in plan.references:
                out(f'  {ref.describe()}')
            style = self.style.SUCCESS if plan.deletable else self.style.ERROR
            out('  ' + style(_OUTCOME_TEXT[plan.outcome]) + (f': {plan.detail}' if plan.detail else ''))
            if plan.non_serum_total:
                moving = sum(r.count for r in plan.references if r.action == 'repoint') \
                    - plan.non_serum_total
                out(f'    {max(moving, 0)} serum row(s) would move, {plan.non_serum_total} left alone:')
            for line in plan.non_serum[:5]:
                out(f'    not serum/plasma? {line}')
        out('')
        out(f'{len(plans) - len(kept)} concept(s) will be removed, {len(kept)} kept.')
        if kept:
            out(self.style.ERROR(
                'Exit 1: the concepts above are left in place by the migration and need a decision.'))
