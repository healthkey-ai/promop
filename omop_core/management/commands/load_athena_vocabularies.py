"""Load OHDSI Athena vocabulary TSVs via stage → validate → publish.

Issue #236 PR 3 (ADR 0001): promop is the governed source of vocabulary data
and consumers mirror it release-by-release, so a load must never leave the
corpus tables in a torn state.  Three phases:

  1. STAGE    — parse the TSVs and COPY into UNLOGGED ``_stage_<table>``
                mirrors.  Live corpus tables are never touched in this phase.
  2. VALIDATE — row-count drift thresholds, natural-key uniqueness, FK
                integrity against the staged concept set, and namespace
                hygiene (no out-of-scope vocabularies, no locally-minted
                FHIR-*/hk* codes in inbound files, no concept_id collisions
                with live HK-* rows).  Any failure aborts before publish.
  3. PUBLISH  — a single transaction (``vocab_release.publish_release``):
                per-row ``ReleaseTableChange`` records (inserts / updates /
                tombstones) are written from stage-vs-live diffs, the staged
                rows are upserted, scope-guarded deletes remove rows Athena
                retired, and the ``VocabRelease`` manifest (checksums over the
                post-publish tables) is flipped to published — all atomically.

``--replace`` is deprecated: loads are now atomic upserts with scope-guarded
deletes, so a destructive clear-and-reload is no longer needed (or allowed —
it would cascade through clinical tables).  ``reset_vocab_tables`` is the
explicit escape hatch.

Scope guards on deletes: only rows whose vocabulary is inside VOCAB_SCOPE are
ever deleted.  Locally-minted HK-* concepts/vocabularies, concept 0, and any
relationship/ancestor/synonym/strength/map row attached to them always
survive a publish.
"""
import csv
import sys
import time
from datetime import date
from pathlib import Path

csv.field_size_limit(sys.maxsize)

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, models

from omop_core.models import (
    Vocabulary, Domain, ConceptClass, Concept,
    Relationship, ConceptRelationship, ConceptAncestor, CdmSource,
)

VOCAB_SCOPE = frozenset({
    'HemOnc', 'RxNorm', 'RxNorm Extension', 'ATC', 'LOINC', 'UCUM',
    'Visit', 'Type Concept',
    # Clinical-record vocabularies (FHIR sync B3): SNOMED covers conditions,
    # procedures, and allergies; ICD10CM covers EHR-sourced diagnoses. CVX
    # (immunizations) is mapped in the ingest but isn't in the current Athena
    # export, so it loads once a CVX-inclusive bundle is fetched.
    'SNOMED', 'ICD10CM', 'CVX',
})
RXNORM_CLASS_SCOPE = frozenset({'Ingredient', 'Clinical Drug', 'Branded Drug', 'Clinical Drug Comp'})
LOINC_DOMAIN_SCOPE = frozenset({'Measurement', 'Observation'})
BATCH = 100_000
PROGRESS_EVERY = 500_000

_VOCAB_SCOPE_SQL = ', '.join(repr(v) for v in sorted(VOCAB_SCOPE))

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

