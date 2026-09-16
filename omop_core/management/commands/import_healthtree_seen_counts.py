"""Set source-code Seen frequencies from a HealthTree CSV snapshot."""
import csv
import json
from collections import defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from omop_core.models import SourceCodeConceptMapping
from omop_core.services.source_vocabularies import ICD10CM_MERGE, VOCABULARY_OID_ALIASES


# Exact HealthTree/FHIR identifiers only. Do not apply One's broad substring
# detectors to an occurrence export: custom systems must remain distinct.
# OIDs are confirmed by One's FHIR/codeSystems/is*.js detectors; canonical
# system URLs also match PROMOP's fhir_export vocabulary mapping.
FHIR_ALIASES = {
    'http://loinc.org': 'LOINC',
    'http://snomed.info/sct': 'SNOMED',
    'http://www.nlm.nih.gov/research/umls/rxnorm': 'RxNorm',
    'http://www.ama-assn.org/go/cpt': 'CPT4',
    'http://hl7.org/fhir/sid/icd-9': 'ICD9CM',
    'http://hl7.org/fhir/sid/icd-10': 'ICD10',
    'http://hl7.org/fhir/sid/icd-10-cm': 'ICD10CM',
    'http://hl7.org/fhir/sid/ndc': 'NDC',
    'CPT': 'CPT4',
}
for oid, vocabulary in {
    '2.16.840.1.113883.6.1': 'LOINC',
    '2.16.840.1.113883.6.96': 'SNOMED',
    '2.16.840.1.113883.6.88': 'RxNorm',
    '2.16.840.1.113883.6.12': 'CPT4',
    '2.16.840.1.113883.6.42': 'ICD9CM',
    '2.16.840.1.113883.6.3': 'ICD10',
    '2.16.840.1.113883.6.90': 'ICD10CM',
}.items():
    FHIR_ALIASES[oid] = FHIR_ALIASES[f'urn:oid:{oid}'] = vocabulary
ALIASES = {**FHIR_ALIASES, **VOCABULARY_OID_ALIASES, '(no system)': ''}


def canonical(vocabulary):
    vocabulary = ALIASES.get(vocabulary, vocabulary)
    return ICD10CM_MERGE.get(vocabulary, vocabulary)


def read_counts(path):
    """Validate the entire snapshot before any database write."""
    counts = {}
    raw_keys = set()
    try:
        with Path(path).open(newline='', encoding='utf-8-sig') as source:
            reader = csv.DictReader(source)
            if not {'code', 'vocabulary', 'occurrences'}.issubset(reader.fieldnames or []):
                raise CommandError('CSV requires code, vocabulary, and occurrences columns.')
            for line, row in enumerate(reader, start=2):
                code = (row.get('code') or '').strip()
                vocabulary = (row.get('vocabulary') or '').strip()
                raw = (row.get('occurrences') or '').strip()
                if not code or not raw.isdecimal() or not 0 <= int(raw) <= 2147483647:
                    raise CommandError(f'Invalid source code or occurrence count on CSV line {line}.')
                raw_key = (row.get('vocabulary') or '', row.get('code') or '')
                key = (ALIASES.get(vocabulary, vocabulary), code)
                if raw_key in raw_keys:
                    raise CommandError(f'Duplicate vocabulary/code identity on CSV line {line}.')
                raw_keys.add(raw_key)
                counts[key] = counts.get(key, 0) + int(raw)
                if counts[key] > 2147483647:
                    raise CommandError(f'Combined occurrence count exceeds storage capacity on CSV line {line}.')
    except (OSError, csv.Error, UnicodeError) as exc:
        raise CommandError(f'Cannot read occurrence CSV: {exc}') from exc
    if not counts:
        raise CommandError('CSV contains no occurrence rows.')
    return counts


def count_index(counts):
    grouped = defaultdict(list)
    for vocabulary, code in counts:
        grouped[(canonical(vocabulary), code)].append((vocabulary, code))
    return grouped


def match_count_key(vocabulary, code, counts, grouped):
    key = (ALIASES.get(vocabulary, vocabulary), code.strip())
    if key in counts:
        return key, 'exact' if key[0] == vocabulary else 'alias'
    alternatives = grouped.get((canonical(vocabulary), code.strip()), [])
    if len(alternatives) == 1:
        return alternatives[0], 'alias'
    return None, 'ambiguous' if alternatives else 'unmatched'


class Command(BaseCommand):
    help = 'Set existing source mappings Seen counts from a HealthTree CSV occurrence snapshot.'

    def add_arguments(self, parser):
        parser.add_argument('csv_path')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--batch-size', type=int, default=1000)
        parser.add_argument('--report', help='Write aggregate counts as JSON (no CSV free text).')

    def handle(self, **options):
        if options['batch_size'] < 1:
            raise CommandError('batch-size must be positive.')
        counts = read_counts(options['csv_path'])
        grouped = count_index(counts)
        stats = dict(input_codes=len(counts), matched_mappings=0, exact_matches=0,
                     alias_matches=0, changed_mappings=0, unchanged_mappings=0,
                     unmatched_mappings=0, ambiguous_mappings=0)
        matched = set()
        with transaction.atomic():
            pending = []
            rows = SourceCodeConceptMapping.objects.only(
                'pk', 'source_vocabulary_id', 'source_code', 'occurrence_count',
            ).iterator(chunk_size=options['batch_size'])
            for mapping in rows:
                key, kind = match_count_key(mapping.source_vocabulary_id, mapping.source_code, counts, grouped)
                if key is None:
                    stats[f'{kind}_mappings'] += 1
                    continue
                matched.add(key)
                stats['matched_mappings'] += 1
                stats[f'{kind}_matches'] += 1
                if mapping.occurrence_count == counts[key]:
                    stats['unchanged_mappings'] += 1
                    continue
                stats['changed_mappings'] += 1
                mapping.occurrence_count = counts[key]
                pending.append(mapping)
                if len(pending) >= options['batch_size']:
                    if not options['dry_run']:
                        SourceCodeConceptMapping.objects.bulk_update(pending, ['occurrence_count'], batch_size=options['batch_size'])
                    pending = []
            if pending and not options['dry_run']:
                SourceCodeConceptMapping.objects.bulk_update(pending, ['occurrence_count'], batch_size=options['batch_size'])
        stats['matched_input_codes'] = len(matched)
        stats['unmatched_input_codes'] = len(counts) - len(matched)
        stats['dry_run'] = options['dry_run']
        report = json.dumps(stats, indent=2, sort_keys=True)
        if options.get('report'):
            Path(options['report']).write_text(report + '\n')
        self.stdout.write(report)
