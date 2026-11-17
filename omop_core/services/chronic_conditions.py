"""Chronic-condition overrides over AHRQ's indicator (#2566, #2512 step 5)."""
from __future__ import annotations

import csv
import re
from pathlib import Path

from omop_core.models import ChronicConditionOverride

CCIR_CSV = Path(__file__).resolve().parent.parent / 'data' / 'ccir' / 'CCIR_v2027-1.csv'

# A category (E03) or a code (E03.9, M85.80): a letter, a digit, a digit or letter
# (C4A, M1A are categories too), then up to four more.
_ICD10CM = re.compile(r'[A-Z]\d[0-9A-Z](?:\.[0-9A-Z]{1,4})?')


def normalize_icd10cm(raw: str | None) -> str | None:
    """An ICD-10-CM code in its dotted form (E039 -> E03.9, ' e03.9 ' -> E03.9),
    or None when the text is not shaped like one."""
    code = (raw or '').strip().strip("'").strip().upper()
    if '.' not in code and len(code) > 3:
        code = f'{code[:3]}.{code[3:]}'
    return code if _ICD10CM.fullmatch(code) else None


def override_for(code: str | None) -> ChronicConditionOverride | None:
    """The override that covers a code: its own, else the nearest ancestor's."""
    code = normalize_icd10cm(code)
    if not code:
        return None
    flat = code.replace('.', '')
    # Every prefix that could be an override, longest first (M85.89, M85.8, M85).
    wanted = {}
    for n in range(len(flat), 2, -1):
        prefix = flat[:n]
        wanted[prefix[:3] + ('.' + prefix[3:] if len(prefix) > 3 else '')] = n
    rows = ChronicConditionOverride.objects.filter(code__in=wanted)
    return max(rows, key=lambda r: wanted[r.code], default=None)


def read_ccir(path) -> tuple[str, dict[str, tuple[int, str]]]:
    """AHRQ's CCIR CSV as ``(release, {dotted code: (indicator, description)})``.

    The file has three header rows (title, the value legend, the column names),
    CRLF line endings, eight columns of which three are filled, and codes in
    single quotes without their decimal point (``'E039'``). A row whose code or
    indicator does not read is skipped; the header rows fall out the same way.
    """
    release, rows = '', {}
    with open(path, encoding='utf-8-sig', newline='') as handle:
        for line, row in enumerate(csv.reader(handle)):
            if line == 0 and row:
                found = re.search(r'v\d{4}\.\d+', row[0])
                release = found.group(0) if found else ''
            if len(row) < 3 or not row[2].strip().isdigit():
                continue
            # The file's own codes are taken as written (a few, like QA00101,
            # are not shaped like the codes the PHR looks up); matching is strict.
            flat = row[0].strip().strip("'").strip().upper()
            code = f'{flat[:3]}.{flat[3:]}' if len(flat) > 3 else flat
            indicator = int(row[2])
            if re.fullmatch(r'[0-9A-Z]{3}(?:\.[0-9A-Z]{1,4})?', code) and indicator in (0, 1, 9):
                rows[code] = (indicator, row[1].strip()[:255])
    return release, rows


def load_indicators(model, path=CCIR_CSV) -> dict[str, int]:
    """Make the indicator table match the CSV: add, change and drop rows.
    Takes the model so a migration can pass its historical one."""
    release, rows = read_ccir(path)
    existing = {row.code: row for row in model.objects.all()}
    created, changed = [], []
    for code, (indicator, description) in rows.items():
        row = existing.get(code)
        if row is None:
            created.append(model(code=code, indicator=indicator, description=description, release=release))
        elif (row.indicator, row.description, row.release) != (indicator, description, release):
            row.indicator, row.description, row.release = indicator, description, release
            changed.append(row)
    removed = [code for code in existing if code not in rows]
    model.objects.bulk_create(created, batch_size=5000)
    model.objects.bulk_update(changed, ['indicator', 'description', 'release'], batch_size=5000)
    if removed:
        model.objects.filter(code__in=removed).delete()
    return {'created': len(created), 'updated': len(changed), 'removed': len(removed), 'total': len(rows)}


# ---------------------------------------------------------------- the filter

NOT_CHRONIC, NO_CODE, LESS_SPECIFIC = 'not_chronic', 'no_code', 'less_specific'


def _icd10cm_of(concept) -> str | None:
    if concept is not None and (concept.vocabulary_id or '').upper() == 'ICD10CM':
        return normalize_icd10cm(concept.concept_code)
    return None


def _mapped_icd10cm(concept_ids: set[int]) -> dict[int, str]:
    """The ICD-10-CM code Athena maps each SNOMED concept back from. When
    several codes map to one concept the most specific (longest) is taken."""
    from omop_core.models import ConceptRelationship

    best: dict[int, str] = {}
    pairs = ConceptRelationship.objects.filter(
        concept_1_id__in=concept_ids, relationship_id='Mapped from', invalid_reason__isnull=True,
        concept_2__vocabulary_id='ICD10CM',
    ).values_list('concept_1_id', 'concept_2__concept_code')
    for concept_id, raw in pairs:
        code = normalize_icd10cm(raw)
        if code and (concept_id not in best or (len(code), code) > (len(best[concept_id]), best[concept_id])):
            best[concept_id] = code
    return best


