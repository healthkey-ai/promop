"""Concept-first curation over each installation's vocabulary and SCCM."""
from django.contrib.postgres.fields import ArrayField
from django.db.models import (
    Case, CharField, Count, Exists, F, Func, IntegerField, OuterRef, Q, Subquery, Sum, TextField, Value, When,
)
from django.db.models.functions import Cast, Coalesce, Lower, NullIf, Trim
from django.utils import timezone

from omop_core.mapping import axis_variants
from omop_core.models import (
    Concept, ConceptMappingDone, FieldConceptMapping, LoincCodeClass, SourceCodeConceptMapping,
)
from omop_core.services.source_vocabularies import (
    DOMAIN_TO_TABLE, WEARABLE_METRIC_CATEGORIES, WEARABLE_SOURCE_VOCABULARIES,
)

# A choice in the tab's domain list that is not an OMOP domain: the standard
# concepts a wearable source code is mapped to (#2656). The codes of one metric
# (HealthKit, Health Connect, OpenWearables, Garmin, HL7 PHR) should converge
# on one concept, and this is where a curator sees whether they do.
WEARABLES_DOMAIN = 'wearables'

# The list is grouped by category only while it is short enough to sort and
# count by a computed value in one request: "All standard concepts" with no
# domain is millions of rows, and a category is a subquery, not a column.
CONCEPT_CATEGORY_GROUP_LIMIT = 5000
# What `category=` sends for the concepts that have no category.
NO_CATEGORY = '__none__'
CONCEPT_ORDERS = ('category', 'priority')


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
        'confidence': row.suggestion_confidence,
        'decision_confidence': row.decision_confidence, 'decision_surface': row.decision_surface,
        'source_label_norm': row.source_label_norm,
        # The note when a curator wrote it; Suggest's rationale is not one (#2423).
        'curator_note': row.notes.strip() if row.curator_note_at else '',
        'units': [e.get('display') or e.get('unit', '') for e in (row.source_unit_evidence or []) if isinstance(e, dict)],
        'based_on': getattr(row, '_source_samples', {}).get('based_on', []),
        # Its units imply this many LOINC axis sets (the blue "n axis values").
        'axis_variant_count': row.axis_variant_count,
        # ... and these are the variants, each with its own destination (#2012).
        **({'axis_variants': [axis_variants.serialize(v) for v in row.axis_variants.all()]}
           if row.axis_variant_count >= axis_variants.FLAG_THRESHOLD else {}),
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
    wearables = params.get('domain') == WEARABLES_DOMAIN
    if params.get('domain') and not wearables:
        fields = fields.filter(omop_table=params['domain'])
    disease = (params.get('disease') or '').strip()
    if disease:
        from omop_core.services.disease_relevance import _disease_fields
        fields = fields.filter(field_name__in=_disease_fields(disease))
    concepts = standard_destinations()
    scope = 'fields' if disease else params.get('scope', 'fields')
    if wearables:
        # Every concept a wearable code maps to, unless "Patient field
        # concepts" is asked for by name (#2808): few of these concepts back a
        # patient field, so that scope is not the default here as it is under
        # the other domains, and a request that names none gets them all. A
        # disease narrows to its fields' concepts, as under every domain. A
        # rejected mapping is a code that is not mapped to the concept.
        concepts = concepts.filter(Exists(SourceCodeConceptMapping.objects.filter(
            target_concept_id=OuterRef('pk'), source_vocabulary_id__in=WEARABLE_SOURCE_VOCABULARIES,
        ).exclude(status='rejected')))
        if disease or params.get('scope') == 'fields':
            concepts = concepts.filter(Exists(fields.filter(concept_id=OuterRef('pk'))))
    elif scope == 'fields':
        concepts = concepts.filter(Exists(fields.filter(concept_id=OuterRef('pk'))))
    elif params.get('domain'):
        domains = [domain for domain, table in DOMAIN_TO_TABLE.items() if table == params['domain']]
        concepts = concepts.filter(domain_id__in=domains)
    if str(params.get('prioritized') or '') in ('1', 'true'):
        concepts = concepts.filter(mapping_priority__isnull=False)
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

    # `category` is selected with the page and read by the order, the filter
    # and the summary; a count that does not filter on it never computes it.
    concepts = concepts.annotate(
        category=concept_category(params),
        priority=F('mapping_priority__position'),
        done=Exists(ConceptMappingDone.objects.filter(concept_id=OuterRef('pk'))),
    )
    category = params.get('category')
    if category:
        # An unknown value matches nothing, like any other filter; one the
        # database cannot be asked about (a NUL) is unknown too.
        concepts = (concepts.none() if '\x00' in category
                    else concepts.filter(category='' if category == NO_CATEGORY else category))
    return concepts, fields, {
        'approved': count('approved'), 'proposed': count('proposed'), 'rejected': count('rejected'),
        'seen': Coalesce(Subquery(mappings.order_by().values('target_concept_id').annotate(
            n=Sum('occurrence_count')).values('n')), Value(0)),
    }


