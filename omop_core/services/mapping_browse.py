"""Bounded curation pages with 100 codes per section."""
from django.db.models import Case, CharField, Count, F, Q, Value, When, Window
from django.db.models.functions import Upper, Trim
from rest_framework.exceptions import ValidationError

from omop_core.services import source_vocabularies as vocab
from omop_core.services.mapping_destinations import with_destination_counts
from omop_core.services.mapping_rollup import count_groups, group_entries
from omop_core.services.source_retirement import mapping_source_retirement

OVERALL = '__overall__'

def _is_athena(origin_system):
    """True for resolved Athena origins (athena, athena-local-maps-to, athena-standard-self).

    ``athena-multiple`` is excluded: it means Athena found several candidates
    and a curator has not chosen yet, so it belongs in Unmapped.
    """
    return (bool(origin_system)
            and origin_system.startswith('athena')
            and origin_system != 'athena-multiple')


# ORM filter for resolved athena-family origins.
_Q_ATHENA = Q(origin_system__startswith='athena') & ~Q(origin_system='athena-multiple')
# A blank origin_system is a real value -- enqueue_unmapped_source_codes writes
# it for every newly queued code -- but '' already means "no filter" on the
# wire, so filtering to it needs a sentinel.
BLANK_PROVENANCE = '__blank__'
GLOBAL_ORGANIZATION = '__none__'
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
    vendor = vocab.hospital_vendor(source)
    if vendor:
        return vendor
    if source in vocab.WEARABLE_SOURCE_VOCABULARIES:
        return 'OpenWearables'
    return vocab.VOCABULARY_OID_ALIASES.get(source, source)


#: How each section carves the tab. Shared with the group-members endpoint so
#: expanding an entry shows the rows the entry was actually counted from -- a
#: label whose codes are split across Unmapped and Mapped is two entries, and
#: expanding either must not show the other's rows.
SECTION_FILTERS = {
    'Unmapped': lambda qs: qs.exclude(_Q_ATHENA).exclude(status__in=['approved', 'rejected']),
    'Mapped': lambda qs: qs.exclude(_Q_ATHENA).filter(status='approved'),
    'Rejected': lambda qs: qs.exclude(_Q_ATHENA).filter(status='rejected'),
    'Athena Mapped': lambda qs: qs.filter(_Q_ATHENA),
}


def tab_queryset(mappings, source):
    """The rows on one source tab, or every row on the Overall tab."""
    from omop_core.services.athena_mapping_guard import source_tab_vocabularies
    if source is None or source == OVERALL:
        return mappings
    from omop_core.services.athena_mapping_guard import source_tab_filter
    return mappings.filter(source_tab_filter(source))


def apply_search(mappings, tab_rows, search):
    """A search deliberately reaches across every tab, not just the active one."""
    if not search:
        return tab_rows
    query = Q()
    for field in ('source_code', 'source_vocabulary_id', 'source_code_description',
                  'target_concept__concept_name', 'target_concept__concept_code',
                  'organization__name', 'organization__slug'):
        query |= Q(**{f'{field}__icontains': search})
    if search.isdigit():
        query |= Q(target_concept_id__icontains=search)
    return mappings.filter(query)


def apply_provenance(queryset, provenance):
    if not provenance:
        return queryset
    return queryset.filter(
        origin_system='' if provenance == BLANK_PROVENANCE else provenance)


def apply_organization(queryset, organization):
    if not organization:
        return queryset
    if organization == GLOBAL_ORGANIZATION:
        return queryset.filter(organization__isnull=True)
    return queryset.filter(organization__slug=organization)


def _seen_only(params):
    """Parse and validate the seen_only parameter."""
    value = params.get('seen_only', '0')
    if value not in ('0', '1'):
        raise ValidationError({'seen_only': 'Use 1 for Seen > 0 or 0 for all codes.'})
    return value == '1'


