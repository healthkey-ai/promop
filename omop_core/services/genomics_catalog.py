"""Versioned CancerBot priority catalog; no patient facts are created by reads."""
import json
from copy import deepcopy
from dataclasses import dataclass

from django.utils import timezone
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def catalog():
    data = json.loads((Path(__file__).resolve().parent.parent / 'data/genomics_catalog_v1.json').read_text())
    data['version'] = 2
    data['naming_decision'] = 'https://github.com/cancerbot-org/cancerbot/issues/4812#issuecomment-5633297037'
    marker = next(m for m in data['markers'] if m['key'] == 'palb1')
    marker.update(key='palb2', field_name='genomics_palb2', gene='PALB2', label='PALB2',
                  aliases=['PALB1'], legacy_keys=['palb1'], expert_review='')
    return data


def markers():
    return catalog()['markers']


def canonicalize_fields(values):
    """Resolve the retired field before routing writes; reject ambiguous lists."""
    values = values.copy()
    if 'genomics_palb1' in values:
        legacy = values.pop('genomics_palb1')
        if 'genomics_palb2' in values and values['genomics_palb2'] != legacy:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({'genomics_palb2': 'Do not send conflicting PALB1 and PALB2 lists.'})
        values['genomics_palb2'] = legacy
    return values


def patient_fields():
    return {m['field_name']: m for m in markers()}


def disease_code(value):
    key = str(value or '').strip().lower().replace('_', ' ').replace('-', ' ')
    aliases = {
        'bc': 'BC', 'breast': 'BC', 'breast cancer': 'BC',
        'mm': 'MM', 'myeloma': 'MM', 'multiple myeloma': 'MM',
        'fl': 'FL', 'lymphoma': 'FL', 'follicular lymphoma': 'FL',
        'mcl': 'MCL', 'mantle cell lymphoma': 'MCL',
        'cll': 'CLL', 'chronic lymphocytic leukemia': 'CLL',
        'chronic lymphocytic leukaemia': 'CLL',
    }
    return aliases.get(key)


def canonicalize_variant(variant):
    """Correct the reviewed code, retaining exact historical gene text on reads."""
    result = dict(variant)
    gene = result.get('gene')
    if isinstance(gene, str) and gene.strip().upper() == 'PALB1':
        result['source_gene'] = gene
        result['gene'] = 'PALB2'
    if result.get('marker_key') == 'palb1':
        result['marker_key'] = 'palb2'
    return result


def marker_sources():
    return {'genomics:' + key: m for m in markers()
            for key in [m['key'], *m.get('legacy_keys', [])]}


def marker_for_variant(variant):
    variant = canonicalize_variant(variant)
    assigned = variant.get('marker_key')
    if assigned:
        return next((m for m in markers() if m['key'] == assigned), None)
    gene = (variant.get('genomic_feature') or variant.get('gene') or '').upper()
    names = {str(variant.get(k) or '').lower().replace(' ', '') for k in ('variant', 'variant_name')}
    # Exact known abnormalities take precedence; do not infer a deletion from
    # a TP53 mutation, or split combined NOTCH/ATM source statements.
    for m in markers():
        aliases = [*m['aliases'], m['label']]
        if m['key'] in ('del17p', 'del11q', 'del13q', 'trisomy12'):
            aliases.append(m['key'])
        if names & {a.lower().replace(' ', '') for a in aliases if a} and m['kind'] == 'abnormality':
            return m
    if variant.get('feature_type') not in (None, '', 'Gene'):
        return None
    return next((m for m in markers() if m['gene'].upper() == gene and m['kind'] == 'gene'), None)


# Markers that can be either asserted (from a report) or derived (computed
# at projection time from other stored findings).
_DERIVABLE_MARKERS = frozenset({'complex_karyotype', 'complex_karyotype_excl_t1114'})


def _derive_complex_karyotype(variants):
    """Stub: complex karyotype threshold logic is an open clinical question.

    Returns None — the derived path is not yet implemented.
    """
    return None


def _derive_complex_karyotype_excl_t1114(variants):
    """Stub: same as _derive_complex_karyotype, excluding t(11;14).

    Returns None — the derived path is not yet implemented.
    """
    return None


_DERIVATION_FUNCTIONS = {
    'complex_karyotype': _derive_complex_karyotype,
    'complex_karyotype_excl_t1114': _derive_complex_karyotype_excl_t1114,
}


@dataclass(frozen=True)
class DerivedFinding:
    """Future derivations must identify their clinical rule version explicitly."""

    finding: dict
    derivation_version: str


def project_priority_variants(variants):
    # Inputs come from canonical parents/components, never previous derived
    # PatientRecord values. Ignore derived rows defensively at this boundary.
    asserted = [{**canonicalize_variant(v), 'provenance': 'asserted'} for v in variants
                if v.get('provenance') != 'derived']
    result = {name: [] for name in patient_fields()}
    for variant in asserted:
        marker = marker_for_variant(variant)
        if marker:
            result[marker['field_name']].append(dict(variant))
    for marker_key in _DERIVABLE_MARKERS:
        field = next(m['field_name'] for m in markers() if m['key'] == marker_key)
        if result[field]:
            continue  # An assertion of any status takes precedence.
        derived = _DERIVATION_FUNCTIONS[marker_key](deepcopy(asserted))
        if derived is None:
            continue
        if (not isinstance(derived, DerivedFinding)
                or not isinstance(derived.derivation_version, str)
                or not derived.derivation_version.strip()
                or not isinstance(derived.finding, dict)
                or 'id' in derived.finding):
            raise ValueError('Derived findings require a rule version and cannot claim a stored parent ID.')
        result[field].append({
            **derived.finding, 'marker_key': marker_key, 'provenance': 'derived',
            'derivation_version': derived.derivation_version,
            'derived_at': timezone.now().isoformat(),
        })
    return result
