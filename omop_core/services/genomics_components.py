"""Effective runtime component registry; historical seed inputs stay frozen."""
from omop_core.services.genomics_catalog import catalog


# Version 2 additions mirror 0226/0227. Do not import this evolving registry
# from historical migrations. A later version may change portable recipes
# only through an explicit migration/curation process.
_V2_COMPONENTS = (
    ('status', '69548-6', 'measurement', 'string'),
    ('clone_fraction', '', 'measurement', 'number'),
    ('transcript_dna_change', '48004-6', 'measurement', 'string'),
    ('coverage_depth', '82121-5', 'observation', 'number'),
    ('amino_acid_change_type', '48006-1', 'measurement', 'string'),
)
_FEATURE_COMPONENTS = (
    ('genomic_feature', '', 'observation', 'string'),
    ('feature_type', '', 'observation', 'string'),
    ('finding_category', '', 'observation', 'string'),
)
_DOMAIN_CORRECTIONS = {
    'genomic_dna_change': 'observation',
    'variant_analysis_method_type': 'observation',
    'variant_category': 'observation',
}


def components():
    """All supported nested fields for writing, discovery and curation.

    ``code`` is the compatible fact source identifier; a portable vocabulary
    code may differ. The persisted approved mapping still governs writes.
    Runtime domain corrections do not overwrite a curator's stored recipe.
    Return copies so consumers cannot mutate the frozen migration catalog.
    """
    attributes = [
        {**a, 'table': _DOMAIN_CORRECTIONS.get(a['key'], a['table'])}
        for a in catalog()['attributes']
    ]
    attributes.extend({
        'key': key, 'code': 'genomics:' + key, 'table': table,
        'value_kind': kind, 'vocabulary_id': 'LOINC' if loinc else '',
        'concept_code': loinc,
    } for key, loinc, table, kind in _V2_COMPONENTS + _FEATURE_COMPONENTS)
    for attribute in attributes:
        if attribute['key'] == 'variant_name':
            attribute.update(vocabulary_id='LOINC', concept_code='81253-7', recipe_version=3)
    return attributes


def component_codes():
    """Compatible source keys and portable LOINC aliases for stored facts."""
    codes = {}
    for attribute in components():
        for code in (attribute['code'], attribute.get('concept_code')):
            if code:
                codes[code] = attribute['key']
    return codes