def apply_seen(queryset, params):
    """The page opts into Seen > 0; other API clients retain their full catalog."""
    return queryset.filter(occurrence_count__gt=0) if _seen_only(params) else queryset


def _apply_seen_for_section(queryset, params, section):
    """Seen > 0 applies only to Unmapped; other sections show all codes."""
    if section == 'Unmapped' and _seen_only(params):
        return queryset.filter(occurrence_count__gt=0)
    return queryset


def visible_rows(mappings, params, section=None):
    """Everything a browse request narrows by, in the order browse applies it.

    The group-members endpoint has to reproduce this exactly: a rollup entry's
    members/seen/proposed counts describe the *filtered* set, so expanding an
    entry that says "3 codes" under a provenance filter must not open into all
    2,557 -- including the rows the filter existed to hide.

    *section* narrows the seen filter: when set, Seen > 0 applies only to
    Unmapped.  When None the filter applies to everything, preserving the
    contract for callers that have no section context.
    """
    source = params.get('source')
    search = (params.get('search') or '').strip()
    rows = apply_search(mappings, tab_queryset(mappings, source), search)
    rows = apply_provenance(rows, (params.get('provenance') or '').strip())
    rows = apply_organization(rows, (params.get('organization') or '').strip())
    if section is not None:
        return _apply_seen_for_section(rows, params, section)
    return apply_seen(rows, params)


def _provenance_options(counts_by_origin):
    """Filter options, most common first, ties broken by name."""
    return [{'origin_system': origin, 'count': n}
            for origin, n in sorted(counts_by_origin.items(), key=lambda kv: (-kv[1], kv[0]))]


