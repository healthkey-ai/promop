"""Approve the CancerBot proposals that need no second review (#1649).

By product decision (cancerbot #5363) two categories are signed off in bulk:

* ``auto_exact`` -- the CB title is the concept's name or an exact synonym, and
  the proposal file marks it ``verified=ok``;
* ``curated`` -- therapies a clinical SME already confirmed (cancerbot #4572,
  #4587).

Everything else stays for the SME in the Code Mapping UI.

A row is approved only if its destination is still exactly what CB proposed
and is of a kind already decided (``bulk_approval_problem``). The comparison
is part of the UPDATE itself, so a curator who re-points the row while this
runs wins. Rows under another user's active edit lock are skipped, and the
reviewer must be someone the UI would let approve.

Approval here writes the queue row only. CB terms are catalog entries, so the
clinical-row re-point that a UI approval runs is not wanted, and
``repoint_clinical_rows`` refuses it for this vocabulary in any case.

Dry run unless ``--apply``.
"""
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from omop_core.models import SourceCodeConceptMapping
from omop_core.services.access import has_org_admin_access
from omop_core.services.cb_vocabulary import (
    SOURCE_VOCABULARY_ID, bulk_approval_problem, read_proposals, resolve_concepts,
)

DEFAULT_MATCHES = ('auto_exact', 'curated')


def _reviewer(email):
    users = list(get_user_model().objects.filter(email__iexact=email, is_active=True))
    if len(users) != 1:
        raise CommandError(f'Expected one active user with email {email!r}, found {len(users)}')
    user = users[0]
    # The same rule as the UI's _can_approve_mappings.
    if not (user.is_staff or has_org_admin_access(user)):
        raise CommandError(f'{user.email} may not approve mappings (staff or org admin only)')
    return user


class Command(BaseCommand):
    help = 'Approve CB proposals in the bulk-approved categories (dry run unless --apply).'

    def add_arguments(self, parser):
        parser.add_argument('--file', required=True, help='The proposal CSV the queue was imported from.')
        parser.add_argument('--match', default=','.join(DEFAULT_MATCHES),
                            help='Comma-separated proposal categories to approve.')
        parser.add_argument('--reviewer', required=True, help='Email of the person signing off.')
        parser.add_argument('--apply', action='store_true', help='Write. Without it, report only.')

    def handle(self, **options):
        matches = {m.strip() for m in options['match'].split(',') if m.strip()}
        unknown = matches - set(DEFAULT_MATCHES)
        if unknown:
            # Widening the bulk set is a product decision, not a flag.
            raise CommandError(f'Only {sorted(DEFAULT_MATCHES)} may be bulk-approved; got {sorted(unknown)}')
        reviewer = _reviewer(options['reviewer'])
        try:
            rows = read_proposals(options['file'])
        except (OSError, ValueError) as exc:
            raise CommandError(f'Cannot read CB proposals: {exc}') from exc

        rows = [r for r in rows if r.match in matches]
        concepts = resolve_concepts(r.proposal for r in rows if r.proposal)
        now = timezone.now()
        lock_cutoff = now - timedelta(minutes=settings.MAPPING_LOCK_TIMEOUT_MINUTES)
        unlocked = Q(locked_by__isnull=True) | Q(locked_at__lt=lock_cutoff) | Q(locked_by=reviewer)
        mappings = {
            m.source_code.lower(): m
            for m in SourceCodeConceptMapping.objects.filter(source_vocabulary_id=SOURCE_VOCABULARY_ID)
        }

        skipped, details, approved = Counter(), [], 0
        with transaction.atomic():
            for row in rows:
                mapping = mappings.get(row.source_code.lower())
                concept = concepts.get(row.proposal) if row.proposal else None
                if row.verified != 'ok':
                    reason = f'verified={row.verified or "-"}'
                elif row.proposal is None:
                    reason = 'no proposal'
                elif bulk_approval_problem(concept):
                    reason = bulk_approval_problem(concept)
                elif mapping is None:
                    reason = 'not in the queue (import first)'
                elif mapping.status != 'proposed':
                    reason = f'already {mapping.status}'
                elif mapping.target_concept_id != concept.concept_id:
                    reason = 're-pointed since import'
                else:
                    current = SourceCodeConceptMapping.objects.filter(
                        unlocked, pk=mapping.pk, status='proposed', target_concept_id=concept.concept_id)
                    if not options['apply']:
                        reason = '' if current.exists() else 'locked or changed'
                    else:
                        reason = '' if current.update(
                            status='approved', reviewer=reviewer, reviewed_at=now, updated_by=reviewer,
                            updated_at=now, pending_repoint_concept_ids=[],
                        ) else 'locked or changed'
                    if not reason:
                        approved += 1
                        continue
                skipped[reason] += 1
                details.append(f'{row.source_code} ({row.proposal or "-"}): {reason}')

        verb = 'Approved' if options['apply'] else 'Dry run -- would approve'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {approved:,} of {len(rows):,} rows in {sorted(matches)} as {reviewer.email}.'))
        for reason, count in sorted(skipped.items()):
            self.stdout.write(f'  skipped {count:,}: {reason}')
        for line in details:
            self.stdout.write(f'    {line}')
