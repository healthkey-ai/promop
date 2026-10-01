"""Build and import the HealthTree Epic/Cerner source-code inventory.

The export grain includes resource paths and, in the supplemental CSV, units.
SCCM's grain is the exact vendor system plus its code, so this module performs
the collapse explicitly and never reduces an Epic/Cerner URI to the vendor tab
name.  That URI is the only hospital/tenant boundary present in these files.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import math

from django.db import transaction
from django.utils import timezone

from omop_core.mapping.code_resolution import SOURCE_CODE_MAX
from omop_core.models import SourceCodeConceptMapping
from omop_core.services import source_vocabularies


DESCRIPTION_MAX = 255
SYSTEM_MAX = 255
UNIT_MAX = 100


@dataclass
class InventoryRow:
    source_vocabulary_id: str
    source_code: str
    source_code_description: str
    occurrence_count: int
    source_group_occurrence_count: int | None = None
    source_metadata: dict = field(default_factory=dict)
    domain_id: str = ''
    omop_table: str = ''
    source_unit_evidence: list[dict] = field(default_factory=list)


@dataclass
class BuildResult:
    rows: dict[tuple[str, str], InventoryRow]
    stats: Counter


def _text(value):
    return str(value or '').strip()


def _count(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _number(value):
    """Return a finite JSON-safe number, or None for absent/invalid evidence."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _source_metadata(state):
    """Compact the code-level extract evidence without claiming clinical truth."""
    if not state['has_metadata']:
        return {}
    records = state['occurrence_count']
    metadata = {
        'records': records,
        'patients': state['patients'],
        'codings': state['codings'],
    }
    range_records = state['range_records']
    unit_records = state['unit_records']
    if unit_records:
        metadata['unit_coverage'] = {
            'records': unit_records,
            'percent': round(unit_records * 100 / records, 1) if records else None,
        }
    if range_records:
        metadata['reference_range'] = {
            'records': range_records,
            'percent': round(range_records * 100 / records, 1) if records else None,
            **state['range_summary'],
        }
    representative = state['representative']
    category = representative.get('category_top', '')
    category_mix = representative.get('category_mix', '')
    if category or category_mix:
        metadata['category'] = {'top': category, 'mix': category_mix}
    value_types = {}
    for key, label in (
        ('pct_value_quantity', 'quantity'),
        ('pct_value_codeable_concept', 'coded'),
        ('pct_value_string', 'text'),
    ):
        value = representative.get(key)
        if value is not None:
            value_types[label] = value
    if value_types:
        metadata['value_types'] = value_types
    return metadata


def _empty_state(primary):
    return {
        'occurrence_count': 0, 'descriptions': Counter(),
        'domains': Counter(), 'primary': primary,
        'source_group_occurrence_count': None,
        # Streaming aggregates only. Keeping each wide Parquet row here would
        # multiply memory use across a 690k-key import.
        'has_metadata': False, 'patients': 0, 'codings': 0,
        'unit_records': 0, 'range_records': 0,
        'range_evidence_records': -1, 'range_summary': {},
        'representative_records': -1, 'representative': {},
    }


def _merge_source_metadata(state, raw, count):
    state['has_metadata'] = True
    state['patients'] += _count(raw.get('n_patients'))
    state['codings'] += _count(raw.get('n_codings'))
    state['unit_records'] += _count(raw.get('n_records_with_unit'))
    range_records = _count(raw.get('n_records_with_reference_range'))
    state['range_records'] += range_records
    if range_records > state['range_evidence_records']:
        # A weighted median cannot be reconstructed from medians. Retain the
        # highest-evidence extract row and identify these values as medians.
        state['range_evidence_records'] = range_records
        state['range_summary'] = {
            'low_p50': _number(raw.get('reference_range_low_p50')),
            'high_p50': _number(raw.get('reference_range_high_p50')),
            'unit': _text(raw.get('unit_from_reference_range_top')),
        }
    if count > state['representative_records']:
        state['representative_records'] = count
        state['representative'] = {
            'category_top': _text(raw.get('category_top')),
            'category_mix': _text(raw.get('category_mix')),
            'pct_value_quantity': _number(raw.get('pct_value_quantity')),
            'pct_value_codeable_concept': _number(raw.get('pct_value_codeable_concept')),
            'pct_value_string': _number(raw.get('pct_value_string')),
        }


def _key(raw, stats, cohort):
    system = _text(raw.get('coding_system')).rstrip('/')
    code = _text(raw.get('coding_code'))
    vendor = source_vocabularies.hospital_vendor(system)
    if not vendor:
        stats['skipped_non_hospital'] += 1
        return None
    if not code:
        stats['skipped_missing_code'] += 1
        return None
    if len(system) > SYSTEM_MAX:
        stats['skipped_long_system'] += 1
        return None
    if len(code) > SOURCE_CODE_MAX:
        code = code[:SOURCE_CODE_MAX]
        stats['truncated_codes'] += 1
    stats[f'{cohort}_{vendor.lower()}_rows'] += 1
    return system, code


