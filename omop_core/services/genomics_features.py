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
# Structural findings that mention a gene are still not a gene-level variant:
# a trial that requires a BRCA1 mutation must not be satisfied by a deletion.
_HGVS = re.compile(r'\b[cgnmrp]\.(?=[A-Za-z0-9*_>-])', re.I)
_STRUCTURAL_TEXT = re.compile(
    r'copy[\s-]*number\s+(?:gain|loss)'
    r'|\bdeletion\b|amplification|rearrangement|translocation|fusion|trisomy|monosomy'
    r'|del\(|t\(|\+[0-9]|-[0-9]'
    r'|\b(?:1q|17p|11q|13q)\s+(?:gain|loss)'
    # A symbol followed by gain/loss is a copy-number statement, but
    # "loss of function" describes a sequence variant.
    r'|\b[A-Z0-9][A-Z0-9-]{1,15}\s+(?:gain|loss)\b(?!\s+of\s+(?:function|activity))',
    re.I,
)


def is_gene_level(finding, marker=None):
    """True when the finding's subject is a gene-level sequence variant.

    Explicit feature type and category win: a chromosome arm, a rearrangement
    partner or a copy-number finding is a distinct finding even when its text
    names a gene. Findings that predate those fields stay gene-level, except
    when the variant text is unmistakably cytogenetic.
    """
    feature_type = str(finding.get('feature_type') or '').strip()
    category = str(finding.get('finding_category') or '').strip()
    if feature_type and feature_type != 'Gene':
        return False
    if category and category != 'Sequence variant':
        return False
    if marker is not None and marker.get('kind') != 'gene':
        return False
    if category == 'Sequence variant':
        # An explicit category wins over the text heuristic, as a structural
        # one does above: an in-frame "exon 19 deletion" is a sequence variant
        # when the source says so (#2489).
        return True
    text = ' '.join(str(finding.get(key) or '') for key in ('variant', 'variant_name', 'mutation'))
    if _HGVS.search(text):
        # HGVS spelling names a sequence variant even when the description also
        # contains words such as "deletion" or a splice-site +1/-2 offset.
        return True
    return not _STRUCTURAL_TEXT.search(text)


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
    from omop_core.services.genomics_catalog import gene_concept_id, marker_for_variant

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
    # Gene identity is the OMOP Genomic concept whose concept_code is the HGNC
    # id (#5673). Abnormalities keep it null: a deletion or translocation is a
    # distinct finding, and matching must never satisfy a gene requirement from
    # it alone. An unknown gene is null, never its display symbol.
    if not is_gene_level(finding, marker):
        result['gene_concept_id'] = None
    elif marker and marker.get('kind') == 'gene':
        result['gene_concept_id'] = marker.get('omop_concept_id')
    else:
        result['gene_concept_id'] = gene_concept_id(gene)
    return result
