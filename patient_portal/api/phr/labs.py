"""Labs: the latest result per test, and one test's full history.

A "test" is a measurement concept — the standard concept, or the source
concept where an import left the standard one unmapped (concept 0), the same
rule PRomop's lab-results screen uses. Ranking, panels and cancer markers come
from LabMarker rows that clinical admins maintain. Vitals are excluded: they
are not part of the record's first version.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from django.db.models import Q

from omop_core.models import Measurement
from patient_portal.models import LabMarker

from .sources import row_sources

# LOINC codes for vitals and performance scores, which the Labs section leaves out.
VITALS = frozenset({
    '29463-7', '3141-9', '8302-2', '39156-5', '8480-6', '8462-4', '85354-9', '8867-4',
    '8310-5', '59408-5', '2708-6', '9279-1', '89247-1', '89243-0',
})
HISTORY_LIMIT = 500
# Results per test in the list's history table; the detail endpoint has them all.
RECENT_LIMIT = 8
PANEL_ORDER = ['CBC', 'BMP', 'CMP']

def split_name(concept_name: str) -> tuple[str, str | None]:
    """Analyte and specimen from a LOINC long name.

    "Hemoglobin [Mass/volume] in Blood" → ("Hemoglobin", "Blood");
    "Creatinine [Mass/volume] in Serum or Plasma by ..." → ("Creatinine", "Serum or Plasma").
    Names without " in " are returned whole. Plain string scanning, not a
    regex: concept names come from imported data, and the obvious pattern
    backtracks polynomially on crafted input (CodeQL py/polynomial-redos).
    """
    name = concept_name or ''
    bracket = name.find(' [')
    close = name.find(']', bracket) if bracket != -1 else -1
    analyte_end = bracket if close != -1 else -1
    marker = name.find(' in ', close + 1 if close != -1 else 0)
    if marker == -1:
        return concept_name, None
    analyte = name[:analyte_end if analyte_end != -1 else marker].strip()
    specimen = name[marker + len(' in '):]
    by = specimen.find(' by ')
    if by != -1:
        specimen = specimen[:by]
    return (analyte or concept_name), (specimen.strip() or None)


def effective_concept(row: Measurement):
    if row.measurement_concept_id == 0 and row.measurement_source_concept_id:
        return row.measurement_source_concept
    return row.measurement_concept


def _number(value: Decimal | None) -> float | int | None:
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


def flag(row: Measurement) -> str | None:
    value = row.value_as_number
    if value is None:
        return None
    if row.range_low is not None and value < row.range_low:
        return 'low'
    if row.range_high is not None and value > row.range_high:
        return 'high'
    if row.range_low is not None or row.range_high is not None:
        return 'normal'
    return None


def result(row: Measurement, source: dict) -> dict[str, Any]:
    unit = (row.unit_source_value or '').strip() or (
        row.unit_concept.concept_code if row.unit_concept_id and row.unit_concept else None
    )
    item = {
        'id': row.pk,
        'date': row.measurement_date.isoformat(),
        'value': _number(row.value_as_number),
        'value_text': (row.value_as_string or '').strip() or None,
        'unit': unit or None,
        'range': {'low': _number(row.range_low), 'high': _number(row.range_high)}
        if row.range_low is not None or row.range_high is not None
        else None,
        'flag': flag(row),
        'source': source,
    }
    return {k: v for k, v in item.items() if v is not None}


def _brief(row: Measurement) -> dict[str, Any]:
    item = {
        'date': row.measurement_date.isoformat(),
        'value': _number(row.value_as_number),
        'value_text': (row.value_as_string or '').strip() or None,
        'flag': flag(row),
    }
    return {k: v for k, v in item.items() if v is not None}


def _genetic_ids(person) -> set[int]:
    """Genetic findings, which Genetic testing shows: each variant's parent
    measurement and the component rows that point back to it."""
    from omop_core.services.genomics import PARENT_CODE

    parents = set(
        Measurement.objects.filter(person=person)
        .filter(Q(measurement_concept__concept_code=PARENT_CODE) | Q(measurement_source_concept__concept_code=PARENT_CODE))
        .values_list('pk', flat=True)
    )
    if not parents:
        return set()
    components = Measurement.objects.filter(person=person, measurement_event_id__in=parents).values_list('pk', flat=True)
    return parents | set(components)


def _rows(person):
    return (
        Measurement.objects.filter(person=person, is_erroneous=False)
        .exclude(pk__in=_genetic_ids(person))
        .exclude(value_as_number__isnull=True, value_as_string__isnull=True)
        .select_related(
            'measurement_concept', 'measurement_source_concept', 'unit_concept', 'visit_occurrence',
        )
        .order_by('-measurement_date', '-measurement_id')
    )


def _grouped(rows) -> dict[int, list[Measurement]]:
    groups: dict[int, list[Measurement]] = defaultdict(list)
    for row in rows:
        concept = effective_concept(row)
        if concept is None or concept.concept_code in VITALS:
            continue
        groups[concept.concept_id].append(row)
    return groups


def _test_meta(concept, markers: dict[str, LabMarker]) -> dict[str, Any]:
    marker = markers.get(concept.concept_code) if (concept.vocabulary_id or '') == 'LOINC' else None
    analyte, specimen = split_name(concept.concept_name)
    meta = {
        'id': str(concept.concept_id),
        'name': (marker.label if marker and marker.label else None) or analyte,
        'specimen': specimen,
        'rank': marker.rank if marker else None,
        'panels': list(marker.panels) if marker else [],
        'disease_slugs': list(marker.disease_slugs) if marker else [],
    }
    return {k: v for k, v in meta.items() if v is not None}


def _cancers(person, record) -> list[dict[str, str]]:
    """The patient's cancers as lab filters: the record's slug for the primary, the name's for the rest."""
    from omop_core.services.disease_episodes import disease_slug

    from .views import diagnoses

    out, seen = [], set()
    primary_slug = ((record.disease_slug if record else '') or '').lower()
    for cancer in diagnoses(person, record)['cancer']:
        slug = primary_slug if cancer['id'] == 'primary' and primary_slug else (disease_slug(cancer['name']) or '')
        if slug and slug not in seen:
            seen.add(slug)
            out.append({'slug': slug, 'name': cancer['name']})
    return out


