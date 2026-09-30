"""Read-only, repeatable before/after comparison of the drug top-N probe."""
import json
from statistics import median
from time import perf_counter

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from omop_core.mapping.suggestions import lexical_candidates, LEXICAL_LIMIT_MAX
from omop_core.models import SourceCodeConceptMapping


class Command(BaseCommand):
    help = 'Compare exact drug candidates and warm median latency with/without the top-N probe.'

    def add_arguments(self, parser):
        parser.add_argument('--text', action='append', default=[])
        parser.add_argument('--count', type=int, default=10)
        parser.add_argument('--limit', type=int, default=10)
        parser.add_argument('--repeat', type=int, default=3)
        parser.add_argument('--json', action='store_true', dest='as_json')

    def handle(self, **options):
        if not 1 <= options['limit'] <= LEXICAL_LIMIT_MAX:
            raise CommandError(f'--limit must be between 1 and {LEXICAL_LIMIT_MAX}.')
        if options['repeat'] < 1 or options['count'] < 1:
            raise CommandError('--repeat and --count must be positive.')

        rows = []
        # A changing vocabulary must not masquerade as an algorithm regression.
        # This also enforces that a benchmark cannot change mappings or concepts.
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            texts = options['text'] or list(
                SourceCodeConceptMapping.objects.filter(domain_id='Drug')
                .exclude(source_code_description='')
                .order_by('source_code_description')
                .values_list('source_code_description', flat=True)
                .distinct()[:options['count']]
            )
            for text in texts:
                rows.append(self._compare(text, options['limit'], options['repeat']))

        if options['as_json']:
            self.stdout.write(json.dumps({'comparisons': rows}, indent=2))
        else:
            for row in rows:
                self.stdout.write(
                    f"{row['text']!r}: identical={row['identical']} "
                    f"before={row['before_median_ms']:.2f}ms "
                    f"after={row['after_median_ms']:.2f}ms "
                    f"speedup={row['speedup']:.2f}x"
                )
            self.stdout.write(f'{sum(r["identical"] for r in rows)}/{len(rows)} identical candidate lists')
        if any(not row['identical'] for row in rows):
            raise CommandError('Candidate output changed; see comparison results.')

    @staticmethod
    def _compare(text, limit, repeat):
        def run(optimize):
            started = perf_counter()
            found = lexical_candidates(text, 'Drug', limit, optimize_drug_search=optimize)
            return (perf_counter() - started) * 1000, found

        # Warm both paths, then alternate execution order to reduce cache bias.
        _, baseline = run(False)
        _, optimized = run(True)
        identical = baseline == optimized
        times = {False: [], True: []}
        for iteration in range(repeat):
            for optimize in ((False, True) if iteration % 2 == 0 else (True, False)):
                elapsed, found = run(optimize)
                times[optimize].append(elapsed)
                identical &= found == baseline
                if optimize:
                    optimized = found
        old, new = median(times[False]), median(times[True])
        return {
            'text': text, 'limit': limit, 'identical': identical,
            'before_median_ms': old, 'after_median_ms': new,
            'speedup': old / new if new else None,
            'before': baseline, 'after': optimized,
        }
