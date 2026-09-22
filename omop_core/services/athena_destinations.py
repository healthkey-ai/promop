"""Exact, evidence-based Athena lookup, independent of application writes."""
import csv
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from urllib.parse import urlencode, urlparse

import requests

ATHENA_URL = 'https://athena.ohdsi.org'


class LookupFailure(Exception):
    """A provider did not return complete, verifiable evidence."""


def parse_date(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc).date()
    text = str(value or '').strip()
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, '%Y%m%d').date()
    return date.fromisoformat(text[:10])


def active(row, today):
    return (row.get('invalid_reason') in (None, '')
            and parse_date(row['valid_start_date']) <= today <= parse_date(row['valid_end_date']))


def concept_from_api(row):
    required = ('id', 'name', 'domainId', 'vocabularyId', 'conceptClassId',
                'conceptCode', 'standardConcept', 'invalidReason', 'validStart', 'validEnd')
    if not isinstance(row, dict) or any(k not in row for k in required):
        raise LookupFailure('Incomplete Athena concept details')
    return {
        'concept_id': int(row['id']), 'concept_name': row['name'],
        'domain_id': row['domainId'], 'vocabulary_id': row['vocabularyId'],
        'concept_class_id': row['conceptClassId'], 'concept_code': row['conceptCode'],
        'standard_concept': {'Standard': 'S', 'Classification': 'C', 'Non-standard': ''}.get(row['standardConcept'], row['standardConcept']),
        'invalid_reason': None if row['invalidReason'] in ('Valid', '', None) else row['invalidReason'],
        'valid_start_date': parse_date(row['validStart']).isoformat(),
        'valid_end_date': parse_date(row['validEnd']).isoformat(),
    }


@dataclass
class Evidence:
    source: dict | None = None
    targets: list = field(default_factory=list)
    reason: str = 'not_found'
    reference: str = ''


class RateLimit:
    def __init__(self, interval=1.0):
        self.interval = interval
        self.last = None

    def wait(self):
        if self.last is not None:
            time.sleep(max(0, self.interval - (time.monotonic() - self.last)))
        self.last = time.monotonic()


class AthenaAPI:
    def __init__(self, *, interval=1.0, timeout=30, retries=2, session=None):
        self.session = session or requests.Session()
        self.limiter = RateLimit(interval)
        self.timeout, self.retries = timeout, retries

    def get(self, path, params=None):
        for attempt in range(self.retries + 1):
            self.limiter.wait()
            response = self.session.get(ATHENA_URL + '/api/v1/concepts' + path,
                params=params, timeout=self.timeout,
                headers={'User-Agent': 'PRomop-Athena-destination-recovery/1.0', 'Accept': 'application/json'})
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.retries:
                    retry_after = response.headers.get('Retry-After', '')
                    delay = float(retry_after) if retry_after.isdigit() else 2 ** attempt
                    time.sleep(min(60, delay))
                    continue
            response.raise_for_status()
            return response.json()
        raise LookupFailure('Athena retry limit reached')

    def lookup(self, vocabulary, code, today):
        return lookup_remote(self.get, vocabulary, code, today)

    def close(self):
        self.session.close()


def lookup_remote(get, vocabulary, code, today):
    """Read every search page; only exact vocabulary/code and outgoing Maps to count."""
    source_ids = set()
    for page in range(1, 101):
        result = get('', {'vocabulary': vocabulary, 'query': code, 'page': page, 'pageSize': 30})
        if not isinstance(result, dict) or not isinstance(result.get('content'), list):
            raise LookupFailure('Invalid Athena search response')
        pages = result.get('totalPages')
        if type(pages) is not int or pages < 0 or pages > 100:
            raise LookupFailure('Athena search is incomplete or exceeds 100 pages')
        for row in result['content']:
            if not isinstance(row, dict):
                raise LookupFailure('Invalid Athena search result')
            if (row.get('vocabulary') == vocabulary
                    and str(row.get('code', '')).strip().casefold() == code.strip().casefold()):
                source_ids.add(int(row['id']))
        if page >= pages:
            break
    sources = []
    for source_id in sorted(source_ids):
        source = concept_from_api(get('/' + str(source_id)))
        if (source['concept_id'] != source_id or source['vocabulary_id'] != vocabulary
                or source['concept_code'].strip().casefold() != code.strip().casefold()):
            raise LookupFailure('Athena source identity changed between search and detail')
        if active(source, today):
            sources.append(source)
    if not sources:
        return Evidence()
    if len(sources) != 1:
        return Evidence(reason='ambiguous_source')
    source = sources[0]
    source_id = source['concept_id']
    relationships = get(f'/{source_id}/relationships')
    if not isinstance(relationships, dict) or not isinstance(relationships.get('items'), list):
        raise LookupFailure('Invalid Athena relationship response')
    rows = []
    for group in relationships['items']:
        if not isinstance(group, dict) or not isinstance(group.get('relationships'), list):
            raise LookupFailure('Invalid Athena relationship group')
        rows.extend(group['relationships'])
    if any(not isinstance(row, dict) for row in rows):
        raise LookupFailure('Invalid Athena relationship row')
    if relationships.get('count') != len(rows):
        raise LookupFailure('Incomplete Athena relationship response')
    target_ids = {int(r['targetConceptId']) for r in rows if r.get('relationshipId') == 'Maps to'}
    # Athena has already filtered expired relationships; verify each destination
    # independently, because the relationship response has no concept validity.
    targets = []
    for target_id in sorted(target_ids):
        target = concept_from_api(get('/' + str(target_id)))
        if target['concept_id'] != target_id:
            raise LookupFailure('Athena target identity mismatch')
        if active(target, today) and target['standard_concept'] == 'S':
            targets.append(target)
    reason = 'found' if len(targets) == 1 else 'ambiguous_targets' if targets else 'not_found'
    return Evidence(source, targets, reason, f'{ATHENA_URL}/search-terms/terms/{source_id}')