def _markers() -> dict[str, LabMarker]:
    return {m.loinc_code: m for m in LabMarker.objects.all()}


def labs(person, record) -> dict[str, Any]:
    rows = list(_rows(person))
    groups = _grouped(rows)
    latest_rows = [group[0] for group in groups.values()]
    sources = row_sources(Measurement, latest_rows, type_attr='measurement_type_concept', date_attr='measurement_date')
    markers = _markers()

    tests = []
    for concept_id, group in groups.items():
        latest = group[0]
        tests.append({
            **_test_meta(effective_concept(latest), markers),
            'latest': result(latest, sources[latest.pk]),
            'recent': [_brief(row) for row in group[:RECENT_LIMIT]],
            'count': len(group),
        })
    tests.sort(key=lambda t: (t.get('rank') is None, t.get('rank') or 0, t['name'].lower()))

    panels = sorted(
        {p for t in tests for p in t['panels']},
        key=lambda p: (PANEL_ORDER.index(p) if p in PANEL_ORDER else len(PANEL_ORDER), p),
    )
    marked = {s for t in tests for s in t['disease_slugs']}
    filters = {
        # A filter for each of the patient's cancers that has marker tests, primary first.
        'diagnoses': [c for c in _cancers(person, record) if c['slug'] in marked],
        'panels': panels,
    }
    return {'tests': tests, 'filters': filters}


def lab_history(person, concept_id: int, record=None) -> dict[str, Any] | None:
    rows = list(
        _rows(person).filter(
            Q(measurement_concept_id=concept_id)
            | Q(measurement_concept_id=0, measurement_source_concept_id=concept_id)
        )[:HISTORY_LIMIT]
    )
    if not rows or effective_concept(rows[0]).concept_code in VITALS:
        return None
    sources = row_sources(Measurement, rows, type_attr='measurement_type_concept', date_attr='measurement_date')
    from .therapy import lines_for_markers

    meta = _test_meta(effective_concept(rows[0]), _markers())
    return {
        **meta,
        'history': [result(row, sources[row.pk]) for row in rows],
        # Treatment lines, for shading the chart and naming the treatment at
        # the time of a result: those of the cancer this test marks (a PSA
        # chart shows the prostate cancer's lines), else the primary cancer's.
        'therapy': [
            # id: a shared chart keeps only the lines the patient shared.
            {k: line[k] for k in ('id', 'number', 'regimen', 'start', 'end') if k in line}
            for line in lines_for_markers(person, record, meta.get('disease_slugs'))
        ],
    }
