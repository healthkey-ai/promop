"""Attach reproducible, unapproved Athena search evidence to a gap inventory."""
import json
from hashlib import sha256
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, connection, transaction
from django.utils import timezone

from omop_core.models import Concept, ConceptSynonym


class Command(BaseCommand):
    help = 'Search every inventoried reference value against current standard names/synonyms. Candidates are never approvals.'

    def add_arguments(self, parser):
        parser.add_argument('--inventory', required=True)
        parser.add_argument('--output', required=True)
        parser.add_argument('--limit', type=int, default=100, help='Maximum labels attempted per invocation.')
        parser.add_argument('--statement-timeout-ms', type=int, default=5000)
        parser.add_argument('--resume', action='store_true', help='Resume this exact inventory, retrying timed-out labels.')

    def handle(self, **options):
        inventory_bytes = Path(options['inventory']).read_bytes()
        inventory = json.loads(inventory_bytes)
        fingerprint = sha256(inventory_bytes).hexdigest()
        if options['limit'] < 1 or not 1 <= options['statement_timeout_ms'] <= 60000:
            raise CommandError('Use a positive limit and a statement timeout between 1 and 60000 ms.')
        labels = {r['display'] for r in inventory['field_choices']}
        for rows in inventory['reference_catalogs'].values():
            labels.update(r.get('title') or r.get('value') for r in rows)
        for group in inventory.get('cancerbot_source_options', []):
            labels.update(r['label'] for r in group['literal_values'] if isinstance(r['label'], str))
        labels = sorted(label for label in labels if isinstance(label, str) and label.strip())
        results = {}
        output = Path(options['output'])
        if options['resume']:
            if not output.exists():
                raise CommandError('Resume requires an existing checkpoint.')
            checkpoint = json.loads(output.read_text())
            if checkpoint.get('inventory_sha256') != fingerprint:
                raise CommandError('Checkpoint inventory differs; use a separate output for this inventory.')
            results = {row['label']: row for row in checkpoint['labels']}
        elif output.exists():
            raise CommandError('Output exists; use --resume or a new output path.')
        today = timezone.localdate()
        standards = Concept.objects.filter(standard_concept='S', invalid_reason__isnull=True,
            concept_id__gt=0, concept_id__lt=2_000_000_000, valid_start_date__lte=today, valid_end_date__gte=today
        ).exclude(source='HealthKey').exclude(vocabulary__vocabulary_id__startswith='HK-')
        pending = [label for label in labels if label not in results or results[label].get('search_error')]

        def checkpoint():
            completed = sum(not row.get('search_error') for row in results.values())
            result = {'searched_at': timezone.now().isoformat(), 'inventory_sha256': fingerprint,
                'release': inventory.get('vocabulary_releases'), 'total_labels': len(labels),
                'completed_labels': completed, 'complete': completed == len(labels),
                'limitations': ['Name/synonym candidates are not semantic approvals.',
                    'No match does not prove no equivalent; review source Maps to relationships, context and composite representations.',
                    'Source options not supplied in a live CancerBot export cannot be searched.'],
                'labels': [results[label] for label in labels if label in results]}
            temporary = output.with_suffix(output.suffix + '.tmp')
            temporary.write_text(json.dumps(result, indent=2, default=str) + '\n')
            temporary.replace(output)

        for label in pending[:options['limit']]:
            try:
                # One transaction per label: a timeout rolls back only that
                # attempt. Enforce the limit after Django connection setup.
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute('SET TRANSACTION READ ONLY')
                        cursor.execute("SELECT set_config('statement_timeout', %s, true)",
                                       [str(options['statement_timeout_ms'])])
                    results[label] = self.search_label(label, standards)
            except OperationalError as exc:
                cause = exc.__cause__
                if getattr(cause, 'sqlstate', None) != '57014':
                    raise CommandError('Reference search connection failed; completed labels remain checkpointed.') from None
                results[label] = {'label': label, 'search_error': 'statement_timeout',
                    'strategy': 'incomplete', 'candidates': [], 'disposition': 'needs_review'}
            checkpoint()
        checkpoint()
        self.stdout.write(f'Checkpointed {len(results)}/{len(labels)} labels; '
                          f'{sum(bool(r["candidates"]) for r in results.values())} have candidates. No database writes.')

    @staticmethod
    def search_label(label, standards):
        strategy = 'exact_name'
        terms = label.strip()
        # Short codes, numbers and contextual grades need the field's
        # answer list, not a misleading global substring search.
        if len(terms) < 3 or not any(ch.isalpha() for ch in terms):
            return {'label': label, 'strategy': 'context_required', 'candidates': [], 'disposition': 'needs_review'}
        found = list(standards.filter(concept_name__iexact=terms).order_by('pk')[:9])
        if not found:
            strategy = 'exact_synonym'
            ids = ConceptSynonym.objects.filter(concept_synonym_name__iexact=terms).values_list('concept_id', flat=True)
            found = list(standards.filter(pk__in=ids).order_by('pk')[:9])
        if not found and len(terms) >= 6:
            strategy = 'name_substring'
            found = list(standards.filter(concept_name__icontains=terms).order_by('pk')[:9])
        return {'label': label, 'strategy': strategy, 'truncated': len(found) > 8,
            'candidates': [{'concept_id': c.pk, 'vocabulary': c.vocabulary_id, 'code': c.concept_code,
                'name': c.concept_name, 'domain': c.domain_id} for c in found[:8]],
            'disposition': 'ambiguous' if len(found) > 1 else 'needs_review'}
