import csv
import shutil
import sys
import time
import zipfile
from datetime import date
from pathlib import Path

csv.field_size_limit(sys.maxsize)

from django.core.management.base import BaseCommand, CommandError
from django.apps import apps
from django.db import connection
from django.db.models import Count

import logging

from omop_core.models import (
    Vocabulary, Domain, ConceptClass, Concept,
    Relationship, ConceptRelationship, ConceptAncestor, CdmSource,
    VocabularyVersionHistory, record_vocabulary_version_history,
    VocabularyRelease,
)

logger = logging.getLogger(__name__)

VOCAB_SCOPE = frozenset({
    'HemOnc', 'RxNorm', 'RxNorm Extension', 'ATC', 'LOINC', 'UCUM',
    'Visit', 'Type Concept',
    # Clinical-record vocabularies (FHIR sync B3): SNOMED covers conditions,
    # procedures, and allergies; ICD10CM covers EHR-sourced diagnoses. CVX
    # (immunizations) is mapped in the ingest but isn't in the current Athena
    # export, so it loads once a CVX-inclusive bundle is fetched.
    'SNOMED', 'ICD10CM', 'CVX',
    # US procedure and professional-service codes. CPT4 is used as a source
    # vocabulary by the code-mapping and FHIR ingestion paths, so it must be
    # retained whenever the Athena bundle includes it.
    'CPT4',
    # Genomic + oncology coding vocabularies (#459)
    'OMOP Genomic', 'ICDO3', 'NCIt',
    # Oncology staging/grading modifiers + cancer registry
    'Cancer Modifier', 'NAACCR', 'CDISC',
    # MeSH for cytogenetic/genomic field concepts (#803)
    'MeSH',
    # OMOP-generated metadata. 'Episode' carries the Treatment Regimen concept the
    # line-of-therapy episodes point at; 'CDM' carries the field concepts
    # EpisodeEvent references. Both were hand-seeded until the seeder was retired —
    # Athena has them, with the same ids, names and codes.
    'Episode', 'CDM',
    # Demographics. Person.gender_concept / race_concept / ethnicity_concept are
    # standard OMOP FKs, and derivation reads the concept before falling back to
    # the source value — so without these loaded a demographic correction cannot
    # be recorded as anything but free text. All three are present in the Athena
    # bundle and were simply never in scope, so no deployment has ever had them.
    'Gender', 'Race', 'Ethnicity',
})
# These vocabularies underpin the clinical concepts PROMOP presents and maps.
# Do not include CVX here: it is deliberately absent from the current Athena
# bundle, even though the importer supports it when a CVX-inclusive bundle is
# used.
REQUIRED_CLINICAL_VOCABULARIES = frozenset({'LOINC', 'RxNorm', 'SNOMED', 'ICD10CM'})
# A --replace reload deletes the entire vocabulary before applying this filter.
# Keep every LOINC domain relevant to patient-clinical data; the preflight below
# turns any future scope drift into a safe, actionable failure.
LOINC_DOMAIN_SCOPE = frozenset({
    # Core clinical domains (original scope)
    'Measurement', 'Observation', 'Meas Value', 'Procedure', 'Note',
    # Patient demographics — LOINC codes for sex, race, ethnicity, birth date,
    # age, language, phone, email, etc. land in these domains
    'Gender', 'Race', 'Ethnicity', 'Provider',
    # Additional clinical domains — some LOINC codes map to conditions,
    # devices, specimens, or drugs
    'Condition', 'Drug', 'Device', 'Specimen',
    # Metadata and type concepts
    'Metadata', 'Type Concept',
})
BATCH = 100_000
PROGRESS_EVERY = 500_000
DEFAULT_GDRIVE_URL = 'https://drive.google.com/drive/u/0/folders/1HoRWGepqcH3pMKK03KNb1oWpaVs0Avl7'
_INCOMING_CONCEPT_TABLE = '_incoming_athena_concept_ids'
_VOCABULARY_CONCEPT_REFERENCE_TABLES = frozenset({
    'concept_relationship', 'concept_ancestor', 'concept_synonym',
    'drug_strength', 'source_to_concept_map',
})

# Defaults for the single self-describing cdm_source row. Kept in sync with
# migration 0112_seed_cdm_source.
_CDM_SOURCE_DEFAULTS = {
    'cdm_source_name': 'PRomop — Decision-Ready Longitudinal Patient Record',
    'cdm_holder': 'HealthKey, Inc.',
    'source_description': (
        'PRomop longitudinal patient record on the OMOP CDM 5.4 clinical '
        'tables with OHDSI oncology extensions and a derived PatientRecord projection.'
    ),
    'source_documentation_reference': 'https://github.com/healthkey-ai',
    'cdm_etl_reference': 'https://github.com/healthkey-ai',
    'cdm_release_date': date(2026, 1, 1),
    'cdm_version': '5.4',
}


def _parse_date(s):
    s = str(s).strip()
    if len(s) == 8 and s.isdigit():
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    return date.fromisoformat(s)


def _open_tsv(base, filename):
    path = Path(base) / filename
    if not path.exists():
        raise CommandError(f'Required file not found: {path}')
    return open(path, encoding='utf-8', newline='')


def _download_gcs_blob(bucket, filename, log):
    blob = bucket.blob(filename)
    if not blob.exists():
        raise CommandError(f'Required blob not found: gs://{bucket.name}/{filename}')
    dest = Path('/tmp/vocab') / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    size_mb = (blob.size or 0) / 1048576
    log(f'  Downloading {filename} ({size_mb:.0f}MB)...')
    t = time.monotonic()
    blob.download_to_filename(str(dest))
    elapsed = time.monotonic() - t
    log(f'  Downloaded {filename} in {elapsed:.0f}s.')
    return open(dest, encoding='utf-8', newline='')


