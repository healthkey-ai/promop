"""Validate and atomically import source-code inventories from CSV."""

import csv
import hashlib
import io
from dataclasses import dataclass

from django.db import connection, transaction
from django.utils import timezone

from omop_core.models import CodeMappingUpload, SourceCodeConceptMapping


MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_ROWS = 100_000
REQUIRED_HEADERS = frozenset({'source code', 'source description'})
ALLOWED_HEADERS = REQUIRED_HEADERS | {'seen count'}


class CodeMappingUploadError(ValueError):
    def __init__(self, detail, errors=None):
        super().__init__(detail)
        self.detail = detail
        self.errors = errors or []


@dataclass(frozen=True)
class UploadRow:
    line: int
    source_code: str
    source_description: str
    seen_count: int


def _normalise_header(value):
    return (value or '').strip().lower()


def parse_upload(upload):
    content = upload.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise CodeMappingUploadError(
            f'CSV exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit.'
        )
    digest = hashlib.sha256(content).hexdigest()
    try:
        text = content.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise CodeMappingUploadError('CSV must be UTF-8 encoded.') from exc
    try:
        reader = csv.reader(io.StringIO(text, newline=''), strict=True)
        raw_headers = next(reader)
    except (StopIteration, csv.Error) as exc:
        raise CodeMappingUploadError('CSV must contain a header row.') from exc

    headers = [_normalise_header(header) for header in raw_headers]
    if len(headers) != len(set(headers)):
        raise CodeMappingUploadError('CSV contains duplicate headers after case/whitespace normalization.')
    missing = sorted(REQUIRED_HEADERS - set(headers))
    unknown = sorted(set(headers) - ALLOWED_HEADERS)
    if missing or unknown:
        parts = []
        if missing:
            parts.append('missing required column(s): ' + ', '.join(missing))
        if unknown:
            parts.append('unknown column(s): ' + ', '.join(unknown))
        raise CodeMappingUploadError('Invalid CSV header: ' + '; '.join(parts) + '.')

    indexes = {header: index for index, header in enumerate(headers)}
    rows = []
    seen_codes = {}
    errors = []
    data_rows = 0
    try:
        for line, values in enumerate(reader, start=2):
            if not values or all(not value.strip() for value in values):
                continue
            data_rows += 1
            if data_rows > MAX_UPLOAD_ROWS:
                raise CodeMappingUploadError(f'CSV exceeds the {MAX_UPLOAD_ROWS:,}-row limit.')
            if len(values) != len(headers):
                errors.append({'row': line, 'detail': f'Expected {len(headers)} columns, found {len(values)}.'})
                continue
            code = values[indexes['source code']].strip()
            description = values[indexes['source description']].strip()
            raw_count = values[indexes['seen count']].strip() if 'seen count' in indexes else ''
            if not code:
                errors.append({'row': line, 'field': 'source code', 'detail': 'Source code is required.'})
                continue
            invalid = False
            if len(code) > 100:
                errors.append({'row': line, 'field': 'source code', 'detail': 'Source code exceeds 100 characters.'})
                invalid = True
            if len(description) > 255:
                errors.append({'row': line, 'field': 'source description', 'detail': 'Source description exceeds 255 characters.'})
                invalid = True
            try:
                seen_count = int(raw_count) if raw_count else 0
                if seen_count < 0:
                    raise ValueError
            except ValueError:
                errors.append({'row': line, 'field': 'seen count', 'detail': 'Seen count must be a non-negative integer.'})
                continue
            if code in seen_codes:
                errors.append({
                    'row': line, 'field': 'source code',
                    'detail': f'Duplicate source code; first appears on row {seen_codes[code]}.',
                })
                continue
            seen_codes[code] = line
            if invalid:
                continue
            rows.append(UploadRow(line, code, description, seen_count))
    except csv.Error as exc:
        raise CodeMappingUploadError(f'Malformed CSV near row {reader.line_num}: {exc}.') from exc

    if errors:
        raise CodeMappingUploadError(
            f'CSV validation failed on {len(errors)} row(s).', errors=errors[:100],
        )
    if not rows:
        raise CodeMappingUploadError('CSV contains no data rows.')
    return rows, digest