def _description(raw):
    return (
        _text(raw.get('source_label'))
        or _text(raw.get('sample_display'))
        or _text(raw.get('display'))
        or _text(raw.get('sample_codeable_concept_text'))
    )[:DESCRIPTION_MAX]


def _domain(raw):
    resource = _text(raw.get('resource_type'))
    path = _text(raw.get('field_path') or raw.get('json_field'))
    category = _text(raw.get('category_top')).casefold()
    if resource == 'Procedure':
        return 'Procedure', 'procedure'
    if resource == 'Immunization':
        return 'Drug', 'drug_exposure'
    if resource != 'Observation':
        return '', ''
    # Coded answers describe an observation result, not the test whose value
    # was recorded.  Treat only the test/component code as a measurement.
    if 'valueCodeableConcept' in path:
        return 'Observation', 'observation'
    if category in {'laboratory', 'vital-signs'}:
        return 'Measurement', 'measurement'
    return 'Observation', 'observation'


def build_inventory(primary_rows, *, unit_rows=(), supplemental_rows=(),
                    evidence_source='healthtree-unmapped-v2'):
    """Collapse primary rows and add only supplement-only source keys."""
    stats = Counter()
    states = {}
    for raw in primary_rows:
        key = _key(raw, stats, 'primary')
        if key is None:
            continue
        count = _count(raw.get('n_records'))
        state = states.setdefault(key, _empty_state(True))
        state['occurrence_count'] += count
        _merge_source_metadata(state, raw, count)
        group_count = _count(raw.get('label_group_records_if_mapped'))
        if group_count:
            state['source_group_occurrence_count'] = max(
                state['source_group_occurrence_count'] or 0, group_count,
            )
        description = _description(raw)
        if description:
            state['descriptions'][description] += max(1, count)
        domain = _domain(raw)
        if domain[0]:
            state['domains'][domain] += max(1, count)

    stats['primary_keys'] = len(states)
    supplement = {}
    for raw in supplemental_rows:
        key = _key(raw, stats, 'supplement')
        if key is None or key in states:
            if key in states:
                stats['supplement_overlap_rows'] += 1
            continue
        count = _count(raw.get('fhir_n_records') or raw.get('n_records'))
        state = supplement.setdefault(key, _empty_state(False))
        # The CSV repeats the code-level count once per candidate unit, so max
        # is the only non-duplicating aggregation at this grain.
        state['occurrence_count'] = max(state['occurrence_count'], count)
        description = _description(raw)
        if description:
            state['descriptions'][description] += max(1, count)
    states.update(supplement)
    stats['supplement_keys'] = len(supplement)

    evidence = {}
    for raw in unit_rows:
        key = _key(raw, stats, 'unit')
        if key is None or key not in states:
            continue
        display = _text(raw.get('unit_display'))
        code = _text(raw.get('unit_ucum'))
        if display == '<no unit>':
            stats['skipped_no_unit'] += 1
            continue
        if not display and not code:
            stats['skipped_no_unit'] += 1
            continue
        unit_key = (display[:UNIT_MAX], code[:UNIT_MAX])
        units = evidence.setdefault(key, {})
        current = units.setdefault(unit_key, {
            'count': 0, 'patients': 0, 'values': 0, 'distribution': None,
            'all_suppressed': True,
        })
        current['count'] += _count(raw.get('n_records'))
        current['patients'] += _count(raw.get('n_patients'))
        values = _count(raw.get('n_values'))
        current['values'] += values
        suppressed = bool(raw.get('is_suppressed'))
        current['all_suppressed'] = current['all_suppressed'] and suppressed
        if not suppressed:
            distribution = {
                label: _number(raw.get(column))
                for label, column in (
                    ('min', 'value_min'), ('p5', 'value_p5'),
                    ('p25', 'value_p25'), ('p50', 'value_p50'),
                    ('p75', 'value_p75'), ('p95', 'value_p95'),
                    ('max', 'value_max'),
                )
            }
            if any(value is not None for value in distribution.values()):
                # Quantiles cannot be combined correctly. Retain the row with
                # the most numeric values when duplicate paths share a unit.
                if current['distribution'] is None or values >= current.get('distribution_values', 0):
                    current['distribution'] = distribution
                    current['distribution_values'] = values

    result = {}
    for key, state in states.items():
        descriptions = state['descriptions']
        domains = state['domains']
        description = max(descriptions, key=lambda value: (descriptions[value], value)) \
            if descriptions else ''
        domain_id, omop_table = (
            max(domains, key=lambda value: (domains[value], value))
            if domains else ('', '')
        )
        units = []
        for (display, code), values in sorted(
                evidence.get(key, {}).items(),
                key=lambda item: (-item[1]['count'], item[0]),
        ):
            unit = {
                'display': display, 'code': code, 'count': values['count'],
                'source': evidence_source,
            }
            if values['patients']:
                unit['patients'] = values['patients']
            if values['values']:
                unit['values'] = values['values']
            if values['all_suppressed']:
                unit['suppressed'] = True
            if values['distribution'] is not None:
                unit['distribution'] = values['distribution']
            units.append(unit)
        result[key] = InventoryRow(
            source_vocabulary_id=key[0], source_code=key[1],
            source_code_description=description,
            occurrence_count=state['occurrence_count'],
            source_group_occurrence_count=state['source_group_occurrence_count'],
            source_metadata=_source_metadata(state),
            domain_id=domain_id, omop_table=omop_table,
            source_unit_evidence=units,
        )
    stats['total_keys'] = len(result)
    stats['keys_with_unit_evidence'] = len(evidence)
    stats['unit_evidence_rows'] = sum(len(value) for value in evidence.values())
    return BuildResult(result, stats)