def _extract_vocabulary_archive(archive, extract_dir, log):
    archive = Path(archive)
    if not archive.exists():
        raise CommandError(f'Vocabulary archive not found: {archive}')

    shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)

    log(f'  Extracting {archive.name}...')
    extract_root = extract_dir.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            target = (extract_dir / member.filename).resolve()
            try:
                target.relative_to(extract_root)
            except ValueError:
                raise CommandError(f'Unsafe path in vocabulary archive: {member.filename}')
        zf.extractall(extract_dir)

    required = 'CONCEPT.csv'
    candidates = [p.parent for p in extract_dir.rglob(required)]
    if not candidates:
        raise CommandError(f'Extracted vocabulary archive did not contain {required}.')
    if len(candidates) > 1:
        log(f'  Found multiple {required} files; using {candidates[0]}.')
    return str(candidates[0])


def _download_gdrive_vocabulary(url, log):
    """Download a Google Drive folder/file containing an Athena vocabulary zip."""
    try:
        import gdown
    except ImportError as exc:
        raise CommandError(
            'Google Drive vocabulary loading requires gdown. Install dependencies '
            'from requirements.txt, then rerun with --gdrive.'
        ) from exc

    download_dir = Path('/tmp/vocab/gdrive')
    extract_dir = Path('/tmp/vocab/gdrive-extracted')
    shutil.rmtree(download_dir, ignore_errors=True)
    shutil.rmtree(extract_dir, ignore_errors=True)
    download_dir.mkdir(parents=True, exist_ok=True)
    extract_dir.mkdir(parents=True, exist_ok=True)

    log(f'Loading Athena vocabulary archive from Google Drive: {url}')
    if '/folders/' in url:
        result = gdown.download_folder(url=url, output=str(download_dir), quiet=False, use_cookies=False)
        if result is None:
            raise CommandError(f'Google Drive folder download failed: {url}')
    else:
        filename = gdown.download(url=url, output=str(download_dir / 'athena-vocabulary.zip'), quiet=False)
        if not filename:
            raise CommandError(f'Google Drive file download failed: {url}')

    zips = sorted(download_dir.rglob('*.zip'))
    if not zips:
        raise CommandError(
            f'No .zip file found after downloading Google Drive vocabulary source: {url}'
        )
    archive = zips[0]
    if len(zips) > 1:
        log(
            f'  Found {len(zips)} zip files; using first by name: {archive.name}. '
            'Pass a direct Google Drive file URL to select a specific zip.'
        )
    return _extract_vocabulary_archive(archive, extract_dir, log)


def _header_index(header_row):
    """Build column-name → index map from a TSV header row."""
    return {col: i for i, col in enumerate(header_row)}


def _concept_in_scope(vid, concept_code, concept_class_id, domain_id):
    if vid not in VOCAB_SCOPE:
        return False
    if vid == 'ATC':
        return concept_code.startswith('L')
    if vid == 'LOINC':
        return domain_id in LOINC_DOMAIN_SCOPE
    return True


def _copy_rows(table, columns, rows, log, direct=False):
    """COPY rows into table. direct=True skips the conflict-tolerant temp table."""
    if not rows:
        return
    connection.ensure_connection()
    cols = ', '.join(columns)
    with connection.connection.cursor() as cur:
        if direct:
            with cur.copy(f'COPY {table} ({cols}) FROM STDIN') as copy:
                for row in rows:
                    copy.write_row(row)
        else:
            tmp = f'_tmp_{table}'
            cur.execute(f'DROP TABLE IF EXISTS {tmp}')
            cur.execute(
                f'CREATE TEMP TABLE {tmp} AS SELECT {cols} FROM {table} WHERE false'
            )
            with cur.copy(f'COPY {tmp} ({cols}) FROM STDIN') as copy:
                for row in rows:
                    copy.write_row(row)
            cur.execute(f'INSERT INTO {table} ({cols}) SELECT {cols} FROM {tmp} ON CONFLICT DO NOTHING')
            cur.execute(f'DROP TABLE {tmp}')


