"""Concept-first review using the existing SCCM approval semantics."""
from uuid import UUID

from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from omop_core.models import FieldConceptMapping, SourceCodeConceptMapping, SuggestRun
from omop_core.services.concept_to_code import (
    browse_concepts, concept_payload, eligible_sources, source_payload, standard_destinations,
)
from omop_core.services.source_vocabularies import DOMAIN_TO_TABLE, VOCABULARY_OID_ALIASES
from omop_core.services.suggest_jobs import get_dispatcher, InlineDispatcher

PAGE_SIZE = 50


def _authorize(user):
    from patient_portal.api.views import _can_manage_field_mappings
    if not _can_manage_field_mappings(user):
        raise PermissionDenied('Professional access required.')


def _paginate(queryset, params):
    try:
        page = int(params.get('page', 1))
        if page < 1:
            raise ValueError
    except (TypeError, ValueError):
        raise ValidationError({'page': 'Enter a positive whole number.'})
    count = queryset.count()
    page = min(page, max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE))
    return queryset[(page - 1) * PAGE_SIZE:page * PAGE_SIZE], {
        'page': page, 'page_size': PAGE_SIZE, 'total': count,
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
    concepts, fields, annotations = browse_concepts(params)
    page, paging = _paginate(concepts, params)
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
                     'seen': concept.seen} for concept in page],
    })


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
    page, paging = _paginate(rows.select_related('target_concept').order_by(
        '-occurrence_count', 'source_vocabulary_id', 'source_code', 'pk'), request.query_params)
    return Response({**paging, **counts, 'concept': concept_payload(concept),
                     'results': [source_payload(row) for row in page]})


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
