"""Concept-first review using the existing SCCM approval semantics."""
from uuid import UUID

from django.db import transaction
from django.db.models import Count, F, Q
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from omop_core.models import ConceptMappingPriority, FieldConceptMapping, SourceCodeConceptMapping, SuggestRun
from omop_core.services.concept_to_code import (
    browse_concepts, concept_payload, eligible_sources, source_payload, standard_destinations,
)
from omop_core.services.source_vocabularies import DOMAIN_TO_TABLE, VOCABULARY_OID_ALIASES
from omop_core.services.suggest_jobs import get_dispatcher, InlineDispatcher

PAGE_SIZE = 50
# The concept list may show more per page, so a curator can drag a concept
# from what would be a later page into the priority order (#1914).
CONCEPT_PAGE_SIZES = (50, 100, 250, 500)


def _authorize(user):
    from patient_portal.api.views import _can_manage_field_mappings
    if not _can_manage_field_mappings(user):
        raise PermissionDenied('Professional access required.')


def _paginate(queryset, params, page_size=PAGE_SIZE):
    try:
        page = int(params.get('page', 1))
        if page < 1:
            raise ValueError
    except (TypeError, ValueError):
        raise ValidationError({'page': 'Enter a positive whole number.'})
    count = queryset.count()
    page = min(page, max(1, (count + page_size - 1) // page_size))
    return queryset[(page - 1) * page_size:page * page_size], {
        'page': page, 'page_size': page_size, 'total': count,
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def concept_to_code_list(request):
    _authorize(request.user)
    params = request.query_params
    if params.get('scope', 'fields') not in ('fields', 'all'):
        raise ValidationError({'scope': 'Choose fields or all.'})
    if params.get('domain') and params['domain'] not in DOMAIN_TO_TABLE.values():
        raise ValidationError({'domain': 'Unknown clinical table.'})
    if params.get('status') and params['status'] not in ('approved', 'proposed'):
        raise ValidationError({'status': 'Choose approved or proposed.'})
    try:
        page_size = int(params.get('page_size', PAGE_SIZE))
    except (TypeError, ValueError):
        page_size = 0
    if page_size not in CONCEPT_PAGE_SIZES:
        raise ValidationError({'page_size': f'Choose one of {", ".join(map(str, CONCEPT_PAGE_SIZES))}.'})
    concepts, fields, annotations = browse_concepts(params)
    page, paging = _paginate(concepts, params, page_size)
    page = list(page.annotate(**annotations))
    field_map = {}
    for field in fields.filter(concept_id__in=[concept.pk for concept in page]).order_by('field_name'):
        field_map.setdefault(field.concept_id, []).append({
            'field_name': field.field_name, 'status': field.status, 'omop_table': field.omop_table,
        })
    domains = FieldConceptMapping.objects.exclude(status='rejected').filter(
        concept_id__in=standard_destinations().values('pk'),
    ).values('omop_table').annotate(count=Count('concept_id', distinct=True)).order_by('omop_table')
    return Response({
        **paging, 'domains': list(domains),
        'results': [{**concept_payload(concept), 'fields': field_map.get(concept.pk, []),
                     'sccm_counts': {key: getattr(concept, key) for key in ('approved', 'proposed', 'rejected')},
                     'seen': concept.seen, 'priority': concept.priority} for concept in page],
        'prioritized': ConceptMappingPriority.objects.count(),
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def concept_to_code_priority(request):
    """Rank one concept at a position, or unrank it with position null (#1914).

    One concept per request, so a move from a filtered or paged view needs no
    knowledge of the rest of the list: the server renumbers it.
    """
    from omop_core.services import concept_priority
    _authorize(request.user)
    data = request.data
    if not isinstance(data, dict):
        raise ValidationError({'detail': 'Supply a JSON object.'})
    concept_id, position = data.get('concept_id'), data.get('position', 'missing')
    if type(concept_id) is not int or concept_id <= 0:
        raise ValidationError({'concept_id': 'Supply a positive integer concept ID.'})
    if position is not None and (type(position) is not int or position < 1):
        raise ValidationError({'position': 'Supply a position of 1 or more, or null to unrank.'})
    concept = get_object_or_404(standard_destinations(), pk=concept_id)
    placed = concept_priority.move(concept, position, request.user)
    return Response({'concept_id': concept.pk, 'position': placed,
                     'prioritized': ConceptMappingPriority.objects.count()})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def concept_to_code_detail(request, concept_id):
    _authorize(request.user)
    concept = get_object_or_404(standard_destinations(), pk=concept_id)
    mode = request.query_params.get('mode', 'linked')
    if mode not in ('linked', 'available'):
        raise ValidationError({'mode': 'Choose linked or available.'})
    rows = (SourceCodeConceptMapping.objects.filter(target_concept=concept) if mode == 'linked'
            else eligible_sources(concept))
    search = request.query_params.get('search', '').strip()
    if search:
        rows = rows.filter(Q(source_code__icontains=search) | Q(source_code_description__icontains=search)
                           | Q(source_vocabulary_id__icontains=search))
    counts = rows.aggregate(encountered=Count('pk', filter=Q(occurrence_count__gt=0)),
                            zero_seen=Count('pk', filter=Q(occurrence_count=0)))
    from omop_core.services.mapping_browse import apply_seen
    rows = apply_seen(rows, request.query_params)
    order = request.query_params.get('order', '')
    if order not in ('', 'confidence', '-confidence'):
        raise ValidationError({'order': 'Choose confidence or -confidence.'})
    ordering = ['-occurrence_count', 'source_vocabulary_id', 'source_code', 'pk']
    if order:
        # Rows no ranker scored sort last in either direction.
        confidence = F('suggestion_confidence')
        ordering.insert(0, confidence.desc(nulls_last=True) if order == '-confidence'
                        else confidence.asc(nulls_last=True))
    page, paging = _paginate(rows.select_related('target_concept').order_by(*ordering), request.query_params)
    return Response({**paging, **counts, 'concept': concept_payload(concept),
                     'results': [source_payload(row) for row in page]})


SOURCE_EXAMPLE_LIMIT = 3
# Candidate inventory rows read before access filtering. A curator who can see
# few of the patients holding a common code may get fewer than the limit; the
# bound keeps the per-patient access checks below from growing with the code.
SOURCE_EXAMPLE_SCAN = 50


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def concept_to_code_source_examples(request):
    """Up to three patients whose ETL inventory carries this exact source code.

    Each example is that patient's `PatientSourceCode` row: the Source Metadata
    a curator checks before approving a mapping (#1911). Read-only. Patient
    source data has the same audience as the patient Source Codes tab --
    administrators -- and each patient passes the same check as opening their
    record, so a curator never sees a patient they could not open.
    """
    from omop_core.authorization import can_access_patient
    from omop_core.models import PatientSourceCode
    from omop_core.services.access import get_admin_orgs
    from patient_portal.api.permissions import get_request_org
    from patient_portal.api.views import _is_admin_actor

    _authorize(request.user)
    if not _is_admin_actor(request):
        raise PermissionDenied('Only administrators can view patient source metadata.')
    params = request.query_params
    vocabulary = str(params.get('source_vocabulary_id') or '').strip()
    code = str(params.get('source_code') or '').strip()
    if not code:
        raise ValidationError({'source_code': 'Supply the source code.'})
    omop_table = str(params.get('omop_table') or '').strip()
    if omop_table and omop_table not in DOMAIN_TO_TABLE.values():
        raise ValidationError({'omop_table': 'Unknown clinical table.'})

    # The ETL posts the system URI it received; mappings store the canonical id.
    aliases = [vocabulary, *[alias for alias, canonical in VOCABULARY_OID_ALIASES.items()
                             if canonical == vocabulary]]
    rows = PatientSourceCode.objects.filter(
        source_vocabulary_id__in=aliases, source_value=code,
        person__patient_record__isnull=False,
    )
    if omop_table:
        rows = rows.filter(Q(omop_table=omop_table) | Q(omop_table=''))
    rows = rows.order_by('-occurrence_count', '-last_seen', 'pk').values(
        'person_id', 'person__patient_record__organization_id', 'omop_table', 'occurrence_count',
        'source_unit', 'source_unit_override', 'example_quantity', 'example_quantity_override',
        'source_metadata', 'last_seen',
    )[:SOURCE_EXAMPLE_SCAN]

    org = get_request_org(request)
    staff = bool(getattr(request.user, 'is_staff', False))
    admin_org_ids = None if staff or org is not None else set(
        get_admin_orgs(request.user).values_list('id', flat=True))

    def visible(row):
        organization_id = row['person__patient_record__organization_id']
        if org is not None:
            return organization_id == org.pk
        if staff:
            return True
        return organization_id in admin_org_ids or can_access_patient(request.user, row['person_id'])

    examples, seen = [], set()
    for row in rows:
        if row['person_id'] in seen or not visible(row):
            continue
        seen.add(row['person_id'])
        examples.append({
            'person_id': row['person_id'], 'omop_table': row['omop_table'],
            'occurrence_count': row['occurrence_count'],
            'source_unit': (row['source_unit_override'] if row['source_unit_override'] is not None
                            else row['source_unit']),
            'example_quantity': (row['example_quantity_override'] if row['example_quantity_override'] is not None
                                 else row['example_quantity']),
            'source_metadata': row['source_metadata'] or {},
            'last_seen': row['last_seen'].isoformat() if row['last_seen'] else None,
        })
        if len(examples) == SOURCE_EXAMPLE_LIMIT:
            break
    return Response({'examples': examples})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def concept_to_code_suggest(request):
    _authorize(request.user)
    data = request.data
    if not isinstance(data, dict):
        raise ValidationError({'detail': 'Supply a JSON object.'})
    ids = data.get('concept_ids')
    if not isinstance(ids, list) or not ids or any(type(value) is not int or value <= 0 for value in ids):
        raise ValidationError({'concept_ids': 'Supply a nonempty list of positive integer concept IDs.'})
    ids = list(dict.fromkeys(ids))
    dispatcher = get_dispatcher()
    inline = isinstance(dispatcher, InlineDispatcher)
    max_concepts = 1 if inline else 10
    if len(ids) > max_concepts:
        raise ValidationError({'concept_ids': f'At most {max_concepts} concepts per run.'})
    if standard_destinations().filter(pk__in=ids).count() != len(ids):
        raise ValidationError({'concept_ids': 'Every destination must be a current standard concept in a clinical domain.'})
    strategies = data.get('strategies', ['umls', 'lexical'])
    if not isinstance(strategies, list) or not strategies or any(s not in ('umls', 'lexical') for s in strategies):
        raise ValidationError({'strategies': 'Choose umls, lexical, or both.'})
    ranking_model = data.get('ranking_model', 'none')
    if ranking_model not in ('none', 'anthropic', 'jev', 'both'):
        raise ValidationError({'ranking_model': 'Unknown ranker.'})
    include_zero = data.get('include_zero_seen', False)
    if type(include_zero) is not bool:
        raise ValidationError({'include_zero_seen': 'Supply a boolean.'})
    limit = data.get('limit', 10)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValidationError({'limit': 'Choose a whole number from 1 to 100.'})
    # One ranking request per candidate, not per concept.
    ceiling = dispatcher.max_codes if ranking_model != 'none' else 100
    limit = min(limit, max(1, ceiling // len(ids)))
    params = dict(direction='reverse', concept_ids=ids, strategies=strategies, limit=limit,
                  include_zero_seen=include_zero, ranking_model=ranking_model,
                  retrieval_timeout_ms=8000 if inline else 30000)
    with transaction.atomic():
        run = SuggestRun.objects.create(
            direction='reverse', total=len(ids), created_by=request.user,
            ranking_model=ranking_model, selection=params,
        )
        dispatcher.dispatch(run, params)
    run.refresh_from_db()
    return Response(_run_payload(run), status=202)


def _run_payload(run):
    return {'run_id': str(run.pk), 'state': run.state, 'total': run.total,
            'done': run.done, 'error': run.error, 'selection': run.selection,
            'activity': run.activity or []}


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def concept_to_code_run(request, run_id):
    _authorize(request.user)
    return Response(_run_payload(get_object_or_404(SuggestRun, pk=run_id, direction='reverse')))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@transaction.atomic
def concept_to_code_propose(request, concept_id):
    """Create a proposal only for a reviewed source in a saved preview."""
    from omop_core.mapping.reverse_catalog import refresh_catalog_candidate
    from patient_portal.api.views import _upsert_source_code_mapping
    _authorize(request.user)
    if not isinstance(request.data, dict):
        raise ValidationError({'detail': 'Supply a JSON object.'})
    if request.data.get('status', 'proposed') != 'proposed':
        raise ValidationError({'status': 'Propose a new source before approving its mapping.'})
    try:
        run_id = UUID(str(request.data.get('run_id', '')))
    except (TypeError, ValueError):
        raise ValidationError({'run_id': 'Supply the source preview run ID.'})
    concept = get_object_or_404(standard_destinations(), pk=concept_id)
    run = get_object_or_404(SuggestRun, pk=run_id, direction='reverse', state__in=[SuggestRun.SUCCESS, SuggestRun.FAILURE])
    candidate = next((candidate for event in (run.activity or [])
                      if event['concept']['concept_id'] == concept.pk
                      for candidate in event['candidates']
                      if candidate.get('mapping_id') is None
                      and candidate['source_code'] == request.data.get('source_code')
                      and candidate['source_vocabulary_id'] == request.data.get('source_vocabulary_id')), None)
    if candidate is None:
        raise ValidationError({'detail': 'Choose a source code from this concept’s saved preview.'})
    fresh = refresh_catalog_candidate(candidate, concept)
    if fresh is None:
        return Response({'detail': 'The source vocabulary changed after the preview. Run the search again.'}, status=409)
    aliases = [fresh['source_vocabulary_id'], *[alias for alias, canonical in VOCABULARY_OID_ALIASES.items()
                                             if canonical == fresh['source_vocabulary_id']]]
    if SourceCodeConceptMapping.objects.select_for_update().filter(
        source_vocabulary_id__in=aliases, source_code=fresh['source_code'],
    ).exists():
        return Response({'detail': 'This source already has a mapping. Refresh before reviewing it.'}, status=409)
    row, _ = _upsert_source_code_mapping(concept, {
        'source_vocabulary_id': fresh['source_vocabulary_id'], 'source_code': fresh['source_code'],
        'source_code_description': fresh['source_code_description'][:255],
        **({'notes': f"Source vocabulary label: {fresh['source_code_description']}"}
           if len(fresh['source_code_description']) > 255 else {}),
        'domain_id': concept.domain_id, 'omop_table': DOMAIN_TO_TABLE[concept.domain_id],
        'status': 'proposed',
    }, request.user)
    # The preview's ranker judged this source against this concept; without a
    # ranker the candidate carries no confidence and the row keeps none.
    if candidate.get('confidence') is not None:
        row.suggestion_confidence = candidate['confidence']
        row.save(update_fields=['suggestion_confidence'])
    return Response(source_payload(row), status=201)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@transaction.atomic
def concept_to_code_apply(request, concept_id, mapping_id):
    """One reviewed source per transaction, with an exact revision precondition."""
    from patient_portal.api.views import (
        _check_mapping_lock, _upsert_source_code_mapping, MappingLocked,
    )
    _authorize(request.user)
    if not isinstance(request.data, dict):
        raise ValidationError({'detail': 'Supply a JSON object.'})
    concept = get_object_or_404(standard_destinations(), pk=concept_id)
    row = get_object_or_404(SourceCodeConceptMapping.objects.select_for_update(), pk=mapping_id)
    if row.status != 'proposed' or row.origin_system == 'athena':
        return Response({'detail': 'This source is no longer an editable proposal. Refresh before reviewing it.'}, status=409)
    if request.data.get('expected_updated_at') != row.updated_at.isoformat():
        return Response({'detail': 'This source changed after the preview. Refresh before reviewing it.'}, status=409)
    try:
        _check_mapping_lock(row, request.user)
    except MappingLocked as exc:
        return exc.as_response()
    table = DOMAIN_TO_TABLE[concept.domain_id]
    if (row.domain_id and row.domain_id != concept.domain_id) or (row.omop_table and row.omop_table != table):
        raise ValidationError({'detail': 'Source and destination domains disagree. Review this source in the full editor.'})
    selected_status = request.data.get('status', 'proposed')
    if selected_status not in ('proposed', 'approved'):
        raise ValidationError({'status': 'Choose proposed or approved.'})
    row, repoint = _upsert_source_code_mapping(concept, {
        'status': selected_status, 'domain_id': concept.domain_id, 'omop_table': table,
        'destination_vocabulary_id': concept.vocabulary_id,
    }, request.user, mapping=row)
    return Response({**source_payload(row), 'repoint': repoint})