class ArchiveLookup:
    """Three streaming passes; memory holds only selected source/target records."""
    def __init__(self, open_file, keys, today, reference):
        self.results = {}
        self.metadata = {}
        sources = {}
        for row in self.rows(open_file, 'CONCEPT.csv'):
            key = (row['vocabulary_id'], row['concept_code'].strip().casefold())
            if key in keys and active(row, today):
                sources.setdefault(key, []).append(row)
        source_ids = {r['concept_id'] for rows in sources.values() for r in rows}
        edges = {}
        for row in self.rows(open_file, 'CONCEPT_RELATIONSHIP.csv'):
            if (row['concept_id_1'] in source_ids and row['relationship_id'] == 'Maps to'
                    and active(row, today)):
                edges.setdefault(row['concept_id_1'], set()).add(row['concept_id_2'])
        wanted = {target for ids in edges.values() for target in ids}
        targets = {}
        for row in self.rows(open_file, 'CONCEPT.csv'):
            if row['concept_id'] in wanted and row['standard_concept'] == 'S' and active(row, today):
                targets[row['concept_id']] = row
        for key, rows in sources.items():
            if len(rows) != 1:
                self.results[key] = Evidence(reason='ambiguous_source', reference=reference)
                continue
            source = rows[0]
            found = [targets[t] for t in sorted(edges.get(source['concept_id'], [])) if t in targets]
            self.results[key] = Evidence(source, found,
                'found' if len(found) == 1 else 'ambiguous_targets' if found else 'not_found', reference)
        # Reference metadata is optional when those tables are already installed.
        # No placeholder OMOP concepts or vocabulary metadata are fabricated.
        for filename, key in [('VOCABULARY.csv', 'vocabulary_id'), ('DOMAIN.csv', 'domain_id'),
                              ('CONCEPT_CLASS.csv', 'concept_class_id')]:
            try:
                self.metadata[key] = {r[key]: r for r in self.rows(open_file, filename)}
            except (OSError, KeyError):
                self.metadata[key] = {}

    @staticmethod
    def rows(open_file, filename):
        with open_file(filename) as stream:
            reader = csv.DictReader(stream, delimiter='\t')
            if not reader.fieldnames:
                raise LookupFailure(f'Empty Athena file: {filename}')
            for row in reader:
                yield row

    def lookup(self, vocabulary, code, today):
        return self.results.get((vocabulary, code.strip().casefold()), Evidence())


class AthenaBrowser:
    """Read the responses actually delivered while navigating Athena's web UI.

    The browser is closed before returning to Django, so Playwright's event
    loop never overlaps ORM writes. No login or access-control bypass is used.
    """
    def __init__(self, *, interval=1.0, timeout=30, storage_state=None, executable=None):
        self.interval, self.timeout = interval, timeout
        self.storage_state, self.executable = storage_state, executable

    def lookup(self, vocabulary, code, today):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise LookupFailure('Web fallback needs requirements-athena-scrape.txt and playwright install chromium') from exc
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=self.executable)
            try:
                context = browser.new_context(storage_state=self.storage_state)
                page = context.new_page()
                page.set_default_timeout(self.timeout * 1000)
                transport = BrowserTransport(page, self.interval)
                return lookup_remote(transport.get, vocabulary, code, today)
            finally:
                browser.close()


class BrowserTransport:
    def __init__(self, page, interval):
        self.page, self.limiter = page, RateLimit(interval)

    def get(self, path, params=None):
        # The concept page loads its relationship table by default. Capture the
        # site's own complete response, not a second request to the direct API.
        term_path = path.removesuffix('/relationships')
        url = ATHENA_URL + '/search-terms/terms' + term_path
        if params:
            url += '?' + urlencode(params)
        expected_path = '/api/v1/concepts' + path
        self.limiter.wait()
        with self.page.expect_response(lambda response: urlparse(response.url).path == expected_path) as pending:
            self.page.goto(url, wait_until='domcontentloaded')
        response = pending.value
        if response.status != 200:
            raise LookupFailure(f'Athena web response HTTP {response.status}')
        return response.json()
