"""Compare lexical retrieval with and without ingredient narrowing.

Reports both the time and what changed in the results, because narrowing is only
worth having if the answers survive it::

    manage.py benchmark_lexical_retrieval --domain Drug --count 10
    manage.py benchmark_lexical_retrieval --text "ASPIRIN 81 MG ORAL TABLET"
"""
import json
import time
from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import connection

from omop_core.mapping import suggestions
from omop_core.models import Concept, SourceCodeConceptMapping

_WIDE_SQL = """SELECT count(*) FROM concept
WHERE standard_concept = 'S' AND invalid_reason IS NULL AND domain_id = %s
  AND upper(concept_name) %% %s"""

_NARROW_SQL = """SELECT count(*) FROM concept
WHERE standard_concept = 'S' AND invalid_reason IS NULL AND domain_id = %s
  AND upper(concept_name) %%> %s"""


class Command(BaseCommand):
    help: str = 'Measure lexical retrieval with and without ingredient narrowing.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--domain', default='Drug', help='OMOP domain to search.')
        parser.add_argument('--count', type=int, default=10, help='Queue rows to sample.')
        parser.add_argument('--text', action='append', default=[],
                            help='Benchmark this text instead of sampling the queue.')
        parser.add_argument('--explain', action='store_true',
                            help='Also report which index each variant used.')

    def handle(self, **options: Any) -> None:
        texts = options['text'] or self._queue_texts(options['domain'], options['count'])
        if not texts:
            self.stdout.write(self.style.WARNING('No source text to benchmark.'))
            return

        rows: list[dict[str, Any]] = []
        for text in texts:
            narrowed = suggestions.narrowing_text(text)
            if not narrowed:
                self.stdout.write(f'  skip (nothing to narrow): {text[:60]}')
                continue
            rows.append(self._compare(text, narrowed, options))
        self._report(rows)

    def _queue_texts(self, domain_id: str, count: int) -> list[str]:
        """Real descriptions from the mapping queue, so the sample is not invented."""
        values = (
            SourceCodeConceptMapping.objects
            .filter(domain_id=domain_id)
            .exclude(source_code_description='')
            .values_list('source_code_description', flat=True)
            .distinct()[:count]
        )
        return list(values)

    def _compare(self, text: str, narrowed: str, options: dict[str, Any]) -> dict[str, Any]:
        wide_secs, wide_ids = self._time(text, options['domain'], narrowing=False)
        narrow_secs, narrow_ids = self._time(text, options['domain'], narrowing=True)
        kept = len(set(wide_ids) & set(narrow_ids))
        row = {
            'text': text, 'narrowed': narrowed,
            'wide_secs': wide_secs, 'narrow_secs': narrow_secs,
            'wide_n': len(wide_ids), 'kept': kept,
            'same_order': wide_ids == narrow_ids,
            'lost': [i for i in wide_ids if i not in set(narrow_ids)],
            'top_kept': narrow_ids[:1],
        }
        # Names, because a dropped candidate is usually noise that shared only
        # "MG ORAL TABLET" and the only way to tell is to read it.
        row['lost_names'] = list(
            Concept.objects.filter(concept_id__in=row['lost'])
            .values_list('concept_name', flat=True)[:5]
        )
        if options['explain']:
            row['wide_index'] = self._index_for(_WIDE_SQL, options['domain'], text)
            row['narrow_index'] = self._index_for(_NARROW_SQL, options['domain'], narrowed)
        self._write_row(row)
        return row

    def _time(self, text: str, domain_id: str, *, narrowing: bool) -> tuple[float, list[int]]:
        """Best of two, because the first call pays for a cold plan cache."""
        original = suggestions.narrowing_text
        if not narrowing:
            suggestions.narrowing_text = lambda _text: None
        try:
            best, ids = float('inf'), []
            for _ in range(2):
                started = time.monotonic()
                found = suggestions.lexical_candidates(text, domain_id)
                best = min(best, time.monotonic() - started)
                ids = [c['concept_id'] for c in found]
            return best, ids
        finally:
            suggestions.narrowing_text = original

    def _index_for(self, sql: str, domain_id: str, text: str) -> str:
        """Index name the planner picked, so a lost index shows up as a result."""
        cursor = connection.cursor()
        cursor.execute('EXPLAIN (FORMAT JSON) ' + sql, (domain_id, text))
        plan = cursor.fetchone()[0]
        if isinstance(plan, str):
            plan = json.loads(plan)
        flat = json.dumps(plan)
        if 'Seq Scan' in flat:
            return 'SEQ SCAN'
        start = flat.find('"Index Name": "')
        return flat[start + 15:flat.find('"', start + 15)] if start >= 0 else 'unknown'

    def _write_row(self, row: dict[str, Any]) -> None:
        speedup = row['wide_secs'] / row['narrow_secs'] if row['narrow_secs'] else 0
        style = self.style.SUCCESS if row['kept'] == row['wide_n'] else self.style.WARNING
        self.stdout.write(f'  {row["text"][:52]!r} -> {row["narrowed"][:32]!r}')
        self.stdout.write(
            f'    wide {row["wide_secs"]:7.3f}s   narrowed {row["narrow_secs"]:7.3f}s'
            f'   {speedup:6.1f}x'
        )
        self.stdout.write(style(
            f'    kept {row["kept"]}/{row["wide_n"]}  same order {row["same_order"]}'
        ))
        for name in row['lost_names']:
            self.stdout.write(f'      dropped: {name[:70]}')
        if 'wide_index' in row:
            self.stdout.write(f'    index wide={row["wide_index"]} narrowed={row["narrow_index"]}')

    def _report(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        wide = sum(r['wide_secs'] for r in rows)
        narrow = sum(r['narrow_secs'] for r in rows)
        found = sum(r['wide_n'] for r in rows)
        kept = sum(r['kept'] for r in rows)
        lost_rows = [r for r in rows if r['lost']]
        self.stdout.write('')
        self.stdout.write(f'{len(rows)} texts: {wide:.3f}s -> {narrow:.3f}s '
                          f'({wide / narrow if narrow else 0:.1f}x)')
        self.stdout.write(
            f'kept {kept}/{found} candidates; {len(lost_rows)} texts dropped one'
        )
        self.stdout.write(
            'Read the dropped names: sharing only dose and form words is what '
            'narrowing is meant to remove.'
        )
