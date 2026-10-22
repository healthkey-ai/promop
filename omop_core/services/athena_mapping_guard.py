"""Prevent redundant curator/suggestion pairs already supplied by Athena."""
from django.db.models import Q
from django.db.models.functions import Lower, Trim

from omop_core.models import SourceCodeConceptMapping
from omop_core.services import source_vocabularies

ATHENA_DUPLICATE_MESSAGE = 'This mapping is already supplied by Athena'


def source_tab_vocabularies(vocabulary_id):
    """The source systems represented by one curation tab."""
    if vocabulary_id in source_vocabularies.WEARABLE_SOURCE_VOCABULARIES:
        return source_vocabularies.WEARABLE_SOURCE_VOCABULARIES
    aliases = source_vocabularies.VOCABULARY_OID_ALIASES
    canonical = aliases.get(vocabulary_id, vocabulary_id)
    return {canonical, *(alias for alias, target in aliases.items() if target == canonical)}


def source_tab_filter(vocabulary_id):
    """A database predicate for every resolver identity shown on one tab."""
    if vocabulary_id == 'EPIC':
        return (
            Q(source_vocabulary_id='EPIC')
            | Q(source_vocabulary_id__istartswith='urn:oid:1.2.840.114350.')
            | Q(source_vocabulary_id__istartswith='http://open.epic.com/')
            | Q(source_vocabulary_id__istartswith='https://open.epic.com/')
        )
    if vocabulary_id == 'CERNER':
        return (
            Q(source_vocabulary_id='CERNER')
            | (
                Q(source_vocabulary_id__iregex=r'^https?://fhir\.cerner\.com/')
                & Q(source_vocabulary_id__icontains='/codeset/')
            )
        )
    return Q(source_vocabulary_id__in=source_tab_vocabularies(vocabulary_id))


def athena_supplies_mapping(vocabulary_id, source_code, destination_id, *, exclude_id=None):
    icd10cm = vocabulary_id == 'ICD10CM'
    if (not icd10cm and not destination_id) or not source_code.strip():
        return False
    mappings = SourceCodeConceptMapping.objects.filter(
        source_tab_filter(vocabulary_id),
        origin_system__startswith='athena', target_concept_id__isnull=False,
    ).alias(trimmed_source_code=Trim('source_code')).filter(
        trimmed_source_code__iexact=source_code.strip(),
    )
    if not icd10cm:
        mappings = mappings.filter(target_concept_id=destination_id)
    if exclude_id is not None:
        mappings = mappings.exclude(pk=exclude_id)
    return mappings.exists()


def without_icd10_athena_duplicates(mappings):
    """Keep Athena-owned ICD-10-CM codes out of the curator work sections.

    Consult the complete table before search/status filtering, so hiding an
    Athena result cannot expose a redundant curator row.
    """
    athena = SourceCodeConceptMapping.objects.using(mappings.db).filter(
        source_vocabulary_id='ICD10CM',
        origin_system__startswith='athena', target_concept_id__isnull=False,
    ).annotate(normalized_code=Lower(Trim('source_code'))).exclude(
        normalized_code='',
    ).order_by().values('normalized_code')
    return mappings.alias(normalized_code=Lower(Trim('source_code'))).exclude(
        Q(source_vocabulary_id='ICD10CM')
        & ~Q(origin_system__startswith='athena') & Q(normalized_code__in=athena)
    )
