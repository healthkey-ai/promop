"""Bounded, source-preserving access to legacy cytogenetic facts.

No token normalization, finding conversion, date substitution or interpretation
of historical selection clears as negative clinical assertions.
"""
from django.db.models import Q
from django.utils.dateparse import parse_date
from rest_framework.exceptions import ValidationError

from omop_core.models import FieldConceptMapping, Measurement, Note, Observation, PatientRecord
from omop_core.services.clinical_text import note_id
from omop_core.services.cytogenetics import LEGACY_CONCEPT_CODE, LEGACY_SOURCE, SOURCE_PREFIX, _note_source
from omop_core.services.omop_projection import CLEAR_VALUE

PAGE_SIZE = 50
TABLES = {'measurement': Measurement, 'observation': Observation}


def _cursor(value):
    if not value:
        return None
    try:
        date, table, row_id = value.split(':')
        parsed = parse_date(date)
        if not parsed or parsed.isoformat() != date or table not in TABLES:
            raise ValueError()
        if not row_id.isascii() or not row_id.isdigit() or len(row_id) > 19 or not 0 < int(row_id) <= 2**63 - 1:
            raise ValueError()
        return parsed, table, int(row_id)
    except (TypeError, ValueError):
        raise ValidationError({'cursor': 'Invalid cytogenetic history cursor.'}) from None


def history_page(person, cursor=None):
    boundary = _cursor(cursor)
    mappings = list(FieldConceptMapping.objects.filter(field_name='cytogenetic_markers'))
    candidates = []
    for table, model in TABLES.items():
        source_field, concept_field, date_field = (f'{table}_{suffix}' for suffix in ('source_value', 'concept', 'date'))
        sources = {LEGACY_SOURCE} | {m.source_value or m.concept_code for m in mappings
                                    if m.omop_table == table and (m.source_value or m.concept_code)}
        identity = Q(**{f'{source_field}__in': sources}) | Q(**{f'{source_field}__startswith': SOURCE_PREFIX})
        identity |= ((Q(**{f'{source_field}__in': ['', LEGACY_CONCEPT_CODE]}) |
                      Q(**{f'{source_field}__isnull': True})) & Q(**{
            f'{concept_field}__vocabulary_id': 'SNOMED', f'{concept_field}__concept_code': LEGACY_CONCEPT_CODE}))
        rows = model.objects.filter(person=person).filter(identity)
        if boundary:
            date, boundary_table, row_id = boundary
            earlier = Q(**{f'{date_field}__lt': date})
            if table == boundary_table:
                earlier |= Q(**{date_field: date, 'pk__lt': row_id})
            elif table < boundary_table:
                earlier |= Q(**{date_field: date})
            rows = rows.filter(earlier)
        rows = rows.select_related(concept_field, 'value_as_concept').order_by(f'-{date_field}', '-pk')[:PAGE_SIZE + 1]
        candidates.extend((getattr(row, date_field), table, row.pk, row) for row in rows)
    candidates.sort(key=lambda item: item[:3], reverse=True)
    page = candidates[:PAGE_SIZE]
    references = {note_id(row.value_as_string) for _, _, _, row in page
                  if row.value_as_string == f'[note:{note_id(row.value_as_string)}]'} - {None}
    notes = {n.pk: n for n in Note.objects.filter(person=person, pk__in=references)} if references else {}
    results = []
    for date, table, row_id, row in page:
        text = row.value_as_string
        reference = note_id(text)
        note = notes.get(reference) if text == f'[note:{reference}]' else None
        note_reference = reference is not None and text == f'[note:{reference}]'
        resolved = note is not None and note.note_source_value == _note_source(row)
        if resolved:
            text = note.note_text
        value_concept = row.value_as_concept
        results.append({
            'id': f'{table}:{row_id}', 'date': date.isoformat(),
            'source_value': getattr(row, f'{table}_source_value'),
            'text': text, 'value_concept_id': row.value_as_concept_id,
            'value_concept_name': value_concept.concept_name if value_concept else None,
            'state': 'marked_in_error' if row.is_erroneous else 'selection_cleared' if row.value_source_value == CLEAR_VALUE else 'recorded',
            'unresolved_note': note_reference and not resolved,
        })
    next_cursor = None
    if len(candidates) > PAGE_SIZE:
        date, table, row_id, _ = page[-1]
        next_cursor = f'{date.isoformat()}:{table}:{row_id}'
    legacy_summary = None
    if boundary is None:
        record = PatientRecord.objects.filter(person=person).values('cytogenetic_markers', 'user_edited_fields').first()
        if record and record['cytogenetic_markers'] and (not candidates or
                {'cytogenetic_markers', 'cytogenic_markers'} & set(record['user_edited_fields'] or [])):
            # Some legacy imports only populated the cache. Keep this text
            # visible without inventing an OMOP fact or a clinical test date.
            legacy_summary = {'text': record['cytogenetic_markers'], 'date': None}
    return {'results': results, 'next_cursor': next_cursor, 'legacy_summary': legacy_summary}
