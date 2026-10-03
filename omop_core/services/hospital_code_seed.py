"""Download and idempotently install a governed hospital-code seed artifact."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tempfile
from zipfile import BadZipFile, ZipFile

from django.core.management.base import CommandError
from django.db import connection
from django.utils import timezone

from omop_core.models import HospitalCodeImport
from omop_core.services import source_vocabularies
from omop_core.services.hospital_code_backfill import BuildResult, InventoryRow, upsert_inventory


SCHEMA_VERSION = 1
MEMBER_NAME = 'hospital_source_codes.jsonl'
IMPORT_LOCK_ID = 1_701_202_610


class HospitalCodeDownloadError(CommandError):
    """A transient Drive listing/download failure."""


def _sha256_stream(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(chunk)
    return digest.hexdigest()


def _sha256_file(path):
    with Path(path).open('rb') as stream:
        return _sha256_stream(stream)


def download_seed(source_url, artifact_filename, destination):
    """Download an exact named file from a public Drive folder or file URL."""
    try:
        import gdown
    except ImportError as exc:
        raise HospitalCodeDownloadError('Google Drive import requires gdown.') from exc

    file_id = None
    if '/folders/' in source_url:
        listing = gdown.download_folder(
            url=source_url, output=None, quiet=True, use_cookies=False,
            skip_download=True,
        )
        if listing is None:
            raise HospitalCodeDownloadError(f'Google Drive folder listing failed: {source_url}')
        matches = [item for item in listing if Path(item.path).name == artifact_filename]
        if len(matches) != 1:
            raise CommandError(
                f'Expected exactly one {artifact_filename!r} in the Drive folder; '
                f'found {len(matches)}.'
            )
        file_id = matches[0].id
    else:
        match = re.search(r'(?:/d/|[?&]id=)([-\w]+)', source_url)
        file_id = match.group(1) if match else None

    destination = Path(destination)
    result = gdown.download(
        id=file_id, url=None if file_id else source_url,
        output=str(destination), quiet=True, use_cookies=False,
    )
    if not result or not destination.is_file():
        raise HospitalCodeDownloadError(f'Google Drive download failed: {source_url}')
    return destination


def _manifest(archive):
    try:
        with ZipFile(archive) as source:
            names = [item.filename for item in source.infolist() if not item.is_dir()]
            for name in names:
                path = Path(name)
                if path.is_absolute() or '..' in path.parts:
                    raise CommandError(f'Unsafe path in hospital-code seed: {name}')
            if 'manifest.json' not in names:
                raise CommandError('Hospital-code seed is missing manifest.json.')
            manifest = json.loads(source.read('manifest.json'))
    except (BadZipFile, json.JSONDecodeError) as exc:
        raise CommandError(f'Invalid hospital-code seed archive: {exc}') from exc
    if manifest.get('schema_version') != SCHEMA_VERSION:
        raise CommandError(
            f"Unsupported hospital-code seed schema {manifest.get('schema_version')!r}; "
            f'expected {SCHEMA_VERSION}.'
        )
    if manifest.get('member') != MEMBER_NAME or MEMBER_NAME not in names:
        raise CommandError(f'Hospital-code seed must contain {MEMBER_NAME}.')
    return manifest


def _validated_payload(raw, line_number):
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CommandError(f'Invalid JSON on seed line {line_number}: {exc}') from exc
    required = {
        'source_vocabulary_id', 'source_code', 'source_code_description',
        'occurrence_count', 'source_metadata', 'source_unit_evidence',
        'domain_id', 'omop_table',
    }
    missing = required - payload.keys()
    if missing:
        raise CommandError(
            f'Seed line {line_number} is missing: {", ".join(sorted(missing))}.'
        )
    system = str(payload['source_vocabulary_id'] or '').strip().rstrip('/')
    code = str(payload['source_code'] or '').strip()
    if not source_vocabularies.hospital_vendor(system):
        raise CommandError(f'Seed line {line_number} is not Epic/Cerner: {system!r}.')
    if not code or len(code) > 100 or len(system) > 255:
        raise CommandError(f'Seed line {line_number} has an invalid source key.')
    if not isinstance(payload['source_metadata'], dict):
        raise CommandError(f'Seed line {line_number} source_metadata must be an object.')
    if not isinstance(payload['source_unit_evidence'], list):
        raise CommandError(f'Seed line {line_number} source_unit_evidence must be an array.')
    return payload


def validate_archive(archive, *, expected_sha256, expected_rows, expected_identity):
    actual_sha = _sha256_file(archive)
    if actual_sha != expected_sha256:
        raise CommandError(
            f'Hospital-code seed SHA-256 mismatch: expected {expected_sha256}, got {actual_sha}.'
        )
    manifest = _manifest(archive)
    if manifest.get('artifact_identity') != expected_identity:
        raise CommandError(
            f"Hospital-code seed identity is {manifest.get('artifact_identity')!r}; "
            f'expected {expected_identity!r}.'
        )
    if manifest.get('counts', {}).get('total') != expected_rows:
        raise CommandError(
            f"Hospital-code seed manifest declares {manifest.get('counts', {}).get('total')!r} "
            f'rows; expected {expected_rows}.'
        )
    count = 0
    with ZipFile(archive) as source:
        with source.open(MEMBER_NAME) as member:
            member_sha = _sha256_stream(member)
        if member_sha != manifest.get('member_sha256'):
            raise CommandError('Hospital-code seed member checksum does not match its manifest.')
        with source.open(MEMBER_NAME) as member:
            for count, raw in enumerate(member, 1):
                _validated_payload(raw, count)
    if count != expected_rows:
        raise CommandError(
            f'Hospital-code seed contains {count} rows; expected {expected_rows}.'
        )
    return manifest


def _inventory_row(payload):
    return InventoryRow(
        source_vocabulary_id=payload['source_vocabulary_id'],
        source_code=payload['source_code'],
        source_code_description=str(payload['source_code_description'] or '')[:255],
        occurrence_count=max(0, int(payload['occurrence_count'] or 0)),
        source_group_occurrence_count=payload.get('source_group_occurrence_count'),
        source_metadata=payload['source_metadata'],
        domain_id=str(payload['domain_id'] or ''),
        omop_table=str(payload['omop_table'] or ''),
        source_unit_evidence=payload['source_unit_evidence'],
    )


def _acquire_lock():
    if connection.vendor != 'postgresql':
        return True
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s)', [IMPORT_LOCK_ID])
        return cursor.fetchone()[0]


def _release_lock():
    if connection.vendor != 'postgresql':
        return
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_unlock(%s)', [IMPORT_LOCK_ID])


def import_archive(archive, job, *, batch_size=2_000):
    manifest = validate_archive(
        archive,
        expected_sha256=job.artifact_sha256,
        expected_rows=job.expected_rows,
        expected_identity=job.artifact_identity,
    )
    if not _acquire_lock():
        raise CommandError('Another hospital-code seed import is already running.')
    totals = {'total': 0, 'new': 0, 'existing': 0, 'updated': 0}
    try:
        with ZipFile(archive) as source, source.open(MEMBER_NAME) as member:
            rows = {}
            for line_number, raw in enumerate(member, 1):
                payload = _validated_payload(raw, line_number)
                row = _inventory_row(payload)
                key = (row.source_vocabulary_id, row.source_code)
                rows[key] = row
                if len(rows) >= batch_size:
                    outcome = upsert_inventory(
                        BuildResult(rows, {}), provenance=job.artifact_identity,
                        batch_size=batch_size,
                    )
                    for key in totals:
                        totals[key] += outcome[key]
                    rows = {}
            if rows:
                outcome = upsert_inventory(
                    BuildResult(rows, {}), provenance=job.artifact_identity,
                    batch_size=batch_size,
                )
                for key in totals:
                    totals[key] += outcome[key]
    finally:
        _release_lock()
    return {**manifest.get('counts', {}), **totals}


def execute_import(job_id):
    job = HospitalCodeImport.objects.get(pk=job_id)
    if job.outcome == 'applied':
        return job.stats
    HospitalCodeImport.objects.filter(pk=job_id).update(
        outcome='running', started_at=timezone.now(), completed_at=None,
        failure_reason='',
    )
    try:
        with tempfile.TemporaryDirectory(prefix='promop-hospital-seed-') as directory:
            archive = download_seed(
                job.source_url, job.artifact_filename,
                Path(directory) / job.artifact_filename,
            )
            stats = import_archive(archive, job)
    except Exception as exc:
        HospitalCodeImport.objects.filter(pk=job_id).update(
            outcome='failed', failure_reason=str(exc), completed_at=timezone.now(),
        )
        raise
    HospitalCodeImport.objects.filter(pk=job_id).update(
        outcome='applied', stats=stats, failure_reason='', completed_at=timezone.now(),
    )
    return stats


def dispatch_queued_imports():
    """Publish migration-created intents only after a new worker is ready."""
    from omop_core.tasks import import_hospital_code_seed_task

    dispatched = 0
    for job in HospitalCodeImport.objects.filter(
            outcome='queued', task_id='').order_by('pk'):
        task_id = job.task_id or f'hospital-code-import-{job.pk}'
        import_hospital_code_seed_task.apply_async(args=[job.pk], task_id=task_id)
        HospitalCodeImport.objects.filter(pk=job.pk, outcome='queued').update(task_id=task_id)
        dispatched += 1
    return dispatched