def browse_mappings(mappings, params, serialize):
    catalog = mappings
    seen_only = _seen_only(params)
    # seen_only narrows only the Unmapped section; other sections show all codes.
    # Tab counts use the full queryset so the tab strip reflects total work.
    counts = {}
    section_counts = {}
    # Provenance counts ride on this aggregate rather than a second GROUP BY:
    # origin_system carries no index, so a query of its own is a sequential
    # scan of the tab on every browse and on every refresh after an approve.
    provenance_counts = {}
    organization_counts = {}
    for group in mappings.order_by().values(
        'source_vocabulary_id', 'status', 'origin_system',
        'organization_id', 'organization__slug', 'organization__name',
    ).annotate(n=Count('pk')):
        source = canonical_source(group['source_vocabulary_id'])
        bucket = counts.setdefault(source, dict(proposed=0, approved=0, athena=0))
        totals = section_counts.setdefault(source, dict(unmapped=0, mapped=0, athena=0, rejected=0, athena_rejected=0))
        is_ath = _is_athena(group['origin_system'])
        section = ('athena_rejected' if is_ath and group['status'] == 'rejected' else
                   'athena' if is_ath else
                   'mapped' if group['status'] == 'approved' else
                   'rejected' if group['status'] == 'rejected' else 'unmapped')
        totals[section] += group['n']
        key = 'athena' if is_ath else group['status']
        if key in bucket:
            bucket[key] += group['n']
        # Athena rows are excluded: they are reference, they sit in a section
        # that starts collapsed, and on the ICD-10 tab they outnumber
        # everything else -- offering them would read as emptying the page.
        if not is_ath:
            per_source = provenance_counts.setdefault(source, {})
            per_source[group['origin_system']] = per_source.get(group['origin_system'], 0) + group['n']
        organization_key = group['organization__slug'] or GLOBAL_ORGANIZATION
        per_source_org = organization_counts.setdefault(source, {})
        current = per_source_org.setdefault(organization_key, {
            'organization_id': group['organization_id'],
            'slug': group['organization__slug'] or '',
            'name': group['organization__name'] or 'Global / unattributed',
            'count': 0,
        })
        current['count'] += group['n']
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

    tab_rows = tab_queryset(mappings, source)
    # Only duplicate groups need full rows. Window filtering preserves every
    # member, including rejected rows outside the current page/search.
    # A duplicate is one code twice in one *vocabulary*, not twice on one tab:
    # the Wearables tab holds Apple, Garmin and OpenWearables, ingest resolves
    # on the exact vocabulary, and the same code in two of them is two
    # legitimate mappings. Only an OID spelling is the same vocabulary.
    alias_cases = [When(source_vocabulary_id=key, then=Value(canonical))
                   for key, canonical in vocab.VOCABULARY_OID_ALIASES.items()]
    duplicate_ids = tab_queryset(catalog, source).order_by().annotate(
        canonical=Case(*alias_cases, default=F('source_vocabulary_id'), output_field=CharField()),
        code=Upper(Trim('source_code')),
    ).exclude(code='').annotate(
        duplicate_count=Window(
            Count('pk'),
            partition_by=[F('organization_id'), F('canonical'), F('code')],
        ),
    ).filter(duplicate_count__gt=1).values_list('pk', flat=True)
    duplicates = list(with_destination_counts(catalog.filter(pk__in=duplicate_ids)))

    # Values offered by the filter control, from the counts above so choosing
    # one does not empty the list of the others.
    if source == OVERALL:
        totals_by_provenance = {}
        for per_source in provenance_counts.values():
            for origin, n in per_source.items():
                totals_by_provenance[origin] = totals_by_provenance.get(origin, 0) + n
    else:
        totals_by_provenance = provenance_counts.get(canonical_source(source), {})
    provenances = _provenance_options(totals_by_provenance)
    if source == OVERALL:
        totals_by_organization = {}
        for per_source in organization_counts.values():
            for key, option in per_source.items():
                current = totals_by_organization.setdefault(key, {**option, 'count': 0})
                current['count'] += option['count']
    else:
        totals_by_organization = organization_counts.get(canonical_source(source), {})
    organizations = sorted(
        totals_by_organization.values(),
        key=lambda option: (-option['count'], option['name']),
    )

    search = params.get('search', '').strip()
    provenance = params.get('provenance', '').strip()
    organization = params.get('organization', '').strip()
    filtered = apply_search(mappings, tab_rows, search)
    if search:
        # A search reaches across every tab, so the tab's own counts would
        # describe a different set of rows than the filter acts on -- offering
        # values that match nothing and hiding ones that dominate the hits.
        provenances = _provenance_options(dict(
            filtered.exclude(_Q_ATHENA).order_by()
            .values_list('origin_system').annotate(n=Count('pk'))
        ))
    # Narrows a cross-tab search as well as a single tab. Duplicates are left
    # unfiltered on purpose: hiding one half of a duplicated code would turn a
    # warning into a puzzle.
    filtered = apply_provenance(filtered, provenance)
    filtered = apply_organization(filtered, organization)
    if search or provenance or organization:
        _unmapped_filter = ~_Q_ATHENA & ~Q(status__in=['approved', 'rejected'])
        if seen_only:
            _unmapped_filter = _unmapped_filter & Q(occurrence_count__gt=0)
        totals = filtered.aggregate(
            unmapped=Count('pk', filter=_unmapped_filter),
            mapped=Count('pk', filter=~_Q_ATHENA & Q(status='approved')),
            athena=Count('pk', filter=_Q_ATHENA & ~Q(status='rejected')),
            rejected=Count('pk', filter=~_Q_ATHENA & Q(status='rejected')),
            athena_rejected=Count('pk', filter=_Q_ATHENA & Q(status='rejected')),
        )
    else:
        selected_counts = section_counts.values() if source == OVERALL else [section_counts.get(canonical_source(source), {})]
        totals = {key: sum(bucket.get(key, 0) for bucket in selected_counts)
                  for key in ('unmapped', 'mapped', 'athena', 'rejected', 'athena_rejected')}
    rejected = totals['rejected']
    section_queries = {name: carve(filtered) for name, carve in SECTION_FILTERS.items()}
    # Seen > 0 narrows only the Unmapped section: a curator triaging the queue
    # wants to focus on codes patients actually have, but Mapped and Athena
    # Mapped are reference that should be visible regardless of occurrence.
    if seen_only:
        section_queries['Unmapped'] = section_queries['Unmapped'].filter(occurrence_count__gt=0)
        totals['unmapped'] = section_queries['Unmapped'].count()
    # Rollup is opt-in. The flat queue stays the default so a caller that has
    # not been taught about groups keeps the behaviour it has, and so the two
    # shapes can be compared on the same data.
    rollup = params.get('rollup') == '1'
    pages, selected_ids, groups = {}, [], {}
    section_totals = [totals['unmapped'], totals['mapped'], rejected,
                      totals['athena'] + totals['athena_rejected']]
    for index, (section, query) in enumerate(section_queries.items()):
        try:
            requested_page = max(1, int(params.get(f'page_{index}', 1)))
        except (ValueError, TypeError):
            raise ValidationError({'page': 'Page must be an integer.'})
        # Every section defaults to Seen descending. Unmapped grouped by
        # provenance until #1575: that ordering is a no-op on a tab holding one
        # provenance (six of nine on staging), and on a tab holding several it
        # buries the rows a Suggest run just answered -- enqueue writes
        # origin_system='' and a run rewrites it to 'suggest v0.4', so the
        # answers move from the first provenance group to the last. Finding
        # curator-edited rows is what the provenance filter is for.
        order = params.get(f'order_{index}', '-occurrence_count')
        field = ORDER_FIELDS.get(order.lstrip('-'))
        if field is None:
            raise ValidationError({'order': 'Unknown sort column.'})
        total = section_totals[index]
        if rollup:
            # Pagination counts groups, not rows -- a page of 100 rows ordered
            # by a group's summed Seen would cut groups in half, and the whole
            # point is that one entry is one decision. Count first so an
            # out-of-range page clamps before the slice rather than paying for
            # a second aggregate.
            group_total = count_groups(query)
            page = min(requested_page, max(1, (group_total + PAGE_SIZE - 1) // PAGE_SIZE))
            groups[section] = group_entries(query, page, PAGE_SIZE)
            pages[section] = dict(page=page, page_size=PAGE_SIZE, total=group_total)
            continue
        page = min(requested_page, max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE))
        # Correlated destination counts and retirement lookups run only on the
        # bounded page unless the curator explicitly sorts by destination count.
        if field == 'destination_count':
            query = with_destination_counts(query)
        ordering = F(field).desc(nulls_last=True) if order.startswith('-') else F(field).asc(nulls_last=True)
        # Keep the most frequently seen codes first within each provenance
        # group, including when the curator reverses the group order.
        secondary = ['-occurrence_count'] if field == 'origin_system' else []
        ids = list(query.order_by(ordering, *secondary, 'source_code', 'id').values_list('pk', flat=True)[(page - 1) * PAGE_SIZE:page * PAGE_SIZE])
        selected_ids.extend(ids)
        pages[section] = dict(page=page, page_size=PAGE_SIZE, total=total)
    # Load all bounded sections together, preserving their selected order.
    page_rows = {row.pk: row for row in with_destination_counts(mappings.filter(pk__in=selected_ids))}
    results = [page_rows[pk] for pk in selected_ids if pk in page_rows]
    metadata = mapping_source_retirement(results + duplicates)

    def render(row):
        return serialize(row.target_concept, row, metadata[row.pk], destination_count=row.destination_count)

    return dict(results=[render(row) for row in results], duplicates=[render(row) for row in duplicates],
                total=sum(section_totals), seen_only=params.get('seen_only') == '1',
                tabs=tabs, selected_source=source, pages=pages, rejected_count=rejected,
                provenances=provenances, selected_provenance=provenance,
                organizations=organizations, selected_organization=organization,
                groups=groups, rollup=rollup)
