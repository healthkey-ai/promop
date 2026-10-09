"""FLIPI-1: five equally weighted, explicitly assessed baseline factors."""
import re

FACTORS = {
    'age': 'Age > 60 years',
    'stage': 'Ann Arbor stage III or IV',
    'hemoglobin': 'Hemoglobin < 12 g/dL',
    'nodalAreas': 'More than 4 involved nodal areas',
    'ldh': 'LDH above the laboratory upper limit of normal',
}
_ALIASES = {
    'age ≥ 60': 'age', 'age > 60': 'age', 'stage iii/iv': 'stage',
    'hgb < 12 g/dl': 'hemoglobin', 'nodal areas > 4': 'nodalAreas', 'elevated ldh': 'ldh',
    **{label.lower(): key for key, label in FACTORS.items()},
    **{key.lower(): key for key in FACTORS},
}


def parse_factors(value):
    """None is unassessed; an explicit empty selection is a valid score of zero."""
    if value is None:
        return None
    values = value if isinstance(value, (list, tuple)) else re.split(r'[,;]', str(value))
    selected = set()
    for item in values:
        item = str(item).strip()
        if not item:
            continue
        key = _ALIASES.get(item.lower())
        if key is None:
            raise ValueError(f'Unrecognized FLIPI factor: {item}')
        selected.add(key)
    return [key for key in FACTORS if key in selected]


def calculate_flipi(value):
    selected = parse_factors(value)
    if selected is None:
        return None, None
    score = len(selected)
    return score, 'Low' if score <= 1 else 'Intermediate' if score == 2 else 'High'


def normalize_grade(value):
    if value in (None, ''):
        return None
    text = str(value).strip().upper()
    match = re.match(r'^(?:GRADE\s*)?([123](?:[AB])?)(?:\b|\s|\()', text)
    if not match or match.group(1) not in {'1', '2', '3', '3A', '3B'}:
        raise ValueError('Use grade 1, 2, 3A, or 3B (3 for an unspecified historical grade).')
    return match.group(1)
