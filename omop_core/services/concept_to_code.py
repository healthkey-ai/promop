"""Concept-first curation over each installation's vocabulary and SCCM."""
from django.db.models import Count, Exists, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from omop_core.models import Concept, FieldConceptMapping, SourceCodeConceptMapping
from omop_core.services.source_vocabularies import DOMAIN_TO_TABLE


def standard_destinations():
    today = timezone.now().date()
    return Concept.objects.filter(
        Q(invalid_reason__isnull=True) | Q(invalid_reason=''),
        standard_concept='S', valid_start_date__lte=today, valid_end_date__gte=today,
        domain_id__in=DOMAIN_TO_TABLE,
    )


def concept_payload(concept):
    return {key: getattr(concept, key) for key in (
        'concept_id', 'concept_name', 'concept_code', 'vocabulary_id',
        'domain_id', 'concept_class_id', 'standard_concept',
    )}


def source_payload(row):
    # The exact revision is a precondition for a write, including changes to
    # status/description which a destination-only comparison would miss.
    return {
        'mapping_id': row.pk, 'source_code': row.source_code,
        'source_vocabulary_id': row.source_vocabulary_id,
        'source_code_description': row.source_code_description,
        'occurrence_count': row.occurrence_count, 'status': row.status,
        'origin_system': row.origin_system, 'updated_at': row.updated_at.isoformat(),
        'destination_concept_id': row.target_concept_id,
        'destination_concept_name': row.target_concept.concept_name if row.target_concept else '',
        'destination_standard_concept': row.target_concept.standard_concept if row.target_concept else None,
        'domain_id': row.domain_id, 'omop_table': row.omop_table,
    }


def eligible_sources(concept):
    """Undecided sources, including imports pointing at nonstandard concepts.

    Retrieval never edits these rows. Approved/rejected decisions and Athena
    reference mappings require the full editor; they cannot be batch replaced.
    Unknown source domains are eligible, known incompatible domains are not.
    """
    return SourceCodeConceptMapping.objects.filter(status='proposed').exclude(
        origin_system='athena',
    ).filter(Q(domain_id='') | Q(domain_id=concept.domain_id)).filter(
        Q(omop_table='') | Q(omop_table=DOMAIN_TO_TABLE[concept.domain_id]),
    ).exclude(target_concept_id=concept.pk)


def browse_concepts(params):
    fields = FieldConceptMapping.objects.exclude(status='rejected')
    if params.get('status'):
        fields = fields.filter(status=params['status'])
    if params.get('domain'):
        fields = fields.filter(omop_table=params['domain'])
    concepts = standard_destinations()
    scope = params.get('scope', 'fields')
    if scope == 'fields':
        concepts = concepts.filter(Exists(fields.filter(concept_id=OuterRef('pk'))))
    elif params.get('domain'):
        domains = [domain for domain, table in DOMAIN_TO_TABLE.items() if table == params['domain']]
        concepts = concepts.filter(domain_id__in=domains)
    search = str(params.get('search') or '').strip()
    if search:
        matching_fields = fields.filter(field_name__icontains=search)
        query = (Q(concept_name__icontains=search) | Q(concept_code__icontains=search)
                 | Q(pk__in=matching_fields.values('concept_id')))
        if search.isdigit():
            query |= Q(pk=int(search))
        concepts = concepts.filter(query)

    # Separate correlated aggregates avoid multiplying SCCM counts when several
    # patient fields share the same destination. Evaluate only the page below.
    mappings = SourceCodeConceptMapping.objects.filter(target_concept_id=OuterRef('pk'))
    def count(status):
        return Coalesce(Subquery(mappings.filter(status=status).order_by().values(
            'target_concept_id').annotate(n=Count('pk')).values('n')), Value(0))

    return concepts.order_by('concept_name', 'pk'), fields, {
        'approved': count('approved'), 'proposed': count('proposed'), 'rejected': count('rejected'),
        'seen': Coalesce(Subquery(mappings.order_by().values('target_concept_id').annotate(
            n=Sum('occurrence_count')).values('n')), Value(0)),
    }