# Loader-managed columns per corpus table, in COPY order.  ``source`` columns
# (concept.source, concept_relationship.source) are HealthKey-provenance
# fields written by FHIR import / local curation — never loader-managed, so
# they are excluded from stage mirrors, upserts, and diff comparisons.
# ``key`` is the CDM natural key used for diff/upsert/delete matching.
# ``delete_where`` (SQL fragment on table alias ``t``) bounds which live
# rows a publish may remove; None = insert/update only (small metadata tables
# that Athena append-only maintains and local features may reference).
# Guards that reference the concept table carry a ``{concept}`` placeholder so
# the same predicate can be evaluated against the live table (deletes,
# tombstones, live drift counts) or the stage mirror (staged drift counts) —
# keeping the drift gate an apples-to-apples comparison of the managed slice.
TABLE_SPECS = {
    'vocabulary': {
        'key': ('vocabulary_id',),
        'cols': ('vocabulary_id', 'vocabulary_name', 'vocabulary_reference',
                 'vocabulary_version', 'vocabulary_concept_id'),
        'delete_where': f't.vocabulary_id IN ({_VOCAB_SCOPE_SQL})',
    },
    'domain': {
        'key': ('domain_id',),
        'cols': ('domain_id', 'domain_name', 'domain_concept_id'),
        'delete_where': None,
    },
    'concept_class': {
        'key': ('concept_class_id',),
        'cols': ('concept_class_id', 'concept_class_name', 'concept_class_concept_id'),
        'delete_where': None,
    },
    'relationship': {
        'key': ('relationship_id',),
        'cols': ('relationship_id', 'relationship_name', 'is_hierarchical',
                 'defines_ancestry', 'reverse_relationship_id', 'relationship_concept_id'),
        'delete_where': None,
    },
    'concept': {
        'key': ('concept_id',),
        'cols': ('concept_id', 'concept_name', 'domain_id', 'vocabulary_id',
                 'concept_class_id', 'standard_concept', 'concept_code',
                 'valid_start_date', 'valid_end_date', 'invalid_reason'),
        'delete_where': f't.vocabulary_id IN ({_VOCAB_SCOPE_SQL})',
    },
    'concept_synonym': {
        'key': ('concept_id', 'concept_synonym_name', 'language_concept_id'),
        'cols': ('concept_id', 'concept_synonym_name', 'language_concept_id'),
        'delete_where': (
            't.concept_id IN (SELECT concept_id FROM {concept} '
            f'WHERE vocabulary_id IN ({_VOCAB_SCOPE_SQL}))'
        ),
    },
    'concept_relationship': {
        'key': ('concept_id_1', 'concept_id_2', 'relationship_id', 'valid_start_date'),
        'cols': ('concept_id_1', 'concept_id_2', 'relationship_id',
                 'valid_start_date', 'valid_end_date', 'invalid_reason'),
        'delete_where': (
            't.concept_id_1 IN (SELECT concept_id FROM {concept} '
            f'WHERE vocabulary_id IN ({_VOCAB_SCOPE_SQL})) AND '
            't.concept_id_2 IN (SELECT concept_id FROM {concept} '
            f'WHERE vocabulary_id IN ({_VOCAB_SCOPE_SQL}))'
        ),
    },
    'concept_ancestor': {
        'key': ('ancestor_concept_id', 'descendant_concept_id'),
        'cols': ('ancestor_concept_id', 'descendant_concept_id',
                 'min_levels_of_separation', 'max_levels_of_separation'),
        'delete_where': (
            "t.ancestor_concept_id IN (SELECT concept_id FROM {concept} "
            "WHERE vocabulary_id = 'HemOnc') AND "
            "t.descendant_concept_id IN (SELECT concept_id FROM {concept} "
            "WHERE vocabulary_id = 'HemOnc')"
        ),
    },
    'drug_strength': {
        'key': ('drug_concept_id', 'ingredient_concept_id'),
        'cols': ('drug_concept_id', 'ingredient_concept_id', 'amount_value',
                 'amount_unit_concept_id', 'numerator_value', 'numerator_unit_concept_id',
                 'denominator_value', 'denominator_unit_concept_id', 'box_size',
                 'valid_start_date', 'valid_end_date', 'invalid_reason'),
        'delete_where': (
            't.drug_concept_id IN (SELECT concept_id FROM {concept} '
            f'WHERE vocabulary_id IN ({_VOCAB_SCOPE_SQL}))'
        ),
    },
    'source_to_concept_map': {
        'key': ('source_code', 'source_vocabulary_id', 'target_concept_id',
                'valid_start_date'),
        'cols': ('source_code', 'source_concept_id', 'source_vocabulary_id',
                 'source_code_description', 'target_concept_id', 'target_vocabulary_id',
                 'valid_start_date', 'valid_end_date', 'invalid_reason'),
        'delete_where': f't.source_vocabulary_id IN ({_VOCAB_SCOPE_SQL})',
    },
}

# Dependency order for inserts/updates (parents before children); publishes
# apply deletes in the reverse order.  ReleaseTableChange seq follows the same
# order so consumers replaying a delta walk a valid path.
APPLY_ORDER = (
    'vocabulary', 'domain', 'concept_class', 'relationship', 'concept',
    'concept_synonym', 'concept_relationship', 'concept_ancestor',
    'drug_strength', 'source_to_concept_map',
)


def _guard(spec, concept_table='concept'):
    """Bind the delete guard's concept-table reference (live or stage mirror)."""
    guard = spec['delete_where']
    return guard.format(concept=concept_table) if guard else None

# Concept FK columns that staged rows must satisfy against _stage_concept.
# (table, column, allow_zero) — STCM source_concept_id may be 0 (no source
# concept); concept 0 is seeded outside the loader and is never staged.
_STAGE_CONCEPT_FKS = (
    ('concept_relationship', 'concept_id_1', False),
    ('concept_relationship', 'concept_id_2', False),
    ('concept_ancestor', 'ancestor_concept_id', False),
    ('concept_ancestor', 'descendant_concept_id', False),
    ('concept_synonym', 'concept_id', False),
    ('concept_synonym', 'language_concept_id', False),
    ('drug_strength', 'drug_concept_id', False),
    ('drug_strength', 'ingredient_concept_id', False),
    ('source_to_concept_map', 'target_concept_id', False),
    ('source_to_concept_map', 'source_concept_id', True),
)


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