def _wearable_rank(column):
    """A wearable metric category's place in the curated list; null for any other value.

    `array_position` reads `column` once. A CASE with one WHEN per category
    would repeat it in each, and where `column` is the list's `category`
    annotation that is a correlated subquery, run again for every WHEN.
    """
    return Func(
        Cast(Value(list(WEARABLE_METRIC_CATEGORIES)), ArrayField(TextField())), Cast(F(column), TextField()),
        function='array_position', output_field=IntegerField(),
    )


def concept_category(params):
    """The expression for a concept's category: a string, '' when it has none.

    The one definition: the list's order, each row's value, the per-category
    counts and the `category=` filter all read the `category` annotation
    `browse_concepts` makes from it.

    - Under the Wearables domain, the metric category of the wearable codes
      mapped to the concept: the most frequent non-blank
      `SourceCodeConceptMapping.category` among its non-rejected mappings in
      the wearable vocabularies, ties going to the earlier entry of
      `WEARABLE_METRIC_CATEGORIES`. Correlated on `target_concept_id`, which
      is indexed.
    - Elsewhere, a LOINC concept's LOINC class by display name (the class code
      when the name is blank), read by primary key; a LOINC code with no
      `LoincCodeClass` row, and every other concept, has its concept class.
    """
    if params.get('domain') == WEARABLES_DOMAIN:
        categories = SourceCodeConceptMapping.objects.filter(
            target_concept_id=OuterRef('pk'), source_vocabulary_id__in=WEARABLE_SOURCE_VOCABULARIES,
        ).exclude(status='rejected').exclude(category='').order_by().values('category').annotate(
            n=Count('pk'),
        ).order_by('-n', _wearable_rank('category').asc(nulls_last=True), 'category').values('category')[:1]
        return Coalesce(Subquery(categories, output_field=CharField()), Value(''), output_field=CharField())
    loinc_class = LoincCodeClass.objects.filter(loinc_num=OuterRef('concept_code')).values(
        label=Coalesce(NullIf(Trim('loinc_class__display_name'), Value('')), F('loinc_class_id'),
                       output_field=CharField()),
    )[:1]
    return Case(
        When(vocabulary_id='LOINC', then=Coalesce(
            Subquery(loinc_class, output_field=CharField()), F('concept_class_id'), output_field=CharField())),
        default=F('concept_class_id'), output_field=CharField(),
    )


def category_ordering(params):
    """Order-by terms that put the `category` annotation in display order.

    Wearables follows `WEARABLE_METRIC_CATEGORIES`; every other domain is
    alphabetical without regard to case. No category comes last in both.
    Each term reads the annotation once: it is a subquery per row.
    """
    named = NullIf('category', Value(''))
    if params.get('domain') == WEARABLES_DOMAIN:
        return [_wearable_rank('category').asc(nulls_last=True), named.asc(nulls_last=True)]
    return [NullIf(Lower('category'), Value('')).asc(nulls_last=True), named.asc(nulls_last=True)]


def order_concepts(concepts, params, order):
    """The list in `order`: 'priority' is the global ranked list, 'category' the
    same order inside each category.

    Curator priority first (#1914), on every page and under every filter;
    unranked concepts keep the alphabetical order after them. Done concepts
    (#2388) come after every other: finished, not forgotten.
    """
    by_priority = [F('priority').asc(nulls_last=True), 'done', 'concept_name', 'pk']
    if order == 'category':
        return concepts.order_by(*category_ordering(params), *by_priority)
    return concepts.order_by(*by_priority)


def category_summary(concepts, params):
    """[{category, count}] for the whole filtered list, in display order: one query."""
    return list(concepts.order_by().values('category').annotate(count=Count('pk')).order_by(
        *category_ordering(params)))