def condition_icd10cm(rows) -> dict[int, str | None]:
    """An ICD-10-CM code per condition row (by pk), None when none can be found:
    the record's own ICD-10-CM coding, else its source text when that is one,
    else the code Athena maps its SNOMED concept from."""
    codes: dict[int, str | None] = {}
    snomed: dict[int, int] = {}
    for row in rows:
        code = (_icd10cm_of(row.condition_source_concept if row.condition_source_concept_id else None)
                or _icd10cm_of(row.condition_concept if row.condition_concept_id else None)
                or normalize_icd10cm(row.condition_source_value))
        codes[row.pk] = code
        if not code and row.condition_concept_id and (row.condition_concept.vocabulary_id or '').upper() == 'SNOMED':
            snomed[row.pk] = row.condition_concept_id
    if snomed:
        mapped = _mapped_icd10cm(set(snomed.values()))
        for pk, concept_id in snomed.items():
            codes[pk] = mapped.get(concept_id)
    return codes


def _indicators(codes: set[str]) -> dict[str, int]:
    """AHRQ's indicator per code. A code the table lacks (a category, or a code
    newer than the release) takes what the codes under it agree on, else 9."""
    from omop_core.models import ChronicConditionIndicator

    found = dict(ChronicConditionIndicator.objects.filter(code__in=codes).values_list('code', 'indicator'))
    for code in codes - set(found):
        under = set(ChronicConditionIndicator.objects.filter(code__startswith=code)
                    .values_list('indicator', flat=True)[:200])
        found[code] = under.pop() if len(under) == 1 else 9
    return found


def is_chronic(code: str | None, indicators: dict[str, int], overrides: dict[str, bool]) -> bool:
    """HealthTree's override first (the longest code that covers this one), then
    AHRQ's indicator; only 1 is chronic."""
    normalized = normalize_icd10cm(code)
    if normalized:
        flat = normalized.replace('.', '')
        for n in range(len(flat), 2, -1):
            prefix = flat[:n]
            key = prefix[:3] + ('.' + prefix[3:] if len(prefix) > 3 else '')
            if key in overrides:
                return overrides[key]
    return indicators.get(code) == 1


def chronic_filter(rows, *, self_reported=frozenset()):
    """Which of the active "other" conditions are not shown, and why (#2512).

    Returns ``(reasons, earliest)``: the reason per hidden row pk
    (``no_code``, ``not_chronic``, ``less_specific``) and, for a row that
    absorbed less specific ones, the earliest date among them. Rows the patient
    typed in themselves (``self_reported`` pks) are kept: they have no code to
    judge. With no AHRQ table loaded nothing is judged, so nothing is hidden.
    """
    from omop_core.models import ChronicConditionIndicator, ConceptAncestor

    if not rows or not ChronicConditionIndicator.objects.exists():
        return {}, {}
    reasons: dict[int, str] = {}
    candidates = [row for row in rows if row.pk not in self_reported]
    for row in candidates:
        if not row.condition_concept_id and not (row.condition_source_value or '').strip():
            reasons[row.pk] = NO_CODE
    judged = [row for row in candidates if row.pk not in reasons]
    codes = condition_icd10cm(judged)
    indicators = _indicators({code for code in codes.values() if code})
    overrides = dict(ChronicConditionOverride.objects.values_list('code', 'is_chronic'))  # a small table
    kept = []
    for row in judged:
        if is_chronic(codes[row.pk], indicators, overrides):
            kept.append(row)
        else:
            reasons[row.pk] = NOT_CHRONIC
    # When one kept concept is an ancestor of another, the descendant says it better.
    by_concept: dict[int, list] = {}
    for row in kept:
        if row.condition_concept_id:
            by_concept.setdefault(row.condition_concept_id, []).append(row)
    earliest: dict[int, object] = {}
    if len(by_concept) > 1:
        ids = set(by_concept)
        pairs = ConceptAncestor.objects.filter(
            ancestor_concept_id__in=ids, descendant_concept_id__in=ids, min_levels_of_separation__gt=0,
        ).values_list('ancestor_concept_id', 'descendant_concept_id')
        for ancestor, descendant in pairs:
            for row in by_concept[ancestor]:
                reasons[row.pk] = LESS_SPECIFIC
            heir = by_concept[descendant][0]
            dates = [r.condition_start_date for r in by_concept[ancestor] + by_concept[descendant]
                     if r.condition_start_date]
            if dates:
                earliest[heir.pk] = min(dates + ([earliest[heir.pk]] if heir.pk in earliest else []))
    return reasons, earliest
