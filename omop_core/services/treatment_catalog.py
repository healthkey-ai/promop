"""Treatment choices copied from cancerbot's disease-specific value options.

The migration seeds TherapyOutcome; defaults also make newly provisioned/test
installations usable before the vocabulary data has been loaded.
"""
from django.db.models import Q

OUTCOMES = [
    ('CR', 'Complete Response', 'Complete Response (CR)'),
    ('sCR', 'Stringent Complete Response', 'Stringent Complete Response (sCR)'),
    ('VGPR', 'Very Good Partial Response', 'Very Good Partial Response (VGPR)'),
    ('PR', 'Partial Response', 'Partial Response (PR)'),
    ('MRD', 'Minimal Residual Disease Negativity', 'Minimal Residual Disease (MRD) Negativity'),
    ('SD', 'Stable Disease', 'Stable Disease (SD)'),
    ('PD', 'Progressive Disease', 'Progressive Disease (PD)'),
]
REFRACTORY_STATUSES = ['Unknown', 'Not Refractory', 'Primary Refractory', 'Secondary Refractory', 'Multi-Refractory']


def disease_code(value):
    text = (value or '').strip().lower()
    for keys, code in [
        (('c9335', 'bc', 'breast'), 'C9335'),
        (('c3242', 'mm', 'myeloma'), 'C3242'),
        (('c3209', 'fl', 'follicular'), 'C3209'),
        (('c2987', 'cll', 'chronic lymph'), 'C2987'),
        (('mcl', 'mantle'), 'MCL'),
        (('dlbcl', 'diffuse large'), 'DLBCL'),
    ]:
        if any(text == key or (len(key) > 3 and key in text) for key in keys):
            return code
    return value or ''


DISEASE_TITLES = {
    'C3242': 'Multiple Myeloma', 'C3209': 'Follicular Lymphoma',
    'C9335': 'Breast Cancer', 'C2987': 'Chronic Lymphocytic Leukemia',
    'MCL': 'Mantle Cell Lymphoma', 'DLBCL': 'Diffuse Large B-Cell Lymphoma',
}


def disease_filter(value, relation='disease'):
    """Match canonical codes and existing vocabulary rows kept under legacy codes."""
    code = disease_code(value)
    query = Q(**{relation + '__code__iexact': code})
    if code in DISEASE_TITLES:
        query |= Q(**{relation + '__title__iexact': DISEASE_TITLES[code]})
    return query


def outcomes_for_disease(value):
    from omop_core.models import TherapyOutcome
    code = disease_code(value)
    rows = TherapyOutcome.objects.filter(disease_filter(code, 'diseases') | Q(diseases__isnull=True)).distinct()
    if rows.exists():
        return [{'code': row.code, 'value': row.value, 'label': row.title} for row in rows]
    return [
        {'code': key, 'value': value, 'label': label}
        for key, value, label in OUTCOMES
        if code != 'C9335' or key in {'CR', 'PR', 'SD', 'PD'}
    ]
