"""Build and import the HealthTree Epic/Cerner source-code inventory.

The export grain includes resource paths and, in the supplemental CSV, units.
SCCM's grain is the exact vendor system plus its code, so this module performs
the collapse explicitly and never reduces an Epic/Cerner URI to the vendor tab
name.  That URI is the only hospital/tenant boundary present in these files.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

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
        state = states.setdefault(key, {
            'occurrence_count': 0, 'descriptions': Counter(),
            'domains': Counter(), 'primary': True,
        })
        state['occurrence_count'] += count
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
        state = supplement.setdefault(key, {
            'occurrence_count': 0, 'descriptions': Counter(),
            'domains': Counter(), 'primary': False,
        })
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
        evidence.setdefault(key, Counter())[unit_key] += _count(raw.get('n_records'))

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
        units = [
            {
                'display': display,
                'code': code,
                'count': count,
                'source': evidence_source,
            }
            for (display, code), count in sorted(
                evidence.get(key, {}).items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]
        result[key] = InventoryRow(
            source_vocabulary_id=key[0], source_code=key[1],
            source_code_description=description,
            occurrence_count=state['occurrence_count'],
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
                    'updated_at',
                ],
                batch_size=batch_size,
            )
        outcome['updated'] = len(updates)
    return outcome
