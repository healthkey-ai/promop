"""Export every CancerBot queue row, decided or not, for CB to load (#1649).

The file is a complete snapshot of the ``CB`` source vocabulary, not a list of
approvals. CB clears a concept for any term the export no longer carries as
approved, so a file that silently omitted rows would read as retractions.
Every row is written with its status, and CB decides what to activate.

Destinations are written as ``(vocabulary_id, concept_code)`` first and
``concept_id`` second: an HK-* id is minted per instance, and CB resolves the
natural key against its own copy of the vocabulary. The versions of every
vocabulary a destination or candidate comes from travel with the file, so CB
can refuse a snapshot built against a release it does not hold.
"""
import json
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone

from omop_core.models import SourceCodeConceptMapping, Vocabulary
from omop_core.services.cb_vocabulary import SOURCE_VOCABULARY_ID

FORMAT_VERSION = 1


def _concept(concept):
    if concept is None:
        return None
    return {
        'vocabulary_id': concept.vocabulary_id,
        'concept_code': concept.concept_code,
        'concept_id': concept.concept_id,
        'concept_name': concept.concept_name,
        'domain_id': concept.domain_id,
        'standard_concept': concept.standard_concept,
        'invalid_reason': concept.invalid_reason,
    }


class Command(BaseCommand):
    help = 'Write every CB mapping row, with status and candidates, as JSON.'

    def add_arguments(self, parser):
        parser.add_argument('--out', required=True, help='Output JSON path.')

    def handle(self, **options):
        # One snapshot: the rows, their candidates and the vocabulary versions
        # are read by separate queries, and a review saved between them would
        # otherwise export a row with another moment's candidates.
        # Only the outermost transaction can choose its isolation level; inside
        # a caller's transaction the caller's level applies. SQLite has no
        # such statement, and a single connection there sees one state anyway.
        outermost = not connection.in_atomic_block
        with transaction.atomic():
            if outermost and connection.vendor == 'postgresql':
                with connection.cursor() as cursor:
                    cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            payload = self._payload()
        out = Path(options['out'])
        out.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
        self.stdout.write(self.style.SUCCESS(
            f'Wrote {len(payload["mappings"]):,} CB mappings ({payload["counts"]}) to {out}'))

    def _payload(self):
        rows = (
            SourceCodeConceptMapping.objects
            .filter(source_vocabulary_id=SOURCE_VOCABULARY_ID)
            .select_related('target_concept', 'reviewer')
            .prefetch_related('destination_candidates__target_concept')
            .order_by('source_code')
        )
        mappings, vocabularies, statuses = [], set(), Counter()
        for m in rows:
            table, _, code = m.source_code.partition(':')
            target = m.target_concept
            if target is not None:
                vocabularies.add(target.vocabulary_id)
            candidates = []
            for c in sorted(m.destination_candidates.all(),
                            key=lambda c: (c.target_vocabulary_id, c.target_concept_code)):
                vocabularies.add(c.target_vocabulary_id)
                candidates.append({
                    'vocabulary_id': c.target_vocabulary_id,
                    'concept_code': c.target_concept_code,
                    'concept_id': c.target_concept_id,
                    'origins': c.origins,
                })
            statuses[m.status] += 1
            mappings.append({
                'source_code': m.source_code,
                'table': table,
                'code': code,
                'description': m.source_code_description,
                'status': m.status,
                'target': _concept(target),
                # A destination id the vocabulary no longer holds: CB must see
                # it as a broken row, not as "no destination".
                'target_concept_id_unresolved': (
                    m.target_concept_id if m.target_concept_id and target is None else None),
                'candidates': candidates,
                'reviewer': m.reviewer.email if m.reviewer else None,
                'reviewed_at': m.reviewed_at.isoformat() if m.reviewed_at else None,
                'updated_at': m.updated_at.isoformat() if m.updated_at else None,
                'notes': m.notes,
            })

        return {
            'format_version': FORMAT_VERSION,
            'source_vocabulary_id': SOURCE_VOCABULARY_ID,
            'exported_at': timezone.now().isoformat(),
            'counts': dict(sorted(statuses.items())),
            'vocabularies': [
                {'vocabulary_id': v.vocabulary_id, 'vocabulary_version': v.vocabulary_version}
                for v in Vocabulary.objects.filter(vocabulary_id__in=vocabularies).order_by('vocabulary_id')
            ],
            'mappings': mappings,
        }