def _chunks(values, size):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def upsert_inventory(build, *, provenance, actor=None, dry_run=False,
                     batch_size=2_000):
    """Insert new mappings and conservatively enrich existing mappings."""
    if not provenance or len(provenance) > 50:
        raise ValueError('provenance must contain 1 to 50 characters')
    rows = build.rows
    systems = {key[0] for key in rows}
    existing = []
    existing_keys = set()
    if systems:
        queryset = (
            SourceCodeConceptMapping.objects
            .filter(organization__isnull=True, source_vocabulary_id__in=systems)
            .only(
                'id', 'source_vocabulary_id', 'source_code',
                'source_code_description', 'domain_id', 'omop_table',
                'occurrence_count', 'source_unit_evidence', 'origin',
                'source_group_occurrence_count', 'source_metadata',
                'origin_system', 'updated_by',
            )
        )
        for mapping in queryset.iterator(chunk_size=batch_size):
            key = (mapping.source_vocabulary_id, mapping.source_code)
            if key in rows:
                existing.append(mapping)
                existing_keys.add(key)

    new_keys = rows.keys() - existing_keys
    outcome = {
        'total': len(rows), 'new': len(new_keys), 'existing': len(existing_keys),
        'updated': 0, 'dry_run': dry_run,
    }
    if dry_run:
        return outcome

    with transaction.atomic():
        for keys in _chunks(new_keys, batch_size):
            SourceCodeConceptMapping.objects.bulk_create([
                SourceCodeConceptMapping(
                    organization=None,
                    source_vocabulary_id=rows[key].source_vocabulary_id,
                    source_code=rows[key].source_code,
                    source_code_description=rows[key].source_code_description,
                    domain_id=rows[key].domain_id,
                    omop_table=rows[key].omop_table,
                    occurrence_count=rows[key].occurrence_count,
                    source_group_occurrence_count=rows[key].source_group_occurrence_count,
                    source_metadata=rows[key].source_metadata,
                    source_unit_evidence=rows[key].source_unit_evidence,
                    origin='import', origin_system=provenance,
                    source='HealthTree', status='proposed',
                    created_by=actor, updated_by=actor,
                )
                for key in keys
            ], batch_size=batch_size)

        updates = []
        for mapping in existing:
            imported = rows[(mapping.source_vocabulary_id, mapping.source_code)]
            changed = False
            if imported.occurrence_count > mapping.occurrence_count:
                mapping.occurrence_count = imported.occurrence_count
                changed = True
            if imported.source_unit_evidence != mapping.source_unit_evidence:
                mapping.source_unit_evidence = imported.source_unit_evidence
                changed = True
            if imported.source_group_occurrence_count != mapping.source_group_occurrence_count:
                mapping.source_group_occurrence_count = imported.source_group_occurrence_count
                changed = True
            if imported.source_metadata != mapping.source_metadata:
                mapping.source_metadata = imported.source_metadata
                changed = True
            may_refresh = mapping.origin == 'import' and mapping.origin_system == provenance
            if imported.source_code_description and (
                not mapping.source_code_description or may_refresh
            ) and imported.source_code_description != mapping.source_code_description:
                mapping.source_code_description = imported.source_code_description
                changed = True
            if imported.domain_id and not mapping.domain_id and mapping.origin == 'import':
                mapping.domain_id = imported.domain_id
                mapping.omop_table = imported.omop_table
                changed = True
            if changed:
                if actor is not None:
                    mapping.updated_by = actor
                mapping.updated_at = timezone.now()
                updates.append(mapping)

        for chunk in _chunks(updates, batch_size):
            SourceCodeConceptMapping.objects.bulk_update(
                chunk,
                [
                    'source_code_description', 'domain_id', 'omop_table',
                    'occurrence_count', 'source_unit_evidence', 'updated_by',
                    'source_group_occurrence_count', 'source_metadata',
                    'updated_at',
                ],
                batch_size=batch_size,
            )
        outcome['updated'] = len(updates)
    return outcome