def _header_index(header_row):
    """Build column-name → index map from a TSV header row."""
    return {col: i for i, col in enumerate(header_row)}


def _concept_in_scope(vid, concept_code, concept_class_id, domain_id):
    if vid not in VOCAB_SCOPE:
        return False
    if vid == 'ATC':
        return concept_code.startswith('L')
    if vid in ('RxNorm', 'RxNorm Extension'):
        return concept_class_id in RXNORM_CLASS_SCOPE
    if vid == 'LOINC':
        return domain_id in LOINC_DOMAIN_SCOPE
    return True


def _copy_rows(table, columns, rows, log):
    """COPY rows directly into a _stage_* mirror (never a live table)."""
    if not rows:
        return
    connection.ensure_connection()
    cols = ', '.join(columns)
    with connection.connection.cursor() as cur:
        with cur.copy(f'COPY {table} ({cols}) FROM STDIN') as copy:
            for row in rows:
                copy.write_row(row)


def seed_concept_zero(log):
    """Ensure OMOP concept 0 ('No matching concept') exists.

    Shared with reset_vocab_tables.  Concept 0 lives outside VOCAB_SCOPE
    (vocabulary 'None') so publishes never delete it.
    """
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
        log('Seeded concept_id=0 (No matching concept)')


def sync_cdm_source_metadata(log):
    """Ensure the cdm_source row exists and fill its vocabulary metadata.

    Shared with reset_vocab_tables (a TRUNCATE ... CASCADE wipes cdm_source
    via its cdm_version_concept FK, and migration-0112's seed does not
    re-run).
    """
    row, created = CdmSource.objects.get_or_create(
        cdm_source_abbreviation='PRomop',
        defaults=_CDM_SOURCE_DEFAULTS,
    )
    if created:
        log('  cdm_source: re-seeded PRomop row (was missing)')
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
        log(f'  cdm_source: updated {fields}')


