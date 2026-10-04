"""Validate and atomically import source-code inventories from CSV."""

import csv
import hashlib
import io
from dataclasses import dataclass

from django.db import connection, transaction
from django.utils import timezone

from omop_core.mapping.code_resolution import repoint_clinical_rows
from omop_core.models import CodeMappingUpload, Concept, SourceCodeConceptMapping
from omop_core.services.source_vocabularies import DOMAIN_TO_TABLE


MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_ROWS = 100_000
REQUIRED_HEADERS = frozenset({'source code', 'source description'})
DESTINATION_HEADER = 'destination concept id'
STATE_HEADER = 'state'
ALLOWED_HEADERS = REQUIRED_HEADERS | {'seen count', DESTINATION_HEADER, STATE_HEADER}


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
    destination_concept_id: int | None
    state: str


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
            raw_destination = (
                values[indexes[DESTINATION_HEADER]].strip()
                if DESTINATION_HEADER in indexes else ''
            )
            state = (
                values[indexes[STATE_HEADER]].strip().lower()
                if STATE_HEADER in indexes else ''
            ) or 'proposed'
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
            try:
                destination_concept_id = int(raw_destination) if raw_destination else None
                if destination_concept_id is not None and destination_concept_id <= 0:
                    raise ValueError
            except ValueError:
                errors.append({
                    'row': line, 'field': DESTINATION_HEADER,
                    'detail': 'Destination concept ID must be a positive integer.',
                })
                continue
            if state not in {'approved', 'proposed'}:
                errors.append({
                    'row': line, 'field': STATE_HEADER,
                    'detail': 'State must be Approved, Proposed, or blank.',
                })
                continue
            if state == 'approved' and destination_concept_id is None:
                errors.append({
                    'row': line, 'field': STATE_HEADER,
                    'detail': 'Approved state requires a destination concept ID.',
                })
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
            rows.append(UploadRow(
                line, code, description, seen_count, destination_concept_id, state,
            ))
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


def _destination_concepts(rows):
    """Resolve and validate every requested destination before writing anything."""
    ids = {row.destination_concept_id for row in rows if row.destination_concept_id}
    concepts = {}
    requested = sorted(ids)
    for offset in range(0, len(requested), 10_000):
        concepts.update(Concept.objects.in_bulk(requested[offset:offset + 10_000]))

    errors = []
    for row in rows:
        concept_id = row.destination_concept_id
        if concept_id is None:
            continue
        concept = concepts.get(concept_id)
        if concept is None:
            errors.append({
                'row': row.line, 'field': DESTINATION_HEADER,
                'detail': f'Destination OMOP concept {concept_id} was not found.',
            })
        elif concept.invalid_reason:
            errors.append({
                'row': row.line, 'field': DESTINATION_HEADER,
                'detail': f'Destination OMOP concept {concept_id} is retired.',
            })
        elif concept.standard_concept != 'S' and concept.source != 'HealthKey':
            errors.append({
                'row': row.line, 'field': DESTINATION_HEADER,
                'detail': (
                    f'Destination OMOP concept {concept_id} is not standard or '
                    'HealthKey-authored.'
                ),
            })
        elif concept.domain_id not in DOMAIN_TO_TABLE:
            errors.append({
                'row': row.line, 'field': DESTINATION_HEADER,
                'detail': (
                    f'Destination OMOP concept {concept_id} has unsupported '
                    f'domain {concept.domain_id!r}.'
                ),
            })
    if errors:
        raise CodeMappingUploadError(
            f'CSV validation failed on {len(errors)} row(s).', errors=errors[:100],
        )
    return concepts


