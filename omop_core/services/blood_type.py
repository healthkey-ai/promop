"""Derive the latest blood type from dated OMOP Measurement facts."""
import re
from datetime import datetime, time, timezone

ABO_RH, ABO, RH = '882-1', '883-9', '10331-7'
BLOOD_TYPING_CODES = frozenset({ABO_RH, ABO, RH})
_GROUP = re.compile(r'\b(AB|A|B|O)\b')


def _text(row):
    text = (row.value_as_string or '').strip()
    if text:
        return text
    if row.value_as_concept_id:
        return (row.value_as_concept.concept_name or '').strip()
    return ''


def _rh(text):
    if 'neg' in text.lower() or re.search(r'(AB|A|B|O)\s*-', text, re.I) or text == '-':
        return 'negative'
    if 'pos' in text.lower() or '+' in text:
        return 'positive'
    return None


def _event_key(row):
    at = row.measurement_datetime or datetime.combine(row.measurement_date, time.min, tzinfo=timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.timestamp(), row.measurement_id


def blood_type_from_measurements(rows):
    """Latest usable ABO and Rh components from combined or separate results.

    Ignore erroneous/empty results and codes from other vocabularies. Concept
    0 may fall back to the original LOINC source concept. Preserve unfamiliar
    result wording rather than inventing a type. No database queries are made
    when the caller supplies the usual select_related measurement snapshot.
    """
    candidates = []
    for row in rows:
        if row.is_erroneous:
            continue
        concept = row.measurement_concept if row.measurement_concept_id else row.measurement_source_concept
        if (concept is not None and concept.vocabulary_id == 'LOINC'
                and concept.concept_code in BLOOD_TYPING_CODES and _text(row)):
            candidates.append((row, concept.concept_code))
    newest = {}
    for row, code in sorted(candidates, key=lambda item: _event_key(item[0]), reverse=True):
        newest.setdefault(code, row)
    primary = [newest[code] for code in (ABO_RH, ABO) if code in newest]
    if not primary:
        return None
    row = max(primary, key=_event_key)
    text = _text(row)
    group = _GROUP.search(text.upper())
    if group is None:
        return text
    rh_rows = [newest[code] for code in (ABO_RH, RH) if code in newest]
    rh = _rh(_text(max(rh_rows, key=_event_key))) if rh_rows else None
    return ' '.join(part for part in (group.group(1), rh) if part)
