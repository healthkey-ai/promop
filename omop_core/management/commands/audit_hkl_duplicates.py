"""
Management command: audit_hkl_duplicates

Read-only audit of the auto-created HK-Labs (``hkl:``) concepts that migration
0380 removes (#2922). For every one present on this instance it lists

* every reference to it: each foreign key to Concept on every model (found by
  looking at the models, not listed), the columns that hold a concept id or
  code as plain text, the kind of data each is (instance_data.py's reference,
  patient or system), and what the migration does with it;
* what becomes of the concept: repointed to the standard LOINC concept it
  duplicates (and whether that concept exists here, is active and is
  standard), or, with no standard equivalent, its references cleared (a
  measurement's source concept becomes 0, a mapping returns to the queue);
* the approved mappings that lose their destination and are demoted to proposed;
* anything the migration will refuse to touch, with the reason.

It runs the same planner as the migration, so it says what the migration will
do. Nothing is written.

Exit status 1 when a concept present here will NOT be removed: the standard
concept of a listed duplicate is missing or inactive, or a reference exists
that cannot be cleared. (A referencing row of a listed duplicate that looks like
urine, a 24 hour collection, saliva or a per-mass figure such as ng/mg is not
serum or plasma: it is cleared, not repointed, and is listed here.) Exit 0 when every concept present
will be removed, and when none is present.

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
                'totals': self._totals(plans),
                'removable': len(plans) - len(kept),
                'kept': len(kept),
                'concepts': [self._as_dict(p) for p in plans],
            }, indent=2))
        else:
            self._print(plans, kept)
        if kept:
            sys.exit(1)

    @staticmethod
    def _totals(plans):
        refs = [(p, r) for p in plans for r in p.references]
        return {
            'concepts_repointed': sum(1 for p in plans if p.action == 'repoint'),
            'concepts_cleared': sum(1 for p in plans if p.action == 'clear'),
            'references_repointed': sum(r.count for p, r in refs if r.action == 'repoint'
                                        and p.deletable) - sum(
                                            p.non_serum_total for p in plans if p.deletable),
            'references_cleared': sum(r.count for _, r in refs if r.action in ('clear', 'clear_mapping'))
            + sum(p.non_serum_total for p in plans if p.deletable),
            'approved_mappings_demoted': sum(len(p.demotions) for p in plans if p.deletable),
        }

    @staticmethod
    def _as_dict(plan):
        return {
            'hkl_code': plan.hkl_code, 'concept_id': plan.concept_id, 'name': plan.concept_name,
            'target': {'vocabulary_id': hkl.STANDARD_VOCABULARY, 'code': plan.target_code,
                       'concept_id': plan.target_id, 'name': plan.target_name,
                       'state': plan.target_state},
            'outcome': plan.outcome, 'detail': plan.detail,
            'action': plan.action,
            'references': [vars(r) for r in plan.references],
            'demotions': plan.demotions,
            'non_serum_rows': plan.non_serum, 'non_serum_total': plan.non_serum_total,
            'blockers': plan.blockers,
        }

    def _print(self, plans, kept):
        out = self.stdout.write
        out(f'Auto-created HK-Labs (hkl:) concepts present on this instance: {len(plans)}.')
        if not plans:
            out(self.style.SUCCESS('Nothing to remove.'))
            return
        for plan in plans:
            out('')
            out(f'{plan.hkl_code}  concept {plan.concept_id}  "{plan.concept_name}"')
            if plan.target_code:
                target = (f'LOINC {plan.target_code} (concept {plan.target_id}, "{plan.target_name[:60]}")'
                          if plan.target_id else f'LOINC {plan.target_code}')
                kind = 'duplicates' if plan.kind == 'table' else 'is the code of'
                out(f'  {kind} {target}: {plan.target_state.replace("_", " ")}')
            out(f'  action: {"repoint to the standard concept" if plan.action == "repoint" else "clear (no standard equivalent here)"}')
            if not plan.references:
                out('  no references')
            for ref in plan.references:
                out(f'  {ref.describe()}')
            for line in plan.demotions:
                out('  ' + self.style.WARNING(f'APPROVED MAPPING DEMOTED to proposed: {line}'))
            style = self.style.SUCCESS if plan.deletable else self.style.ERROR
            out('  ' + style(_OUTCOME_TEXT[plan.outcome]) + (f': {plan.detail}' if plan.detail else ''))
            if plan.non_serum_total:
                moving = sum(r.count for r in plan.references if r.action == 'repoint') \
                    - plan.non_serum_total
                out(f'    {max(moving, 0)} serum row(s) would move, {plan.non_serum_total} cleared instead:')
            for line in plan.non_serum[:5]:
                out(f'    not serum/plasma, cleared: {line}')
        totals = self._totals(plans)
        out('')
        out(f'{len(plans) - len(kept)} concept(s) will be removed, {len(kept)} kept. '
            f'{totals["references_repointed"]} reference(s) repointed to a standard concept, '
            f'{totals["references_cleared"]} cleared, '
            f'{totals["approved_mappings_demoted"]} approved mapping(s) demoted to proposed.')
        if kept:
            out(self.style.ERROR(
                'Exit 1: the concepts above are left in place by the migration and need a decision.'))