def _lock_vocabulary(vocabulary):
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext('code-mapping-upload:' || %s))",
                [vocabulary],
            )


def import_upload(*, upload, vocabulary, provenance, actor):
    vocabulary = (vocabulary or '').strip()
    provenance = (provenance or '').strip()
    if not vocabulary:
        raise CodeMappingUploadError('Source vocabulary is required.')
    if vocabulary.startswith('HK-'):
        raise CodeMappingUploadError('HK-* vocabularies are destinations, not source code systems.')
    if len(vocabulary) > 255:
        raise CodeMappingUploadError('Source vocabulary exceeds 255 characters.')
    if not provenance:
        raise CodeMappingUploadError('Provenance is required.')
    if len(provenance) > 50:
        raise CodeMappingUploadError('Provenance exceeds 50 characters.')

    rows, digest = parse_upload(upload)
    filename = (getattr(upload, 'name', '') or 'upload.csv')[:255]
    with transaction.atomic():
        _lock_vocabulary(vocabulary)
        receipt, created = CodeMappingUpload.objects.get_or_create(
            source_vocabulary_id=vocabulary,
            provenance=provenance,
            content_sha256=digest,
            defaults={'actor': actor, 'filename': filename},
        )
        if not created:
            return receipt, True

        existing = {}
        source_codes = [row.source_code for row in rows]
        for offset in range(0, len(source_codes), 10_000):
            locked = SourceCodeConceptMapping.objects.select_for_update().filter(
                organization__isnull=True,
                source_vocabulary_id=vocabulary,
                source_code__in=source_codes[offset:offset + 10_000],
            )
            existing.update((mapping.source_code, mapping) for mapping in locked)
        now = timezone.now()
        inserts = []
        updates = []
        unchanged = 0
        for row in rows:
            mapping = existing.get(row.source_code)
            if mapping is None:
                inserts.append(SourceCodeConceptMapping(
                    organization=None,
                    source_vocabulary_id=vocabulary,
                    source_code=row.source_code,
                    source_code_description=row.source_description,
                    occurrence_count=row.seen_count,
                    first_seen=now if row.seen_count else None,
                    last_seen=now if row.seen_count else None,
                    origin='import',
                    origin_system=provenance,
                    status='proposed',
                    created_by=actor,
                    updated_by=actor,
                ))
                continue

            changed = False
            if row.source_description and row.source_description != mapping.source_code_description:
                mapping.source_code_description = row.source_description
                changed = True
            if row.seen_count:
                mapping.occurrence_count += row.seen_count
                mapping.first_seen = mapping.first_seen or now
                mapping.last_seen = now
                changed = True
            if not mapping.origin_system:
                mapping.origin_system = provenance
                changed = True
            if changed:
                mapping.updated_by = actor
                mapping.updated_at = now
                updates.append(mapping)
            else:
                unchanged += 1

        if inserts:
            SourceCodeConceptMapping.objects.bulk_create(inserts, batch_size=2_000)
        if updates:
            SourceCodeConceptMapping.objects.bulk_update(
                updates,
                ['source_code_description', 'occurrence_count', 'first_seen',
                 'last_seen', 'origin_system', 'updated_by', 'updated_at'],
                batch_size=1_000,
            )
        receipt.total_rows = len(rows)
        receipt.inserted_rows = len(inserts)
        receipt.updated_rows = len(updates)
        receipt.unchanged_rows = unchanged
        receipt.save(update_fields=[
            'total_rows', 'inserted_rows', 'updated_rows', 'unchanged_rows',
        ])
    return receipt, False


def serialize_receipt(receipt, duplicate=False):
    return {
        'upload_id': receipt.pk,
        'duplicate': duplicate,
        'source_vocabulary_id': receipt.source_vocabulary_id,
        'provenance': receipt.provenance,
        'filename': receipt.filename,
        'sha256': receipt.content_sha256,
        'total': receipt.total_rows,
        'inserted': receipt.inserted_rows,
        'updated': receipt.updated_rows,
        'unchanged': receipt.unchanged_rows,
        'created_at': receipt.created_at,
    }
