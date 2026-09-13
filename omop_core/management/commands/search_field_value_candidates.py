"""Attach reproducible, unapproved Athena search evidence to a gap inventory."""
import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone

from omop_core.models import Concept, ConceptSynonym


class Command(BaseCommand):
    help = 'Search every inventoried reference value against current standard names/synonyms. Candidates are never approvals.'

    def add_arguments(self, parser):
        parser.add_argument('--inventory', required=True)
        parser.add_argument('--output', required=True)

    def handle(self, **options):
        inventory = json.loads(Path(options['inventory']).read_text())
        labels = {r['display'] for r in inventory['field_choices']}
        for rows in inventory['reference_catalogs'].values():
            labels.update(r.get('title') or r.get('value') for r in rows)
        for group in inventory.get('cancerbot_source_options', []):
            labels.update(r['label'] for r in group['literal_values'] if isinstance(r['label'], str))
        labels = sorted(label for label in labels if isinstance(label, str) and label.strip())
        results = []
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION READ ONLY')
            today = timezone.localdate()
            standards = Concept.objects.filter(standard_concept='S', invalid_reason__isnull=True,
                concept_id__gt=0, concept_id__lt=2_000_000_000, valid_start_date__lte=today, valid_end_date__gte=today
            ).exclude(source='HealthKey').exclude(vocabulary__vocabulary_id__startswith='HK-')
            for label in labels:
                strategy = 'exact_name'
                terms = label.strip()
                # Short codes, numbers and contextual grades need the field's
                # answer list, not a misleading global substring search.
                if len(terms) < 3 or not any(ch.isalpha() for ch in terms):
                    results.append({'label': label, 'strategy': 'context_required', 'candidates': [], 'disposition': 'needs_review'})
                    continue
                found = list(standards.filter(concept_name__iexact=terms).order_by('pk')[:9])
                if not found:
                    strategy = 'exact_synonym'
                    ids = ConceptSynonym.objects.filter(concept_synonym_name__iexact=terms).values_list('concept_id', flat=True)
                    found = list(standards.filter(pk__in=ids).order_by('pk')[:9])
                if not found and len(terms) >= 6:
                    strategy = 'name_substring'
                    found = list(standards.filter(concept_name__icontains=terms).order_by('pk')[:9])
                results.append({'label': label, 'strategy': strategy, 'truncated': len(found) > 8,
                    'candidates': [{'concept_id': c.pk, 'vocabulary': c.vocabulary_id, 'code': c.concept_code,
                        'name': c.concept_name, 'domain': c.domain_id} for c in found[:8]],
                    'disposition': 'ambiguous' if len(found) > 1 else 'needs_review'})
        result = {'searched_at': timezone.now().isoformat(), 'release': inventory.get('vocabulary_releases'),
            'limitations': ['Name/synonym candidates are not semantic approvals.',
                'No match does not prove no equivalent; review source Maps to relationships, context and composite representations.',
                'Source options not supplied in a live CancerBot export cannot be searched.'],
            'labels': results}
        Path(options['output']).write_text(json.dumps(result, indent=2, default=str) + '\n')
        self.stdout.write(f'Searched {len(results)} distinct option labels; {sum(bool(r["candidates"]) for r in results)} have candidates. No database writes.')
