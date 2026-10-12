"""Confirmed wrong-code recipes from #1227; these are not clinical aliases."""

from django.db.models import Q


WRONG_FIELD_LOINCS = {
    'ki67_proliferation_index': ('85319-2', '85337-4'),
    'test_methodology': ('85337-4',),
    'oncotype_dx_score': ('85337-4',),
    'lymph_node_status': ('92837-4',),
}


def wrong_breast_field_mappings():
    """Match resolved concepts as well as stale recipe metadata/source aliases."""
    predicate = Q(pk__in=[])
    for field, codes in WRONG_FIELD_LOINCS.items():
        predicate |= Q(field_name=field) & (
            Q(concept__vocabulary_id='LOINC', concept__concept_code__in=codes)
            | Q(vocabulary_id='LOINC', concept_code__in=codes)
            | Q(source_value__in=codes)
        )
    return predicate