def import_upload(*, upload, vocabulary, provenance, actor, can_approve=True):
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
    if not can_approve and any(row.state == 'approved' for row in rows):
        raise CodeMappingUploadError(
            'Only org admins and staff can import mappings with Approved state.'
        )
    concepts = _destination_concepts(rows)
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
        if not can_approve:
            protected = [
                row for row in rows
                if row.destination_concept_id is not None
                and (mapping := existing.get(row.source_code)) is not None
                and mapping.status == 'approved'
            ]
            if protected:
                raise CodeMappingUploadError(
                    f'CSV validation failed on {len(protected)} row(s).',
                    errors=[{
                        'row': row.line,
                        'field': STATE_HEADER,
                        'detail': (
                            'Only org admins and staff can change an approved mapping.'
                        ),
                    } for row in protected[:100]],
                )
        now = timezone.now()
        inserts = []
        updates = []
        unchanged = 0
        approval_repoints = []
        for row in rows:
            mapping = existing.get(row.source_code)
            concept = concepts.get(row.destination_concept_id)
            approved = concept is not None and row.state == 'approved'
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
                    target_concept=concept,
                    destination_vocabulary_id=concept.vocabulary_id if concept else '',
                    domain_id=concept.domain_id if concept else '',
                    omop_table=DOMAIN_TO_TABLE.get(concept.domain_id, '') if concept else '',
                    status='approved' if approved else 'proposed',
                    reviewer=actor if approved else None,
                    reviewed_at=now if approved else None,
                    created_by=actor,
                    updated_by=actor,
                ))
                if approved:
                    approval_repoints.append((row.source_code, False, set(), concept.pk))
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
            if concept:
                was_approved = mapping.status == 'approved'
                previous_concept_id = mapping.target_concept_id
                approval_changed = (
                    approved and (
                        not was_approved
                        or previous_concept_id != concept.pk
                    )
                )
                destination_changed = (
                    previous_concept_id is not None
                    and previous_concept_id != concept.pk
                )
                pending_repoints = set(mapping.pending_repoint_concept_ids)
                if not approved and destination_changed:
                    pending_repoints.add(previous_concept_id)
                if approval_changed:
                    old_concept_ids = pending_repoints
                    if destination_changed:
                        old_concept_ids.add(previous_concept_id)
                    approval_repoints.append((
                        row.source_code, was_approved,
                        old_concept_ids, concept.pk,
                    ))
                expected_table = DOMAIN_TO_TABLE[concept.domain_id]
                if (
                    mapping.status != row.state
                    or mapping.target_concept_id != concept.pk
                    or mapping.destination_vocabulary_id != concept.vocabulary_id
                    or mapping.domain_id != concept.domain_id
                    or mapping.omop_table != expected_table
                ):
                    if mapping.target_concept_id != concept.pk:
                        # The ranker's score was for the destination it chose.
                        mapping.suggestion_confidence = None
                    mapping.target_concept = concept
                    mapping.destination_vocabulary_id = concept.vocabulary_id or ''
                    mapping.domain_id = concept.domain_id
                    mapping.omop_table = expected_table
                    mapping.status = row.state
                    changed = True
                next_pending = [] if approved else sorted(pending_repoints)
                if mapping.pending_repoint_concept_ids != next_pending:
                    mapping.pending_repoint_concept_ids = next_pending
                    changed = True
                if approval_changed:
                    mapping.reviewer = actor
                    mapping.reviewed_at = now
                    changed = True
                elif was_approved and not approved:
                    mapping.reviewer = None
                    mapping.reviewed_at = None
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
                 'last_seen', 'origin_system', 'target_concept',
                 'destination_vocabulary_id', 'domain_id', 'omop_table', 'status',
                 'reviewer', 'reviewed_at', 'pending_repoint_concept_ids',
                 'suggestion_confidence', 'updated_by', 'updated_at'],
                batch_size=1_000,
            )
        if approval_repoints:
            approved = {
                mapping.source_code: mapping
                for mapping in SourceCodeConceptMapping.objects.filter(
                    organization__isnull=True,
                    source_vocabulary_id=vocabulary,
                    source_code__in=[item[0] for item in approval_repoints],
                )
            }
            for source_code, was_approved, old_concept_ids, new_concept_id in approval_repoints:
                mapping = approved[source_code]
                if not was_approved:
                    repoint_clinical_rows(
                        mapping=mapping, old_concept_id=0,
                        new_concept_id=new_concept_id, match_description=False,
                    )
                for old_concept_id in sorted(old_concept_ids - {new_concept_id}):
                    repoint_clinical_rows(
                        mapping=mapping, old_concept_id=old_concept_id,
                        new_concept_id=new_concept_id,
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
