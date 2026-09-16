"""Bounded curation pages with 100 codes per section."""
from django.db.models import Case, CharField, Count, F, Q, Value, When, Window
from django.db.models.functions import Upper, Trim
from rest_framework.exceptions import ValidationError

from omop_core.services import source_vocabularies as vocab
from omop_core.services.mapping_destinations import with_destination_counts
from omop_core.services.source_retirement import mapping_source_retirement

OVERALL = '__overall__'
PAGE_SIZE = 100
ORDER_FIELDS = {
    'origin_system': 'origin_system', 'source_code': 'source_code',
    'occurrence_count': 'occurrence_count',
    'source_code_description': 'source_code_description',
    'destination_concept_name': 'target_concept__concept_name',
    'destination_concept_id': 'target_concept_id',
    'destination_count': 'destination_count', 'status': 'status',
}


def canonical_source(source):
    if source in vocab.WEARABLE_SOURCE_VOCABULARIES:
        return 'OpenWearables'
    return {**vocab.ICD10CM_MERGE, **vocab.VOCABULARY_OID_ALIASES}.get(source, source)


def browse_mappings(mappings, params, serialize):
    # Counts use the same Athena de-duplication as the visible rows.
    counts = {}
    section_counts = {}
    for group in mappings.order_by().values('source_vocabulary_id', 'status', 'origin_system').annotate(n=Count('pk')):
        source = canonical_source(group['source_vocabulary_id'])
        bucket = counts.setdefault(source, dict(proposed=0, approved=0, athena=0))
        totals = section_counts.setdefault(source, dict(unmapped=0, mapped=0, athena=0, rejected=0, athena_rejected=0))
        section = ('athena_rejected' if group['origin_system'] == 'athena' and group['status'] == 'rejected' else
                   'athena' if group['origin_system'] == 'athena' else
                   'mapped' if group['status'] == 'approved' else
                   'rejected' if group['status'] == 'rejected' else 'unmapped')
        totals[section] += group['n']
        key = 'athena' if group['origin_system'] == 'athena' else group['status']
        if key in bucket:
            bucket[key] += group['n']
    ordered = sorted(counts, key=lambda key: (vocab.source_tab_sort_key(key), key))
    tabs = [{
        'vocabulary_id': key, 'label': vocab.source_tab_label(key),
        'is_standard': key in vocab.STANDARD_SOURCE_VOCABULARIES, **counts[key],
    } for key in ordered if key in vocab.SOURCE_TAB_ORDER or counts[key]['proposed']]
    default = next((tab['vocabulary_id'] for tab in tabs if tab['proposed']),
                   next((tab['vocabulary_id'] for tab in tabs if tab['approved'] or tab['athena']),
                        tabs[0]['vocabulary_id'] if tabs else OVERALL))
    source = params.get('source', default)
    tabs.append(dict(vocabulary_id=OVERALL, label='Overall', is_standard=False,
                     **{key: sum(c[key] for c in counts.values()) for key in ('proposed', 'approved', 'athena')}))

    from omop_core.services.athena_mapping_guard import source_tab_vocabularies
    tab_rows = mappings if source == OVERALL else mappings.filter(source_vocabulary_id__in=source_tab_vocabularies(source))
    # Only duplicate groups need full rows. Window filtering preserves every
    # member, including rejected rows outside the current page/search.
    alias_cases = [When(source_vocabulary_id=key, then=Value(canonical_source(key)))
                   for key in set(vocab.ICD10CM_MERGE) | set(vocab.VOCABULARY_OID_ALIASES) | set(vocab.WEARABLE_SOURCE_VOCABULARIES)]
    duplicate_ids = tab_rows.order_by().annotate(
        canonical=Case(*alias_cases, default=F('source_vocabulary_id'), output_field=CharField()),
        code=Upper(Trim('source_code')),
    ).exclude(code='').annotate(
        duplicate_count=Window(Count('pk'), partition_by=[F('canonical'), F('code')]),
    ).filter(duplicate_count__gt=1).values_list('pk', flat=True)
    duplicates = list(with_destination_counts(mappings.filter(pk__in=duplicate_ids)))

    search = params.get('search', '').strip()
    filtered = mappings if search else tab_rows
    if search:
        query = Q()
        for field in ('source_code', 'source_vocabulary_id', 'source_code_description',
                      'target_concept__concept_name', 'target_concept__concept_code'):
            query |= Q(**{f'{field}__icontains': search})
        if search.isdigit():
            query |= Q(target_concept_id__icontains=search)
        filtered = filtered.filter(query)
    if search:
        totals = filtered.aggregate(
            unmapped=Count('pk', filter=~Q(origin_system='athena') & ~Q(status__in=['approved', 'rejected'])),
            mapped=Count('pk', filter=~Q(origin_system='athena') & Q(status='approved')),
            athena=Count('pk', filter=Q(origin_system='athena') & ~Q(status='rejected')),
            rejected=Count('pk', filter=~Q(origin_system='athena') & Q(status='rejected')),
            athena_rejected=Count('pk', filter=Q(origin_system='athena', status='rejected')),
        )
    else:
        selected_counts = section_counts.values() if source == OVERALL else [section_counts.get(canonical_source(source), {})]
        totals = {key: sum(bucket.get(key, 0) for bucket in selected_counts)
                  for key in ('unmapped', 'mapped', 'athena', 'rejected', 'athena_rejected')}
    rejected = totals['rejected']
    if params.get('show_rejected') != 'true':
        filtered = filtered.exclude(status='rejected')
    section_queries = {
        'Unmapped': filtered.exclude(origin_system='athena').exclude(status='approved'),
        'Mapped': filtered.exclude(origin_system='athena').filter(status='approved'),
        'Athena Mapped': filtered.filter(origin_system='athena'),
    }
    pages, selected_ids = {}, []
    section_totals = [totals['unmapped'] + (rejected if params.get('show_rejected') == 'true' else 0),
                      totals['mapped'], totals['athena'] + (totals['athena_rejected'] if params.get('show_rejected') == 'true' else 0)]
    for index, (section, query) in enumerate(section_queries.items()):
        try:
            requested_page = max(1, int(params.get(f'page_{index}', 1)))
        except (ValueError, TypeError):
            raise ValidationError({'page': 'Page must be an integer.'})
        order = params.get(f'order_{index}', '-occurrence_count')
        field = ORDER_FIELDS.get(order.lstrip('-'))
        if field is None:
            raise ValidationError({'order': 'Unknown sort column.'})
        total = section_totals[index]
        page = min(requested_page, max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE))
        # Correlated destination counts and retirement lookups run only on the
        # bounded page unless the curator explicitly sorts by destination count.
        if field == 'destination_count':
            query = with_destination_counts(query)
        ordering = F(field).desc(nulls_last=True) if order.startswith('-') else F(field).asc(nulls_last=True)
        ids = list(query.order_by(ordering, 'source_code', 'id').values_list('pk', flat=True)[(page - 1) * PAGE_SIZE:page * PAGE_SIZE])
        selected_ids.extend(ids)
        pages[section] = dict(page=page, page_size=PAGE_SIZE, total=total)
    # Load all three bounded sections together, preserving their selected order.
    page_rows = {row.pk: row for row in with_destination_counts(mappings.filter(pk__in=selected_ids))}
    results = [page_rows[pk] for pk in selected_ids if pk in page_rows]
    metadata = mapping_source_retirement(results + duplicates)

    def render(row):
        return serialize(row.target_concept, row, metadata[row.pk], destination_count=row.destination_count)

    return dict(results=[render(row) for row in results], duplicates=[render(row) for row in duplicates],
                tabs=tabs, selected_source=source, pages=pages, rejected_count=rejected)
