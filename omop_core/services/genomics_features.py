"""Feature terminology for findings; original gene and report text stay intact.

Only exact catalog identities supply an abnormality classification. A gene
name alone never establishes a sequence variant, deletion or rearrangement.
"""
import re

FEATURE_TYPES = ('Gene', 'Chromosome(s)', 'Chromosome arm/region', 'Rearrangement partners')
FINDING_CATEGORIES = (
    'Sequence variant', 'Deletion', 'Gain', 'Translocation', 'Aneuploidy',
    'Ploidy abnormality', 'Complex structural rearrangement',
)
_REGIONS = {'del17p': '17p', 'del11q': '11q', 'del13q': '13q', 'gain1q': '1q21'}


def marker_features(marker):
    key = marker['key']
    feature = marker['gene']
    kind = 'Gene'
    category = ''
    if key in _REGIONS:
        feature, kind = _REGIONS[key], 'Chromosome arm/region'
        category = 'Gain' if key == 'gain1q' else 'Deletion'
    elif key == 'trisomy12':
        feature, kind, category = 'Chromosome 12', 'Chromosome(s)', 'Aneuploidy'
    elif key in ('t414', 't1114', 't1416'):
        kind, category = 'Rearrangement partners', 'Translocation'
    elif feature == 'Chromosomal':
        feature, kind = 'Chromosomes', 'Chromosome(s)'
        category = {'hyperdiploidy': 'Ploidy abnormality',
                    'chromothripsis': 'Complex structural rearrangement'}.get(key, '')
    elif key == 'bcl2_amplification':
        category = 'Gain'
    return {'genomic_feature': feature, 'feature_type': kind, 'finding_category': category}


def describe_finding(finding):
    from omop_core.services.genomics_catalog import marker_for_variant

    result = dict(finding)
    marker = marker_for_variant(finding)
    defaults = marker_features(marker) if marker else {}
    gene = str(finding.get('gene') or '').strip()
    if not defaults and gene:
        if re.fullmatch(r'(?:[1-9]|1[0-9]|2[0-2]|X|Y)[pq][0-9.]*', gene, re.I):
            defaults = {'genomic_feature': gene[0].upper() + gene[1:].lower(),
                        'feature_type': 'Chromosome arm/region'}
        elif re.fullmatch(r'(?:[1-9]|1[0-9]|2[0-2]|X|Y)', gene, re.I):
            defaults = {'genomic_feature': 'Chromosome ' + gene.upper(), 'feature_type': 'Chromosome(s)'}
        elif gene.lower() == 'chromosomal':
            defaults = {'genomic_feature': 'Chromosomes', 'feature_type': 'Chromosome(s)'}
        else:
            defaults = {'genomic_feature': gene.upper(), 'feature_type': 'Gene'}
    # Explicit component values take precedence over catalog fallbacks.
    for key, value in defaults.items():
        if value:
            result.setdefault(key, value)
    if 'finding_category' not in result and finding.get('variant_category') == 'Simple variant':
        result['finding_category'] = 'Sequence variant'
    return result