class Command(BaseCommand):
    help = 'Load OHDSI Athena vocabulary TSV files into OMOP vocabulary tables'

    def add_arguments(self, parser):
        parser.add_argument('--path',
                            help='Directory containing Athena TSV files')
        parser.add_argument('--archive',
                            help='Zip archive containing Athena TSV files')
        parser.add_argument('--bucket',
                            help='GCS bucket name to stream files from (alternative to --path)')
        parser.add_argument('--gdrive', nargs='?', const=DEFAULT_GDRIVE_URL,
                            help=(
                                'Google Drive folder or file URL containing a zipped '
                                f'Athena vocabulary export. Defaults to {DEFAULT_GDRIVE_URL}.'
                            ))
        parser.add_argument('--replace', action='store_true', help=(
            'Remove Athena concepts absent from the incoming release after loading. '
            'Patient records are retained and stale concept references are cleared.'
        ))
        parser.add_argument('--dry-run', action='store_true', dest='dry_run',
                            help='Count rows without writing to DB')
        parser.add_argument('--skip-clinical-vocabulary-verification', action='store_true',
                            help='Do not verify that required clinical vocabularies loaded')
        parser.add_argument('--concepts-only', action='store_true',
                            help=(
                                'Load vocabulary/domain/concept_class/concept and stop. '
                                'Skips concept_relationship, concept_ancestor, '
                                'concept_synonym and drug_strength.'
                            ))

    def handle(self, *args, **options):
        base = options['path']
        archive = options['archive']
        bucket_name = options['bucket']
        gdrive_url = options['gdrive']
        replace = options['replace']
        dry_run = options['dry_run']
        skip_clinical_vocabulary_verification = options['skip_clinical_vocabulary_verification']

        sources = [bool(base), bool(archive), bool(bucket_name), bool(gdrive_url)]
        if sum(sources) != 1:
            raise CommandError('Provide exactly one of --path, --archive, --bucket, or --gdrive')

        self._gcs_bucket = None
        if bucket_name:
            from google.cloud import storage as gcs
            self._gcs_bucket = gcs.Client().bucket(bucket_name)
            self._log(f'Loading from gs://{bucket_name}/ (download-one-process-delete)')
        if gdrive_url:
            base = _download_gdrive_vocabulary(gdrive_url, self._log)
        if archive:
            base = _extract_vocabulary_archive(
                archive, Path('/tmp/vocab/archive-extracted'), self._log
            )

        t0 = time.monotonic()
        self._build_start = time.time()  # wall-clock for VocabularyRelease

        self._base = base

        # Never TRUNCATE vocabulary tables: concept is referenced by patient data,
        # and PostgreSQL TRUNCATE ... CASCADE deletes those dependent rows.
        self._direct = False

        if replace:
            logger.warning(
                '--replace removes Athena concepts missing from the incoming '
                'release after safely clearing their references from patient data.'
            )
            self._validate_replace_loinc_scope()
            self._validate_replace_vocab_coverage()
            if not dry_run:
                self._replace_tracking = True
                self._create_incoming_concept_table()

        counts = {
            'relationship':         self._load_relationships(dry_run),
            'vocabulary':           self._load_vocabularies(dry_run),
            'domain':               self._load_domains(dry_run),
            'concept_class':        self._load_concept_classes(dry_run),
            'concept':              self._load_concepts(dry_run),
        }
        if options['concepts_only']:
            # Adding a small vocabulary to VOCAB_SCOPE means ~1.5k new concepts,
            # but the relationship, ancestor and synonym files are ~26M rows that
            # would be re-streamed to change almost nothing. Those tables are
            # keyed on concept membership, so they stay valid; a later full load
            # backfills anything the new concepts participate in.
            self.stdout.write(self.style.WARNING(
                '  --concepts-only: skipping concept_relationship, '
                'concept_ancestor, concept_synonym and drug_strength.'
            ))
        else:
            counts.update({
                'concept_relationship': self._load_concept_relationships(dry_run),
                'concept_ancestor':     self._load_concept_ancestors(dry_run),
                'concept_synonym':      self._load_concept_synonym(dry_run),
                'drug_strength':        self._load_drug_strength(dry_run),
                'source_to_concept_map': self._load_source_to_concept_map(dry_run),
            })
        if not dry_run:
            self._seed_concept_zero()
            if replace:
                self._remove_stale_concepts()
            self._sync_cdm_source_metadata()
            if not skip_clinical_vocabulary_verification:
                self._verify_required_clinical_vocabularies()
            self._record_version_history(replace)
            self._publish_release(counts)
            self._load_code_mappings(options['verbosity'])
        elapsed = time.monotonic() - t0
        verb = 'would load' if dry_run else 'loaded'
        total = sum(counts.values())
        self._log('')
        self._log('=' * 60)
        self._log('  LOAD SUMMARY')
        self._log('=' * 60)
        for table, n in counts.items():
            self._log(f'  {table:.<30s} {n:>12,} rows {verb}')
        self._log(f'  {"TOTAL":.<30s} {total:>12,} rows')
        self._log('-' * 60)
        if hasattr(self, '_vocab_counts') and self._vocab_counts:
            self._log('  Concepts by vocabulary:')
            for vid, n in sorted(self._vocab_counts.items(), key=lambda x: -x[1]):
                self._log(f'    {vid:.<28s} {n:>12,}')
            self._log(f'  Scanned {self._concept_scanned:,} concept rows ({total and counts["concept"] * 100 // self._concept_scanned or 0}% in scope)')
        self._log(f'  Elapsed: {elapsed:.0f}s')
        self._log('=' * 60)

    def _log(self, msg):
        self.stdout.write(msg)
        self.stdout.flush()

    def _verify_required_clinical_vocabularies(self):
        """Fail the load when a partial Athena bundle omits core clinical vocabularies."""
        counts = dict(
            Concept.objects.filter(vocabulary_id__in=REQUIRED_CLINICAL_VOCABULARIES)
            .values('vocabulary_id')
            .annotate(total=Count('concept_id'))
            .values_list('vocabulary_id', 'total')
        )
        missing = sorted(REQUIRED_CLINICAL_VOCABULARIES - counts.keys())
        if missing:
            raise CommandError(
                'Required clinical vocabularies are missing after the load: '
                f"{', '.join(missing)}. This database cannot reliably map clinical "
                'conditions, diagnoses, medications, and labs. Fetch an Athena bundle '
                'that includes the missing vocabularies and rerun this command without '
                '--replace, which would remove the loaded concepts this database '
                'still maps clinical data against.'
            )
        self._log(
            '  verified required clinical vocabularies: ' +
            ', '.join(f'{vid} ({counts[vid]:,})' for vid in sorted(counts))
        )

    def _open(self, filename):
        if self._gcs_bucket:
            return _download_gcs_blob(self._gcs_bucket, filename, self._log)
        return _open_tsv(self._base, filename)

    def _cleanup(self, filename):
        if self._gcs_bucket:
            tmp = Path('/tmp/vocab') / filename
            if tmp.exists():
                tmp.unlink()
                self._log(f'  Cleaned up {filename}.')

    # Kept for the focused legacy unit tests that exercise local-concept
    # serialization. The replacement path no longer calls either helper.
    _HK_CONCEPT_COLS = (
        'concept_id', 'concept_name', 'domain_id', 'vocabulary_id',
        'concept_class_id', 'standard_concept', 'concept_code',
        'valid_start_date', 'valid_end_date', 'invalid_reason', 'source',
    )

    def _save_healthkey_concepts(self):
        return list(
            Concept.objects.filter(source='HealthKey').values_list(*self._HK_CONCEPT_COLS)
        )

    def _restore_healthkey_concepts(self):
        if not self._hk_concepts:
            return
        for vid in {row[3] for row in self._hk_concepts}:
            Vocabulary.objects.get_or_create(
                vocabulary_id=vid,
                defaults={'vocabulary_name': vid, 'vocabulary_concept_id': 0},
            )
        for did in {row[2] for row in self._hk_concepts}:
            Domain.objects.get_or_create(
                domain_id=did,
                defaults={'domain_name': did, 'domain_concept_id': 0},
            )
        for cid in {row[4] for row in self._hk_concepts}:
            ConceptClass.objects.get_or_create(
                concept_class_id=cid,
                defaults={'concept_class_name': cid, 'concept_class_concept_id': 0},
            )
        _copy_rows('concept', self._HK_CONCEPT_COLS, self._hk_concepts, self._log)

    def _create_incoming_concept_table(self):
        """Create a temporary, database-side index of incoming concept IDs."""
        with connection.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS {_INCOMING_CONCEPT_TABLE}')
            cur.execute(
                f'CREATE TEMP TABLE {_INCOMING_CONCEPT_TABLE} '
                '(concept_id integer PRIMARY KEY)'
            )

    def _record_incoming_concept_ids(self, rows):
        if not rows:
            return
        connection.ensure_connection()
        with connection.connection.cursor() as cur:
            with cur.copy(
                f'COPY {_INCOMING_CONCEPT_TABLE} (concept_id) FROM STDIN'
            ) as copy:
                for concept_id in rows:
                    copy.write_row((concept_id,))

    def _remove_stale_concepts(self):
        """Delete stale Athena concepts without deleting patient-owned rows."""
        stale_sql = (
            'SELECT c.concept_id FROM concept c '
            'WHERE c.vocabulary_id = ANY(%s) AND c.source IS NULL '
            f'AND NOT EXISTS (SELECT 1 FROM {_INCOMING_CONCEPT_TABLE} incoming '
            'WHERE incoming.concept_id = c.concept_id)'
        )
        params = [list(VOCAB_SCOPE)]
        qn = connection.ops.quote_name
        references = []
        for model in apps.get_models():
            for field in model._meta.get_fields():
                if (
                    not field.auto_created
                    and getattr(field, 'many_to_one', False)
                    and field.related_model is Concept
                ):
                    references.append((model, field))

        with connection.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM ({stale_sql}) stale', params)
            stale_count = cur.fetchone()[0]
            if not stale_count:
                cur.execute(f'DROP TABLE {_INCOMING_CONCEPT_TABLE}')
                self._log('  --replace: no stale Athena concepts to remove.')
                return

            for model, field in references:
                table = qn(model._meta.db_table)
                column = qn(field.column)
                if model._meta.db_table in _VOCABULARY_CONCEPT_REFERENCE_TABLES:
                    cur.execute(
                        f'DELETE FROM {table} WHERE {column} IN ({stale_sql})', params
                    )
                else:
                    value = 'NULL' if field.null else '0'
                    cur.execute(
                        f'UPDATE {table} SET {column} = {value} '
                        f'WHERE {column} IN ({stale_sql})', params
                    )
            cur.execute(f'DELETE FROM concept WHERE concept_id IN ({stale_sql})', params)
            cur.execute(f'DROP TABLE {_INCOMING_CONCEPT_TABLE}')
        self._log(
            f'  --replace: removed {stale_count:,} stale Athena concept(s); '
            'patient-owned rows were retained.'
        )

    def _validate_replace_loinc_scope(self):
        """Abort before replacement if loaded LOINC data falls outside the filter.

        `--replace` removes concepts absent from the configured incoming scope.
        Without this check, adding a new LOINC domain to a live database can make
        a later ordinary replacement silently delete its concepts.
        """
        excluded = Concept.objects.filter(vocabulary_id='LOINC').exclude(
            domain_id__in=LOINC_DOMAIN_SCOPE,
        )
        count = excluded.count()
        if not count:
            return

        domains = list(
            excluded.order_by('domain_id').values_list('domain_id', flat=True).distinct()
        )
        raise CommandError(
            '--replace aborted before removing stale concepts: '
            f'{count:,} loaded LOINC concept(s) use domain(s) outside '
            f'LOINC_DOMAIN_SCOPE: {", ".join(domains)}. '
            'Add the required domain(s) to LOINC_DOMAIN_SCOPE, then rerun.'
        )

    def _validate_replace_vocab_coverage(self):
        """Abort before replacement if the incoming CSV would drop vocabularies.

        If the Athena CSV omits a vocabulary that has existing rows in the DB,
        replacement would remove every loaded Athena concept in that vocabulary.
        This pre-flight check compares the vocabularies present in the DB against
        what the incoming CONCEPT.csv will provide and aborts that unsafe case.
        """
        # Vocabularies currently stored in the DB (within VOCAB_SCOPE).
        db_vocabs = set(
            Concept.objects.filter(vocabulary_id__in=VOCAB_SCOPE)
            .exclude(source='HealthKey')
            .values_list('vocabulary_id', flat=True)
            .distinct()
        )
        if not db_vocabs:
            return  # Nothing to lose.

        # Scan the incoming CONCEPT.csv to find which vocabularies it covers.
        csv_vocabs = set()
        try:
            f = self._open('CONCEPT.csv')
        except CommandError:
            raise CommandError(
                '--replace aborted: CONCEPT.csv not found. Cannot verify '
                'vocabulary coverage before removing stale concepts.'
            )
        with f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            i_vid = idx['vocabulary_id']
            for cols in reader:
                try:
                    vid = cols[i_vid]
                except IndexError:
                    continue
                if vid in VOCAB_SCOPE:
                    csv_vocabs.add(vid)

        missing = sorted(db_vocabs - csv_vocabs)
        if missing:
            raise CommandError(
            '--replace aborted before removing stale concepts: the incoming CONCEPT.csv '
                f'contains no rows for {len(missing)} vocabulary/ies that have '
                f'existing concepts in the database: {", ".join(missing)}. '
                'Replacement would permanently delete those concepts. Either '
                'fetch an Athena bundle that includes the missing vocabularies, '
                'or run without --replace to upsert.'
            )
        self._log(
            f'  --replace pre-flight: all {len(db_vocabs)} in-scope DB '
            f'vocabularies covered by incoming CSV.'
        )

    def _seed_concept_zero(self):
        Vocabulary.objects.get_or_create(
            vocabulary_id='None',
            defaults={'vocabulary_name': 'None', 'vocabulary_concept_id': 0},
        )
        Domain.objects.get_or_create(
            domain_id='Metadata',
            defaults={'domain_name': 'Metadata', 'domain_concept_id': 0},
        )
        ConceptClass.objects.get_or_create(
            concept_class_id='Undefined',
            defaults={'concept_class_name': 'Undefined', 'concept_class_concept_id': 0},
        )
        _, created = Concept.objects.get_or_create(
            concept_id=0,
            defaults={
                'concept_name': 'No matching concept',
                'domain_id': 'Metadata',
                'vocabulary_id': 'None',
                'concept_class_id': 'Undefined',
                'concept_code': 'No matching concept',
                'valid_start_date': '1970-01-01',
                'valid_end_date': '2099-12-31',
            },
        )
        if created:
            self._log('Seeded concept_id=0 (No matching concept)')

    def _load_relationships(self, dry_run):
        self._log('Loading RELATIONSHIP.csv...')
        t = time.monotonic()
        count = 0
        rows = []
        with self._open('RELATIONSHIP.csv') as f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            for cols in reader:
                try:
                    row = (
                        cols[idx['relationship_id']][:20],
                        cols[idx['relationship_name']][:255],
                        int(cols[idx['is_hierarchical']] or 0),
                        int(cols[idx['defines_ancestry']] or 0),
                        cols[idx['reverse_relationship_id']][:20],
                        int(cols[idx['relationship_concept_id']] or 0),
                    )
                except (ValueError, KeyError, IndexError) as exc:
                    self._log(f'Warning: skipping malformed relationship row: {exc}')
                    continue
                count += 1
                if not dry_run:
                    rows.append(row)
                    if len(rows) >= BATCH:
                        _copy_rows('relationship',
                                   ('relationship_id', 'relationship_name', 'is_hierarchical',
                                    'defines_ancestry', 'reverse_relationship_id', 'relationship_concept_id'),
                                   rows, self._log, direct=self._direct)
                        rows = []
        if not dry_run:
            _copy_rows('relationship',
                       ('relationship_id', 'relationship_name', 'is_hierarchical',
                        'defines_ancestry', 'reverse_relationship_id', 'relationship_concept_id'),
                       rows, self._log, direct=self._direct)
        self._cleanup('RELATIONSHIP.csv')
        self._log(f'  RELATIONSHIP.csv: {count:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_vocabularies(self, dry_run):
        self._log('Loading VOCABULARY.csv...')
        t = time.monotonic()
        count = 0
        rows = []
        self._cdm_vocab_version = None
        try:
            f = self._open('VOCABULARY.csv')
        except CommandError:
            self._log('  VOCABULARY.csv not found, skipping.')
            return 0
        with f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            for cols in reader:
                vid = cols[idx['vocabulary_id']]
                # 'None' is out of scope for concept loading but its row carries
                # the CDM release version in vocabulary_version — keep it so
                # cdm_source can self-describe.
                if vid not in VOCAB_SCOPE and vid != 'None':
                    continue
                count += 1
                version = (cols[idx.get('vocabulary_version', -1)] if 'vocabulary_version' in idx else '')[:255] or ''
                if vid == 'None' and version:
                    self._cdm_vocab_version = version
                if not dry_run:
                    rows.append((
                        vid[:20],
                        cols[idx['vocabulary_name']][:255],
                        (cols[idx.get('vocabulary_reference', -1)] if 'vocabulary_reference' in idx else '')[:255] or '',
                        version,
                        int(cols[idx['vocabulary_concept_id']] or 0) if 'vocabulary_concept_id' in idx else 0,
                    ))
        if not dry_run and rows:
            _copy_rows('vocabulary',
                       ('vocabulary_id', 'vocabulary_name', 'vocabulary_reference',
                        'vocabulary_version', 'vocabulary_concept_id'),
                       rows, self._log, direct=self._direct)
        if not dry_run and self._cdm_vocab_version:
            # The 'None' row usually pre-exists (seeded by migration 0068 with no
            # version), so the COPY above conflicts on the PK and no-ops — update
            # it directly so the CDM release version actually lands.
            Vocabulary.objects.filter(vocabulary_id='None').update(
                vocabulary_version=self._cdm_vocab_version
            )
        self._cleanup('VOCABULARY.csv')
        self._log(f'  VOCABULARY.csv: {count:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_domains(self, dry_run):
        self._log('Loading DOMAIN.csv...')
        t = time.monotonic()
        count = 0
        rows = []
        try:
            f = self._open('DOMAIN.csv')
        except CommandError:
            self._log('  DOMAIN.csv not found, skipping.')
            return 0
        with f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            for cols in reader:
                count += 1
                if not dry_run:
                    rows.append((
                        cols[idx['domain_id']][:20],
                        cols[idx['domain_name']][:255],
                        int(cols[idx['domain_concept_id']] or 0) if 'domain_concept_id' in idx else 0,
                    ))
        if not dry_run and rows:
            _copy_rows('domain',
                       ('domain_id', 'domain_name', 'domain_concept_id'),
                       rows, self._log, direct=self._direct)
        self._cleanup('DOMAIN.csv')
        self._log(f'  DOMAIN.csv: {count:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_concept_classes(self, dry_run):
        self._log('Loading CONCEPT_CLASS.csv...')
        t = time.monotonic()
        count = 0
        rows = []
        try:
            f = self._open('CONCEPT_CLASS.csv')
        except CommandError:
            self._log('  CONCEPT_CLASS.csv not found, skipping.')
            return 0
        with f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            for cols in reader:
                count += 1
                if not dry_run:
                    rows.append((
                        cols[idx['concept_class_id']][:20],
                        cols[idx['concept_class_name']][:255],
                        int(cols[idx['concept_class_concept_id']] or 0) if 'concept_class_concept_id' in idx else 0,
                    ))
        if not dry_run and rows:
            _copy_rows('concept_class',
                       ('concept_class_id', 'concept_class_name', 'concept_class_concept_id'),
                       rows, self._log, direct=self._direct)
        self._cleanup('CONCEPT_CLASS.csv')
        self._log(f'  CONCEPT_CLASS.csv: {count:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_concepts(self, dry_run):
        self._log('Loading CONCEPT.csv...')
        t = time.monotonic()
        count = 0
        scanned = 0
        vocab_counts = {}
        rows = []
        incoming_ids = []
        with self._open('CONCEPT.csv') as f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            i_id = idx['concept_id']
            i_name = idx['concept_name']
            i_domain = idx['domain_id']
            i_vocab = idx['vocabulary_id']
            i_class = idx['concept_class_id']
            i_std = idx['standard_concept']
            i_code = idx['concept_code']
            i_start = idx['valid_start_date']
            i_end = idx['valid_end_date']
            i_invalid = idx['invalid_reason']
            for cols in reader:
                scanned += 1
                if scanned % PROGRESS_EVERY == 0:
                    self._log(f'  concepts: scanned {scanned:,}, {count:,} in scope ({time.monotonic() - t:.0f}s)...')
                vid = cols[i_vocab]
                if not _concept_in_scope(vid, cols[i_code], cols[i_class], cols[i_domain]):
                    continue
                try:
                    concept_id = int(cols[i_id])
                    start = _parse_date(cols[i_start])
                    end = _parse_date(cols[i_end])
                except (ValueError, IndexError) as exc:
                    self._log(f'Warning: skipping malformed concept row: {exc}')
                    continue
                count += 1
                vocab_counts[vid] = vocab_counts.get(vid, 0) + 1
                if not dry_run:
                    if hasattr(self, '_replace_tracking'):
                        incoming_ids.append(concept_id)
                    std = cols[i_std][:1] if cols[i_std] else None
                    inv = cols[i_invalid][:1] if cols[i_invalid] else None
                    rows.append((
                        concept_id,
                        cols[i_name][:255],
                        cols[i_domain][:20],
                        vid[:20],
                        cols[i_class][:20],
                        std,
                        cols[i_code][:50],
                        start.isoformat(),
                        end.isoformat(),
                        inv,
                    ))
                    if len(rows) >= BATCH:
                        _copy_rows('concept',
                                   ('concept_id', 'concept_name', 'domain_id', 'vocabulary_id',
                                    'concept_class_id', 'standard_concept', 'concept_code',
                                    'valid_start_date', 'valid_end_date', 'invalid_reason'),
                                   rows, self._log, direct=self._direct)
                        if incoming_ids:
                            self._record_incoming_concept_ids(incoming_ids)
                            incoming_ids = []
                        rows = []
        if not dry_run:
            _copy_rows('concept',
                       ('concept_id', 'concept_name', 'domain_id', 'vocabulary_id',
                        'concept_class_id', 'standard_concept', 'concept_code',
                        'valid_start_date', 'valid_end_date', 'invalid_reason'),
                       rows, self._log, direct=self._direct)
            if incoming_ids:
                self._record_incoming_concept_ids(incoming_ids)
        self._cleanup('CONCEPT.csv')
        self._log(f'  concepts: {count:,} loaded from {scanned:,} rows in {time.monotonic() - t:.0f}s')
        self._vocab_counts = vocab_counts
        self._concept_scanned = scanned
        return count

    def _load_concept_relationships(self, dry_run):
        self._log('Loading CONCEPT_RELATIONSHIP.csv...')
        t = time.monotonic()
        loaded_ids = set(
            Concept.objects.filter(vocabulary_id__in=VOCAB_SCOPE)
                           .values_list('concept_id', flat=True)
        )
        self._log(f'  {len(loaded_ids):,} concept IDs in filter set')
        count = 0
        scanned = 0
        rows = []
        with self._open('CONCEPT_RELATIONSHIP.csv') as f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            i_c1 = idx['concept_id_1']
            i_c2 = idx['concept_id_2']
            i_rel = idx['relationship_id']
            i_start = idx['valid_start_date']
            i_end = idx['valid_end_date']
            i_invalid = idx['invalid_reason']
            for cols in reader:
                scanned += 1
                if scanned % PROGRESS_EVERY == 0:
                    self._log(f'  relationships: scanned {scanned:,}, {count:,} matched ({time.monotonic() - t:.0f}s)...')
                try:
                    c1 = int(cols[i_c1])
                    c2 = int(cols[i_c2])
                except (ValueError, IndexError):
                    continue
                if c1 not in loaded_ids or c2 not in loaded_ids:
                    continue
                count += 1
                if not dry_run:
                    inv = cols[i_invalid][:1] if cols[i_invalid] else None
                    rows.append((
                        c1, c2,
                        cols[i_rel][:20],
                        _parse_date(cols[i_start]).isoformat(),
                        _parse_date(cols[i_end]).isoformat(),
                        inv,
                    ))
                    if len(rows) >= BATCH:
                        _copy_rows('concept_relationship',
                                   ('concept_id_1', 'concept_id_2', 'relationship_id',
                                    'valid_start_date', 'valid_end_date', 'invalid_reason'),
                                   rows, self._log, direct=self._direct)
                        rows = []
        if not dry_run:
            _copy_rows('concept_relationship',
                       ('concept_id_1', 'concept_id_2', 'relationship_id',
                        'valid_start_date', 'valid_end_date', 'invalid_reason'),
                       rows, self._log, direct=self._direct)
        self._cleanup('CONCEPT_RELATIONSHIP.csv')
        self._log(f'  relationships: {count:,} loaded from {scanned:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_concept_ancestors(self, dry_run):
        self._log('Loading CONCEPT_ANCESTOR.csv...')
        t = time.monotonic()
        loaded_ids = set(
            Concept.objects.filter(vocabulary_id__in=VOCAB_SCOPE)
                           .values_list('concept_id', flat=True)
        )
        self._log(f'  {len(loaded_ids):,} concept IDs in filter set')
        count = 0
        scanned = 0
        rows = []
        with self._open('CONCEPT_ANCESTOR.csv') as f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            i_anc = idx['ancestor_concept_id']
            i_desc = idx['descendant_concept_id']
            i_min = idx['min_levels_of_separation']
            i_max = idx['max_levels_of_separation']
            for cols in reader:
                scanned += 1
                if scanned % PROGRESS_EVERY == 0:
                    self._log(f'  ancestors: scanned {scanned:,}, {count:,} matched ({time.monotonic() - t:.0f}s)...')
                try:
                    anc = int(cols[i_anc])
                    desc = int(cols[i_desc])
                except (ValueError, IndexError):
                    continue
                if anc not in loaded_ids or desc not in loaded_ids:
                    continue
                count += 1
                if not dry_run:
                    try:
                        min_sep = int(cols[i_min])
                        max_sep = int(cols[i_max])
                    except (ValueError, IndexError):
                        continue
                    rows.append((anc, desc, min_sep, max_sep))
                    if len(rows) >= BATCH:
                        _copy_rows('concept_ancestor',
                                   ('ancestor_concept_id', 'descendant_concept_id',
                                    'min_levels_of_separation', 'max_levels_of_separation'),
                                   rows, self._log, direct=self._direct)
                        rows = []
        if not dry_run:
            _copy_rows('concept_ancestor',
                       ('ancestor_concept_id', 'descendant_concept_id',
                        'min_levels_of_separation', 'max_levels_of_separation'),
                       rows, self._log, direct=self._direct)
        self._cleanup('CONCEPT_ANCESTOR.csv')
        self._log(f'  ancestors: {count:,} loaded from {scanned:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_concept_synonym(self, dry_run):
        self._log('Loading CONCEPT_SYNONYM.csv...')
        t = time.monotonic()
        try:
            f = self._open('CONCEPT_SYNONYM.csv')
        except CommandError:
            self._log('  CONCEPT_SYNONYM.csv not found, skipping.')
            return 0
        loaded_ids = set(Concept.objects.values_list('concept_id', flat=True))
        count = scanned = 0
        rows = []
        cols_out = ('concept_id', 'concept_synonym_name', 'language_concept_id')
        with f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            i_cid, i_name, i_lang = idx['concept_id'], idx['concept_synonym_name'], idx['language_concept_id']
            for row in reader:
                scanned += 1
                if scanned % PROGRESS_EVERY == 0:
                    self._log(f'  synonyms: scanned {scanned:,}, {count:,} matched ({time.monotonic() - t:.0f}s)...')
                try:
                    cid, lang = int(row[i_cid]), int(row[i_lang])
                except (ValueError, IndexError):
                    continue
                # both concept and language must be loaded to satisfy FK constraints
                if cid not in loaded_ids or lang not in loaded_ids:
                    continue
                count += 1
                if not dry_run:
                    rows.append((cid, row[i_name][:1000], lang))
                    if len(rows) >= BATCH:
                        _copy_rows('concept_synonym', cols_out, rows, self._log, direct=self._direct)
                        rows = []
        if not dry_run:
            _copy_rows('concept_synonym', cols_out, rows, self._log, direct=self._direct)
        self._cleanup('CONCEPT_SYNONYM.csv')
        self._log(f'  synonyms: {count:,} loaded from {scanned:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_drug_strength(self, dry_run):
        self._log('Loading DRUG_STRENGTH.csv...')
        t = time.monotonic()
        try:
            f = self._open('DRUG_STRENGTH.csv')
        except CommandError:
            self._log('  DRUG_STRENGTH.csv not found, skipping.')
            return 0
        loaded_ids = set(Concept.objects.values_list('concept_id', flat=True))
        count = scanned = 0
        rows = []
        cols_out = (
            'drug_concept_id', 'ingredient_concept_id', 'amount_value', 'amount_unit_concept_id',
            'numerator_value', 'numerator_unit_concept_id', 'denominator_value',
            'denominator_unit_concept_id', 'box_size', 'valid_start_date', 'valid_end_date',
            'invalid_reason',
        )

        def _fk(v):
            """Concept id if loaded, else None — keeps optional unit FKs valid."""
            try:
                iv = int(v)
            except (ValueError, TypeError):
                return None
            return iv if iv in loaded_ids else None

        def _f(v):
            v = (v or '').strip()
            return float(v) if v else None

        def _i(v):
            v = (v or '').strip()
            return int(v) if v else None

        with f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            gi = idx.get
            for row in reader:
                scanned += 1
                if scanned % PROGRESS_EVERY == 0:
                    self._log(f'  drug_strength: scanned {scanned:,}, {count:,} matched ({time.monotonic() - t:.0f}s)...')
                try:
                    drug = int(row[idx['drug_concept_id']])
                    ing = int(row[idx['ingredient_concept_id']])
                except (ValueError, IndexError):
                    continue
                # required FKs must be loaded
                if drug not in loaded_ids or ing not in loaded_ids:
                    continue
                count += 1
                if not dry_run:
                    inv = row[idx['invalid_reason']][:1] if row[idx['invalid_reason']] else None
                    rows.append((
                        drug, ing,
                        _f(row[gi('amount_value')]), _fk(row[gi('amount_unit_concept_id')]),
                        _f(row[gi('numerator_value')]), _fk(row[gi('numerator_unit_concept_id')]),
                        _f(row[gi('denominator_value')]), _fk(row[gi('denominator_unit_concept_id')]),
                        _i(row[gi('box_size')]),
                        _parse_date(row[idx['valid_start_date']]).isoformat(),
                        _parse_date(row[idx['valid_end_date']]).isoformat(),
                        inv,
                    ))
                    if len(rows) >= BATCH:
                        _copy_rows('drug_strength', cols_out, rows, self._log, direct=self._direct)
                        rows = []
        if not dry_run:
            _copy_rows('drug_strength', cols_out, rows, self._log, direct=self._direct)
        self._cleanup('DRUG_STRENGTH.csv')
        self._log(f'  drug_strength: {count:,} loaded from {scanned:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _sync_cdm_source_metadata(self):
        """Ensure the cdm_source row exists and fill its vocabulary metadata.

        get_or_create keeps the command resilient when the migration seed has
        not yet run.
        """
        row, created = CdmSource.objects.get_or_create(
            cdm_source_abbreviation='PRomop',
            defaults=_CDM_SOURCE_DEFAULTS,
        )
        if created:
            self._log('  cdm_source: re-seeded PRomop row (was missing — e.g. cleared by --replace)')
        vocab_version = (
            Vocabulary.objects.filter(vocabulary_id='None')
            .values_list('vocabulary_version', flat=True).first()
        )
        version_concept_id = 756265 if Concept.objects.filter(concept_id=756265).exists() else None
        fields = {}
        if vocab_version:
            fields['vocabulary_version'] = vocab_version[:20]
        if version_concept_id:
            fields['cdm_version_concept_id'] = version_concept_id
        if fields and CdmSource.objects.filter(pk=row.pk).update(**fields):
            self._log(f'  cdm_source: updated {fields}')

    def _record_version_history(self, replace):
        """Append an immutable version-history row per loaded vocabulary.

        This append-only table records which release was implemented when
        (promop#305, TI.4.2#01/#09). action='replaced' for --replace, else
        'loaded'. cdm_release_date is taken from the self-describing cdm_source
        row.
        """
        action = (
            VocabularyVersionHistory.ACTION_REPLACED if replace
            else VocabularyVersionHistory.ACTION_LOADED
        )
        cdm_release_date = (
            CdmSource.objects.filter(cdm_source_abbreviation='PRomop')
            .values_list('cdm_release_date', flat=True).first()
        )
        recorded = 0
        for vid, version in (
            Vocabulary.objects.order_by('vocabulary_id')
            .values_list('vocabulary_id', 'vocabulary_version')
        ):
            record_vocabulary_version_history(
                vocabulary_id=vid,
                version=version,
                action=action,
                cdm_release_date=cdm_release_date,
            )
            recorded += 1
        self._log(f'  vocabulary_version_history: recorded {recorded} {action} row(s)')

    def _load_source_to_concept_map(self, dry_run):
        self._log('Loading SOURCE_TO_CONCEPT_MAP.csv...')
        t = time.monotonic()
        try:
            f = self._open('SOURCE_TO_CONCEPT_MAP.csv')
        except CommandError:
            self._log('  SOURCE_TO_CONCEPT_MAP.csv not found, skipping.')
            return 0
        loaded_ids = set(Concept.objects.values_list('concept_id', flat=True))
        count = scanned = 0
        rows = []
        cols_out = (
            'source_code', 'source_concept_id', 'source_vocabulary_id',
            'source_code_description', 'target_concept_id', 'target_vocabulary_id',
            'valid_start_date', 'valid_end_date', 'invalid_reason',
        )
        with f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            i_src_code = idx['source_code']
            i_src_cid = idx['source_concept_id']
            i_src_vid = idx['source_vocabulary_id']
            i_src_desc = idx['source_code_description']
            i_tgt_cid = idx['target_concept_id']
            i_tgt_vid = idx['target_vocabulary_id']
            i_start = idx['valid_start_date']
            i_end = idx['valid_end_date']
            i_inv = idx['invalid_reason']
            for row in reader:
                scanned += 1
                if scanned % PROGRESS_EVERY == 0:
                    self._log(f'  stcm: scanned {scanned:,}, {count:,} matched ({time.monotonic() - t:.0f}s)...')
                try:
                    src_cid = int(row[i_src_cid])
                    tgt_cid = int(row[i_tgt_cid])
                except (ValueError, IndexError):
                    continue
                if src_cid not in loaded_ids or tgt_cid not in loaded_ids:
                    continue
                count += 1
                if not dry_run:
                    rows.append((
                        row[i_src_code][:50],
                        src_cid,
                        row[i_src_vid][:20],
                        (row[i_src_desc] or None) if i_src_desc < len(row) else None,
                        tgt_cid,
                        row[i_tgt_vid][:20],
                        _parse_date(row[i_start]),
                        _parse_date(row[i_end]),
                        row[i_inv][:1] if row[i_inv] else None,
                    ))
                    if len(rows) >= BATCH:
                        _copy_rows('source_to_concept_map', cols_out, rows, self._log, direct=self._direct)
                        rows = []
        if not dry_run:
            _copy_rows('source_to_concept_map', cols_out, rows, self._log, direct=self._direct)
        self._cleanup('SOURCE_TO_CONCEPT_MAP.csv')
        self._log(f'  stcm: {count:,} loaded from {scanned:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _publish_release(self, counts):
        """Create a published VocabularyRelease row capturing this load."""
        from datetime import datetime as dt, timezone as _tz
        from django.utils import timezone

        vocab_versions = dict(
            Vocabulary.objects.order_by('vocabulary_id')
            .values_list('vocabulary_id', 'vocabulary_version')
        )
        # row_counts must reflect the ACTUAL table content the snapshot streams
        # (SELECT COUNT(*)), not this run's load counts — the table can hold rows
        # from prior loads / seed data / metadata vocabularies, so a consumer that
        # cross-checks streamed rows against the manifest (EXACT's fail-closed
        # completeness gate) would otherwise always mismatch.
        checksums = {}
        real_counts = {}
        for table_name in counts:
            try:
                with connection.cursor() as cur:
                    cur.execute(
                        f'SELECT COUNT(*), MIN(ctid::text), MAX(ctid::text) '
                        f'FROM {table_name}'
                    )
                    row = cur.fetchone()
                    real_counts[table_name] = row[0]
                    checksums[table_name] = {
                        'count': row[0],
                        'min_ctid': row[1],
                        'max_ctid': row[2],
                    }
            except Exception as exc:
                # Fall back to this run's load count, but never silently: a bare
                # swallow here reintroduces the manifest↔stream mismatch (#343)
                # with no server-side signal for the consumer's fail-closed gate.
                logger.warning(
                    "vocabulary_release: COUNT(*)/ctid probe failed for %s (%s); "
                    "falling back to this run's load count — the manifest may "
                    "disagree with the streamed table for %s.",
                    table_name, exc, table_name, exc_info=True,
                )
                real_counts[table_name] = counts[table_name]
                checksums[table_name] = {'count': counts[table_name]}

        now = timezone.now()
        build_ts = dt.fromtimestamp(self._build_start, tz=_tz.utc)
        release = VocabularyRelease.objects.create(
            schema_version='5.4',
            scope=sorted(VOCAB_SCOPE),
            build_timestamp=build_ts,
            athena_version=getattr(self, '_cdm_vocab_version', None),
            vocab_versions=vocab_versions,
            row_counts=real_counts,
            checksums=checksums,
            status='published',
            published_at=now,
        )
        self._log(f'  vocabulary_release: published release pk={release.pk}')

    def _load_code_mappings(self, verbosity):
        """Load approved code-to-concept mappings from the bundled artifact."""
        from django.core.management import call_command
        artifact = Path(__file__).resolve().parents[2] / 'data' / 'code_concept_mappings.json'
        if not artifact.exists():
            self._log(
                '  load_mappings: artifact not found at '
                f'{artifact} — skipping code mapping load.'
            )
            return
        self._log('')
        self._log('  Loading approved code-to-concept mappings from artifact...')
        call_command('load_mappings', artifact=str(artifact), verbosity=verbosity)
