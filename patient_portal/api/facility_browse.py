"""Browse source codes by facility, then source code system (#2839).

A navigation layer over what already exists: ``SourceSystemFacility`` says
which facilities were observed using an exact source system, and
``SourceCodeConceptMapping`` holds the codes. Nothing here changes a code's
identity, axis variants or approval; a row links to the Edit Mapping dialog.
Rejected associations are ignored. A system with no usable association is
listed under the ``Unattributed`` facility so its codes stay reachable.
"""

from django.core.paginator import EmptyPage, Paginator
from django.db.models import Count, F, Q
from django.db.models.functions import Length
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from omop_core.models import SourceCodeConceptMapping, SourceSystemFacility
from omop_core.services.access import has_mapping_read_access

UNATTRIBUTED = '__unattributed__'
PAGE_SIZE = 100
STATUSES = ('unmapped', 'proposed', 'approved', 'rejected')
SORTS = {'code': ('source_code_length', 'source_code', 'id'),
         '-code': ('-source_code_length', '-source_code', '-id'),
         'seen': ('-occurrence_count', 'source_code', 'id'),
         'description': ('source_code_description', 'source_code', 'id')}


def _associations():
    return SourceSystemFacility.objects.exclude(review_status='rejected')


def _attributed_systems():
    return set(_associations().order_by().values_list(
        'source_vocabulary_id', flat=True).distinct())


def _systems_of(facility):
    """Systems the facility uses; for Unattributed, every system with none."""
    if facility == UNATTRIBUTED:
        used = set(SourceCodeConceptMapping.objects.order_by().values_list(
            'source_vocabulary_id', flat=True).distinct())
        return used - _attributed_systems()
    return set(_associations().filter(facility_name__iexact=facility).order_by()
               .values_list('source_vocabulary_id', flat=True).distinct())


def _code_counts(systems):
    return dict(SourceCodeConceptMapping.objects.filter(source_vocabulary_id__in=systems)
                .order_by().values_list('source_vocabulary_id')
                .annotate(n=Count('id')).values_list('source_vocabulary_id', 'n'))


def _status_filter(qs, value):
    has_destination = Q(target_concept_id__isnull=False) & ~Q(target_concept_id=0)
    if value == 'unmapped':
        return qs.exclude(has_destination)
    if value in ('proposed', 'approved', 'rejected'):
        if value == 'rejected':
            return qs.filter(status='rejected')
        return qs.filter(has_destination, status=value)
    return qs


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def facility_browse(request):
    user = request.user
    if not (user.is_staff or has_mapping_read_access(user)):
        return Response({'detail': 'Mapping access required.'},
                        status=status.HTTP_403_FORBIDDEN)
    params = request.query_params
    facility = params.get('facility', '').strip()
    system = params.get('system', '').strip()
    q = params.get('q', '').strip()
    state = params.get('status', '').strip()
    table = params.get('omop_table', '').strip()
    sort = params.get('sort', 'code')
    if max(len(facility), len(system), len(q), len(table)) > 255:
        return Response({'detail': 'Filter is too long.'}, status=status.HTTP_400_BAD_REQUEST)
    if state and state not in STATUSES:
        return Response({'detail': 'Invalid status.'}, status=status.HTTP_400_BAD_REQUEST)
    if sort not in SORTS:
        return Response({'detail': 'Invalid sort.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        page = int(params.get('page', '1'))
    except ValueError:
        page = 0
    if page < 1:
        return Response({'detail': 'page must be a positive integer.'},
                        status=status.HTTP_400_BAD_REQUEST)

    if not facility and not system:
        return Response({'facilities': _facility_list(params.get('search', '').strip())})

    systems = {system} if system and not facility else _systems_of(facility)
    counts = _code_counts(systems)
    other = {}
    if facility != UNATTRIBUTED:
        for sys_id, name in _associations().filter(source_vocabulary_id__in=systems).exclude(
                facility_name__iexact=facility).order_by().values_list(
                'source_vocabulary_id', 'facility_name'):
            other.setdefault(sys_id, set()).add(name.casefold())
    system_rows = [{'source_vocabulary_id': s, 'code_count': counts.get(s, 0),
                    'shared_with_facilities': len(other.get(s, ()))}
                   for s in sorted(systems, key=lambda v: (v.casefold(), v))]

    rows = SourceCodeConceptMapping.objects.filter(source_vocabulary_id__in=systems)
    if system:
        rows = rows.filter(source_vocabulary_id=system)
    if q:
        rows = rows.filter(Q(source_code__icontains=q) | Q(source_code_description__icontains=q))
    if table:
        rows = rows.filter(omop_table=table)
    rows = _status_filter(rows, state)
    rows = rows.annotate(source_code_length=Length('source_code')).select_related(
        'target_concept', 'destination_unit_concept').order_by(*SORTS[sort])
    paginator = Paginator(rows, PAGE_SIZE)
    try:
        current = paginator.page(page)
    except EmptyPage:
        current = paginator.page(paginator.num_pages)
    results = [{
        'id': m.id, 'source_vocabulary_id': m.source_vocabulary_id,
        'source_code': m.source_code, 'description': m.source_code_description,
        'omop_table': m.omop_table, 'status': m.status,
        'destination_concept_id': m.target_concept_id or None,
        'destination_concept_name': m.target_concept.concept_name if m.target_concept_id else '',
        'destination_vocabulary_id': m.target_concept.vocabulary_id if m.target_concept_id else '',
        'destination_concept_code': m.target_concept.concept_code if m.target_concept_id else '',
        'destination_unit': (m.destination_unit_concept.concept_code
                             if m.destination_unit_concept_id else ''),
        'seen': m.occurrence_count, 'axis_variant_count': m.axis_variant_count,
    } for m in current.object_list]
    return Response({
        'facility': facility, 'systems': system_rows, 'results': results,
        'total': paginator.count, 'page': current.number, 'page_size': PAGE_SIZE,
    })


def _facility_list(search):
    assoc = _associations()
    if search:
        assoc = assoc.filter(facility_name__icontains=search)
    by_name = {}
    for sys_id, name in assoc.order_by().values_list('source_vocabulary_id', 'facility_name'):
        entry = by_name.setdefault(name.casefold(), {'facility': name, 'systems': set()})
        entry['systems'].add(sys_id)
    counts = _code_counts({s for e in by_name.values() for s in e['systems']})
    out = [{'facility': e['facility'], 'system_count': len(e['systems']),
            'code_count': sum(counts.get(s, 0) for s in e['systems'])}
           for e in by_name.values()]
    out.sort(key=lambda e: e['facility'].casefold())
    if not search:
        loose = _systems_of(UNATTRIBUTED)
        if loose:
            loose_counts = _code_counts(loose)
            out.append({'facility': UNATTRIBUTED, 'system_count': len(loose),
                        'code_count': sum(loose_counts.values())})
    return out