class Command(BaseCommand):
    help = 'Load OHDSI Athena vocabulary TSVs: stage → validate → publish (atomic)'

    def add_arguments(self, parser):
        parser.add_argument('--path',
                            help='Directory containing Athena TSV files')
        parser.add_argument('--bucket',
                            help='GCS bucket name to stream files from (alternative to --path)')
        parser.add_argument('--replace', action='store_true',
                            help='DEPRECATED, ignored: loads are now atomic upserts. '
                                 'Use reset_vocab_tables for a full wipe.')
        parser.add_argument('--dry-run', action='store_true', dest='dry_run',
                            help='Count rows without writing to DB')
        parser.add_argument('--stage-only', action='store_true', dest='stage_only',
                            help='Stop after staging+validation (leaves _stage_* '
                                 'tables in place for inspection; next run drops them)')
        parser.add_argument('--max-drift-pct', type=float, default=25.0,
                            help='Abort if any staged table row count differs from live '
                                 'by more than this percentage (default 25; ignored when '
                                 'the live table is empty)')

    def handle(self, *args, **options):
        base = options['path']
        bucket_name = options['bucket']
        dry_run = options['dry_run']

        if not base and not bucket_name:
            raise CommandError('Provide either --path or --bucket')

        if options['replace']:
            self._log('WARNING: --replace is deprecated and ignored. Loads are now '
                      'atomic stage → validate → publish upserts with scope-guarded '
                      'deletes. Use reset_vocab_tables --confirm for a full wipe.')

        self._gcs_bucket = None
        if bucket_name:
            from google.cloud import storage as gcs
            self._gcs_bucket = gcs.Client().bucket(bucket_name)
            self._log(f'Loading from gs://{bucket_name}/ (download-one-process-delete)')

        t0 = time.monotonic()
        self._base = base

        if dry_run:
            counts = self._stage_all(dry_run=True)
            self._log_summary(counts, elapsed=time.monotonic() - t0, verb='would load')
            return

        # Phase 1 — STAGE
        self._create_stage_tables()
        try:
            counts = self._stage_all(dry_run=False)

            # Phase 2 — VALIDATE
            self._validate_stage(max_drift_pct=options['max_drift_pct'])
            self._log('  Validation passed (uniqueness, FK integrity, namespace, drift).')

            if options['stage_only']:
                self._log('--stage-only: stopping before publish; _stage_* tables left '
                          'in place.')
                self._log_summary(counts, elapsed=time.monotonic() - t0, verb='staged')
                return

            # Phase 3 — PUBLISH (single transaction inside publish_release)
            from omop_core.services.vocab_release import publish_release
            release = publish_release(
                notes='Athena load via load_athena_vocabularies',
                before_publish=self._apply_stage,
            )
            change_counts = self._change_counts(release)
            if change_counts:
                ops = ', '.join(f'{op}={n:,}' for op, n in sorted(change_counts.items()))
                self._log(f'  Published {release.release_id}: {ops}')
            else:
                self._log(f'  Published {release.release_id}: no row changes')
        finally:
            if not options['stage_only']:
                self._drop_stage_tables()

        seed_concept_zero(self._log)
        sync_cdm_source_metadata(self._log)
        self._log_summary(counts, elapsed=time.monotonic() - t0, verb='loaded')

    # -- phases -------------------------------------------------------------

    def _create_stage_tables(self):
        self._log('Creating UNLOGGED _stage_* mirrors...')
        with connection.cursor() as cur:
            for table, spec in TABLE_SPECS.items():
                cols = ', '.join(spec['cols'])
                cur.execute(f'DROP TABLE IF EXISTS _stage_{table}')
                cur.execute(
                    f'CREATE UNLOGGED TABLE _stage_{table} AS '
                    f'SELECT {cols} FROM {table} WHERE false'
                )

    def _drop_stage_tables(self):
        with connection.cursor() as cur:
            for table in TABLE_SPECS:
                cur.execute(f'DROP TABLE IF EXISTS _stage_{table}')

    def _validate_stage(self, max_drift_pct):
        """Abort the load before publish if staged data fails any gate."""
        problems = []
        with connection.cursor() as cur:
            # 1. Natural-key uniqueness inside each staged file
            for table, spec in TABLE_SPECS.items():
                key = ', '.join(spec['key'])
                cur.execute(
                    f'SELECT {key} FROM _stage_{table} GROUP BY {key} '
                    f'HAVING COUNT(*) > 1 LIMIT 1'
                )
                if cur.fetchone():
                    problems.append(f'{table}: duplicate natural key ({key}) in staged rows')

            # 2. FK integrity against the staged concept set
            for table, col, allow_zero in _STAGE_CONCEPT_FKS:
                zero_ok = 'AND s.{col} <> 0'.format(col=col) if allow_zero else ''
                cur.execute(
                    f'SELECT s.{col} FROM _stage_{table} s '
                    f'WHERE s.{col} IS NOT NULL {zero_ok} AND NOT EXISTS '
                    f'(SELECT 1 FROM _stage_concept c WHERE c.concept_id = s.{col}) '
                    f'LIMIT 1'
                )
                if cur.fetchone():
                    problems.append(f'{table}.{col}: references a concept_id not present '
                                    f'in the staged concept set')

            # 3. Namespace hygiene
            cur.execute(
                f'SELECT concept_id FROM _stage_concept '
                f'WHERE vocabulary_id NOT IN ({_VOCAB_SCOPE_SQL}) LIMIT 1'
            )
            if cur.fetchone():
                problems.append('concept: staged rows outside VOCAB_SCOPE '
                                '(inbound file claims a non-scope vocabulary)')
            cur.execute(
                "SELECT concept_id FROM _stage_concept "
                "WHERE concept_code LIKE 'FHIR-%' "
                "OR concept_code ~* '^hk[a-z-]*:' LIMIT 1"
            )
            if cur.fetchone():
                problems.append('concept: staged rows carry locally-minted codes '
                                '(FHIR-* / hk*:) — local concepts must never arrive '
                                'via Athena files')
            cur.execute(
                f'SELECT vocabulary_id FROM _stage_vocabulary '
                f"WHERE vocabulary_id LIKE 'HK-%' "
                f"OR (vocabulary_id NOT IN ({_VOCAB_SCOPE_SQL}) "
                f"AND vocabulary_id <> 'None') LIMIT 1"
            )
            if cur.fetchone():
                problems.append('vocabulary: staged rows outside VOCAB_SCOPE or '
                                'claiming the local HK-* namespace')
            cur.execute(
                f'SELECT s.concept_id FROM _stage_concept s '
                f'JOIN concept t ON t.concept_id = s.concept_id '
                f"WHERE t.vocabulary_id NOT IN ({_VOCAB_SCOPE_SQL}) "
                f"AND t.vocabulary_id <> 'None' LIMIT 1"
            )
            if cur.fetchone():
                problems.append('concept: staged concept_id collides with a live '
                                'locally-minted (HK-*) concept — aborting to avoid '
                                'overwriting local content')

            # 4. Row-count drift (skipped for empty live slices = bootstrap).
            # Only guarded tables: the loader fully owns their in-scope slice,
            # so stage-vs-live is an apples-to-apples comparison (concept 0,
            # HK-* rows, and child rows of local concepts are excluded — the
            # SAME guard is applied to the stage mirror, e.g. the staged
            # 'None' vocabulary row is out of scope and must not be counted).
            # Unguarded metadata tables legitimately hold non-Athena rows
            # (migration seeds, local additions), so drift cannot be judged.
            for table, spec in TABLE_SPECS.items():
                if spec['delete_where'] is None:
                    continue
                cur.execute(
                    f'SELECT COUNT(*) FROM {table} t WHERE {_guard(spec)}')
                live_n = cur.fetchone()[0]
                cur.execute(
                    f'SELECT COUNT(*) FROM _stage_{table} t '
                    f'WHERE {_guard(spec, "_stage_concept")}')
                stage_n = cur.fetchone()[0]
                if live_n and abs(stage_n - live_n) / live_n * 100 > max_drift_pct:
                    problems.append(f'{table}: staged row count {stage_n:,} drifts '
                                    f'{abs(stage_n - live_n) / live_n:.0%} from live '
                                    f'{live_n:,} (threshold {max_drift_pct:.0f}% — '
                                    f'override with --max-drift-pct)')

        if problems:
            raise CommandError(
                'Staged vocabulary load failed validation; live tables untouched:\n'
                + '\n'.join(f'  - {p}' for p in problems)
            )

    def _apply_stage(self, release):
        """Publish callback: write ReleaseTableChange rows and apply mutations.

        Runs inside publish_release's transaction, so change rows, corpus
        mutations, and the manifest flip commit or roll back together.  Change
        rows are computed per op immediately before that op's mutation against
        the same join fragments, so they record exactly what is applied; seq
        order (inserts/updates in APPLY_ORDER, then tombstones in reverse) is
        the order consumers replay.
        """
        seq = 0
        stats = {}
        with connection.cursor() as cur:
            for table in APPLY_ORDER:
                spec = TABLE_SPECS[table]
                for op in ('insert', 'update'):
                    # concept_synonym's natural key IS the whole row — a key
                    # match is identical by definition, so there is no update
                    # op for all-key tables (and no SET clause to generate).
                    if op == 'update' and all(c in spec['key'] for c in spec['cols']):
                        continue
                    seq = self._write_change_rows(cur, release, table, spec, op, seq)
                    stats[(table, op)] = self._apply_mutation(cur, table, spec, op)
            for table in reversed(APPLY_ORDER):
                spec = TABLE_SPECS[table]
                if spec['delete_where'] is None:
                    continue
                seq = self._write_change_rows(cur, release, table, spec, 'tombstone', seq)
                stats[(table, 'tombstone')] = self._apply_mutation(
                    cur, table, spec, 'tombstone')
        applied = {f'{t}/{op}': n for (t, op), n in stats.items() if n}
        if applied:
            self._log('  Applied: ' + ', '.join(f'{k}={v:,}' for k, v in applied.items()))
        else:
            self._log('  No corpus changes (stage matches live).')

    # -- publish SQL generation ----------------------------------------------

    @staticmethod
    def _fragments(table, spec):
        key, cols = spec['key'], spec['cols']
        nonkey = tuple(c for c in cols if c not in key)
        join = ' AND '.join(f't.{k} = s.{k}' for k in key)
        row_key_s = "concat_ws('|', " + ', '.join(f's.{k}::text' for k in key) + ')'
        row_key_t = "concat_ws('|', " + ', '.join(f't.{k}::text' for k in key) + ')'
        jsonb_s = 'jsonb_build_object(' + ', '.join(
            f"'{c}', s.{c}" for c in cols) + ')'
        jsonb_new_nonkey = 'jsonb_build_object(' + ', '.join(
            f"'{c}', s.{c}" for c in nonkey) + ')'
        jsonb_old_nonkey = 'jsonb_build_object(' + ', '.join(
            f"'{c}', t.{c}" for c in nonkey) + ')'
        distinct = (
            f"ROW({', '.join('t.' + c for c in nonkey)}) IS DISTINCT FROM "
            f"ROW({', '.join('s.' + c for c in nonkey)})"
        )
        order_s = ', '.join(f's.{k}' for k in key)
        order_t = ', '.join(f't.{k}' for k in key)
        return {
            'key': key, 'cols': cols, 'nonkey': nonkey, 'join': join,
            'row_key_s': row_key_s, 'row_key_t': row_key_t,
            'jsonb_s': jsonb_s, 'distinct': distinct,
            'jsonb_update': f"jsonb_build_object('old', {jsonb_old_nonkey}, "
                            f"'new', {jsonb_new_nonkey})",
            'order_s': order_s, 'order_t': order_t,
            'col_list': ', '.join(cols),
            'set_clause': ', '.join(f'{c} = s.{c}' for c in nonkey),
        }

    def _write_change_rows(self, cur, release, table, spec, op, seq):
        f = self._fragments(table, spec)
        if op == 'insert':
            src = (f"SELECT %s, row_number() OVER (ORDER BY {f['order_s']}) + %s, "
                   f"'{table}', 'insert', {f['row_key_s']}, {f['jsonb_s']} "
                   f"FROM _stage_{table} s "
                   f"WHERE NOT EXISTS (SELECT 1 FROM {table} t WHERE {f['join']})")
        elif op == 'update':
            src = (f"SELECT %s, row_number() OVER (ORDER BY {f['order_s']}) + %s, "
                   f"'{table}', 'update', {f['row_key_s']}, {f['jsonb_update']} "
                   f"FROM _stage_{table} s JOIN {table} t ON {f['join']} "
                   f"WHERE {f['distinct']}")
        else:  # tombstone
            src = (f"SELECT %s, row_number() OVER (ORDER BY {f['order_t']}) + %s, "
                   f"'{table}', 'tombstone', {f['row_key_t']}, to_jsonb(t) "
                   f"FROM {table} t WHERE {_guard(spec)} AND NOT EXISTS "
                   f"(SELECT 1 FROM _stage_{table} s WHERE {f['join']})")
        cur.execute(
            'INSERT INTO release_table_change '
            '(release_id, seq, table_name, operation, row_key, payload) ' + src,
            [release.release_id, seq],
        )
        return seq + cur.rowcount

    def _apply_mutation(self, cur, table, spec, op):
        f = self._fragments(table, spec)
        if op == 'insert':
            cur.execute(
                f"INSERT INTO {table} ({f['col_list']}) "
                f"SELECT {f['col_list']} FROM _stage_{table} s "
                f"WHERE NOT EXISTS (SELECT 1 FROM {table} t WHERE {f['join']})"
            )
        elif op == 'update':
            cur.execute(
                f"UPDATE {table} t SET {f['set_clause']} "
                f"FROM _stage_{table} s WHERE {f['join']} AND {f['distinct']}"
            )
        else:  # tombstone
            cur.execute(
                f"DELETE FROM {table} t WHERE {_guard(spec)} AND NOT EXISTS "
                f"(SELECT 1 FROM _stage_{table} s WHERE {f['join']})"
            )
        return cur.rowcount

    # -- misc -----------------------------------------------------------------

    def _change_counts(self, release):
        return dict(
            release.changes.values_list('operation')
            .annotate(n=models.Count('operation'))
            .values_list('operation', 'n')
        )

    def _log(self, msg):
        self.stdout.write(msg)
        self.stdout.flush()

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

    def _stage_ids(self, where=''):
        """concept_id set from the staged concept mirror (optionally filtered)."""
        with connection.cursor() as cur:
            cur.execute(f'SELECT concept_id FROM _stage_concept {where}')
            return {r[0] for r in cur.fetchall()}

    def _log_summary(self, counts, elapsed, verb):
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
            self._log(f'  Scanned {self._concept_scanned:,} concept rows '
                      f'({total and counts["concept"] * 100 // self._concept_scanned or 0}% in scope)')
        self._log(f'  Elapsed: {elapsed:.0f}s')
        self._log('=' * 60)

    def _stage_all(self, dry_run):
        return {
            'relationship':         self._load_relationships(dry_run),
            'vocabulary':           self._load_vocabularies(dry_run),
            'domain':               self._load_domains(dry_run),
            'concept_class':        self._load_concept_classes(dry_run),
            'concept':              self._load_concepts(dry_run),
            'concept_relationship': self._load_concept_relationships(dry_run),
            'concept_ancestor':     self._load_concept_ancestors(dry_run),
            'concept_synonym':      self._load_concept_synonym(dry_run),
            'drug_strength':        self._load_drug_strength(dry_run),
            'source_to_concept_map': self._load_source_to_concept_map(dry_run),
        }

    def _copy_batch(self, dry_run, table, spec, rows):
        if not dry_run:
            _copy_rows(f'_stage_{table}', spec['cols'], rows, self._log)

    # -- per-file loaders (parse → _stage_* mirrors) ---------------------------

    def _load_relationships(self, dry_run):
        self._log('Loading RELATIONSHIP.csv...')
        t = time.monotonic()
        spec = TABLE_SPECS['relationship']
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
                        self._copy_batch(dry_run, 'relationship', spec, rows)
                        rows = []
        self._copy_batch(dry_run, 'relationship', spec, rows)
        self._cleanup('RELATIONSHIP.csv')
        self._log(f'  RELATIONSHIP.csv: {count:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_vocabularies(self, dry_run):
        self._log('Loading VOCABULARY.csv...')
        t = time.monotonic()
        spec = TABLE_SPECS['vocabulary']
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
        self._copy_batch(dry_run, 'vocabulary', spec, rows)
        self._cleanup('VOCABULARY.csv')
        self._log(f'  VOCABULARY.csv: {count:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_domains(self, dry_run):
        self._log('Loading DOMAIN.csv...')
        t = time.monotonic()
        spec = TABLE_SPECS['domain']
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
        self._copy_batch(dry_run, 'domain', spec, rows)
        self._cleanup('DOMAIN.csv')
        self._log(f'  DOMAIN.csv: {count:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_concept_classes(self, dry_run):
        self._log('Loading CONCEPT_CLASS.csv...')
        t = time.monotonic()
        spec = TABLE_SPECS['concept_class']
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
        self._copy_batch(dry_run, 'concept_class', spec, rows)
        self._cleanup('CONCEPT_CLASS.csv')
        self._log(f'  CONCEPT_CLASS.csv: {count:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_concepts(self, dry_run):
        self._log('Loading CONCEPT.csv...')
        t = time.monotonic()
        spec = TABLE_SPECS['concept']
        count = 0
        scanned = 0
        vocab_counts = {}
        rows = []
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
                        self._copy_batch(dry_run, 'concept', spec, rows)
                        rows = []
        self._copy_batch(dry_run, 'concept', spec, rows)
        self._cleanup('CONCEPT.csv')
        self._log(f'  concepts: {count:,} loaded from {scanned:,} rows in {time.monotonic() - t:.0f}s')
        self._vocab_counts = vocab_counts
        self._concept_scanned = scanned
        return count

    def _load_concept_relationships(self, dry_run):
        self._log('Loading CONCEPT_RELATIONSHIP.csv...')
        t = time.monotonic()
        spec = TABLE_SPECS['concept_relationship']
        if dry_run:
            loaded_ids = None  # counted without a filter set (no stage tables)
        else:
            loaded_ids = self._stage_ids()
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
                if loaded_ids is not None and (c1 not in loaded_ids or c2 not in loaded_ids):
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
                        self._copy_batch(dry_run, 'concept_relationship', spec, rows)
                        rows = []
        self._copy_batch(dry_run, 'concept_relationship', spec, rows)
        self._cleanup('CONCEPT_RELATIONSHIP.csv')
        self._log(f'  relationships: {count:,} loaded from {scanned:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_concept_ancestors(self, dry_run):
        self._log('Loading CONCEPT_ANCESTOR.csv...')
        t = time.monotonic()
        spec = TABLE_SPECS['concept_ancestor']
        if dry_run:
            hemonc_ids = None
        else:
            hemonc_ids = self._stage_ids("WHERE vocabulary_id = 'HemOnc'")
            self._log(f'  {len(hemonc_ids):,} HemOnc IDs in filter set')
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
                if hemonc_ids is not None and (anc not in hemonc_ids or desc not in hemonc_ids):
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
                        self._copy_batch(dry_run, 'concept_ancestor', spec, rows)
                        rows = []
        self._copy_batch(dry_run, 'concept_ancestor', spec, rows)
        self._cleanup('CONCEPT_ANCESTOR.csv')
        self._log(f'  ancestors: {count:,} loaded from {scanned:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_concept_synonym(self, dry_run):
        self._log('Loading CONCEPT_SYNONYM.csv...')
        t = time.monotonic()
        spec = TABLE_SPECS['concept_synonym']
        try:
            f = self._open('CONCEPT_SYNONYM.csv')
        except CommandError:
            self._log('  CONCEPT_SYNONYM.csv not found, skipping.')
            return 0
        # both concept and language must be staged to satisfy FK constraints
        loaded_ids = None if dry_run else self._stage_ids()
        count = scanned = 0
        rows = []
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
                if loaded_ids is not None and (cid not in loaded_ids or lang not in loaded_ids):
                    continue
                count += 1
                if not dry_run:
                    rows.append((cid, row[i_name][:1000], lang))
                    if len(rows) >= BATCH:
                        self._copy_batch(dry_run, 'concept_synonym', spec, rows)
                        rows = []
        self._copy_batch(dry_run, 'concept_synonym', spec, rows)
        self._cleanup('CONCEPT_SYNONYM.csv')
        self._log(f'  synonyms: {count:,} loaded from {scanned:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_drug_strength(self, dry_run):
        self._log('Loading DRUG_STRENGTH.csv...')
        t = time.monotonic()
        spec = TABLE_SPECS['drug_strength']
        try:
            f = self._open('DRUG_STRENGTH.csv')
        except CommandError:
            self._log('  DRUG_STRENGTH.csv not found, skipping.')
            return 0
        loaded_ids = None if dry_run else self._stage_ids()

        def _fk(v):
            """Concept id if staged, else None — keeps optional unit FKs valid."""
            try:
                iv = int(v)
            except (ValueError, TypeError):
                return None
            return iv if loaded_ids is None or iv in loaded_ids else None

        def _f(v):
            v = (v or '').strip()
            return float(v) if v else None

        def _i(v):
            v = (v or '').strip()
            return int(v) if v else None

        count = scanned = 0
        rows = []
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
                # required FKs must be staged
                if loaded_ids is not None and (drug not in loaded_ids or ing not in loaded_ids):
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
                        self._copy_batch(dry_run, 'drug_strength', spec, rows)
                        rows = []
        self._copy_batch(dry_run, 'drug_strength', spec, rows)
        self._cleanup('DRUG_STRENGTH.csv')
        self._log(f'  drug_strength: {count:,} loaded from {scanned:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_source_to_concept_map(self, dry_run):
        self._log('Loading SOURCE_TO_CONCEPT_MAP.csv...')
        t = time.monotonic()
        spec = TABLE_SPECS['source_to_concept_map']
        try:
            f = self._open('SOURCE_TO_CONCEPT_MAP.csv')
        except CommandError:
            self._log('  SOURCE_TO_CONCEPT_MAP.csv not found, skipping.')
            return 0
        # target (always) and source (unless 0 = no source concept) must be staged
        loaded_ids = None if dry_run else self._stage_ids()
        count = scanned = 0
        rows = []
        with f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            i_code = idx['source_code']
            i_scid = idx['source_concept_id']
            i_svid = idx['source_vocabulary_id']
            i_desc = idx['source_code_description']
            i_tcid = idx['target_concept_id']
            i_tvid = idx['target_vocabulary_id']
            i_start = idx['valid_start_date']
            i_end = idx['valid_end_date']
            i_invalid = idx['invalid_reason']
            for row in reader:
                scanned += 1
                if scanned % PROGRESS_EVERY == 0:
                    self._log(f'  source_to_concept_map: scanned {scanned:,}, {count:,} matched ({time.monotonic() - t:.0f}s)...')
                # corpus boundary: only source vocabularies inside VOCAB_SCOPE
                if row[i_svid] not in VOCAB_SCOPE:
                    continue
                try:
                    scid = int(row[i_scid] or 0)
                    tcid = int(row[i_tcid])
                except (ValueError, IndexError):
                    continue
                if loaded_ids is not None and (
                        tcid not in loaded_ids or (scid != 0 and scid not in loaded_ids)):
                    continue
                count += 1
                if not dry_run:
                    inv = row[i_invalid][:1] if row[i_invalid] else None
                    rows.append((
                        row[i_code][:50],
                        scid,
                        row[i_svid][:20],
                        (row[i_desc] or '')[:255] or None,
                        tcid,
                        row[i_tvid][:20],
                        _parse_date(row[i_start]).isoformat(),
                        _parse_date(row[i_end]).isoformat(),
                        inv,
                    ))
                    if len(rows) >= BATCH:
                        self._copy_batch(dry_run, 'source_to_concept_map', spec, rows)
                        rows = []
        self._copy_batch(dry_run, 'source_to_concept_map', spec, rows)
        self._cleanup('SOURCE_TO_CONCEPT_MAP.csv')
        self._log(f'  source_to_concept_map: {count:,} loaded from {scanned:,} rows in {time.monotonic() - t:.0f}s')
        return count
