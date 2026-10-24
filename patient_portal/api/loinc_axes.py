"""The six LOINC axes of a source code, and its axis variants, for curators.

``GET  /api/v1/code-mappings/<id>/loinc-axes/`` -- what the source settles on
each axis and why, the curator's entries, and the variants its units imply.
``PUT`` saves the curator's entries. ``POST .../axis-variants/`` points one
variant at a destination and, when approving, re-points its stored rows.
``POST .../axis-variants/suggest/`` queues a run that suggests a standard LOINC
for each variant without one and stores it as proposed (#2012).
``GET /api/v1/loinc/axis-values/`` suggests values for an axis as a curator
types. See ``omop_core/mapping/loinc_axes.py`` and ``axis_variants.py``.
"""
from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from omop_core.mapping import axis_variants, loinc_axes
from omop_core.models import Concept, LoincCodeClass, SourceCodeConceptMapping, SuggestRun

AXIS_VALUE_LIMIT = 25


def _views():
    # Imported late: views.py is the module that routes to this one.
    from patient_portal.api import views
    return views


def _axes_and_variants(mapping, domain):
    """``(axes, hints, detected, notes)`` from one scan of the unit evidence;
    ``axes`` is None when the domain cannot hold a LOINC, and then nothing
    is scanned."""
    from omop_core.mapping.suggestions import LOINC_AXIS_DOMAINS, source_loinc_axes

    if domain not in LOINC_AXIS_DOMAINS and not loinc_axes.is_loinc(mapping.source_vocabulary_id):
        return None, {}, [], []
    counts = axis_variants.unit_counts(mapping)
    axes, hints = source_loinc_axes(
        domain_id=domain, source_vocabulary_id=mapping.source_vocabulary_id,
        source_code=mapping.source_code, metadata=mapping.source_metadata,
        overrides=mapping.loinc_axes,
        observed_units=axis_variants.observed_units(mapping, counts),
    )
    detected, notes = axis_variants.detect(mapping, counts)
    return axes, hints, detected, notes


def _payload(mapping):
    domain = mapping.domain_id or {'measurement': 'Measurement', 'observation': 'Observation'}.get(
        mapping.omop_table, '')
    axes, hints, detected, notes = _axes_and_variants(mapping, domain)
    stored = {v.axis_key: v for v in mapping.axis_variants.select_related('target_concept')}
    variants = []
    for variant in detected:
        row = stored.pop(variant['axis_key'], None)
        variants.append({
            **(axis_variants.serialize(row) if row else {
                'id': None, 'status': 'proposed', 'target_concept': None, 'reviewed_at': None,
            }),
            **variant,
        })
    # A stored variant not seen in the current evidence keeps its destination.
    variants += [{**axis_variants.serialize(row), 'seen': False} for row in stored.values()]
    return {
        'mapping_id': mapping.pk,
        'applies': axes is not None,
        'loinc_axes': mapping.loinc_axes or {},
        'source_axes': loinc_axes.source_summary(axes or {}, hints),
        'axis_labels': loinc_axes.AXIS_LABELS,
        'axis_variant_count': mapping.axis_variant_count,
        'variants': variants if len(variants) >= axis_variants.FLAG_THRESHOLD else [],
        'variant_notes': notes,
    }


@api_view(['GET', 'PUT'])
@permission_classes([IsAuthenticated])
def code_mapping_loinc_axes(request, mapping_id):
    views = _views()
    if not views._can_manage_field_mappings(request.user):
        return Response({'detail': 'Organization admin access required.'}, status=status.HTTP_403_FORBIDDEN)
    mapping = SourceCodeConceptMapping.objects.filter(pk=mapping_id).first()
    if mapping is None:
        return Response({'detail': 'Mapping not found.'}, status=status.HTTP_404_NOT_FOUND)
    if request.method == 'GET':
        return Response(_payload(mapping))

    if not views._can_administer_code_mapping(request.user, mapping):
        return Response({'detail': 'You do not administer this mapping organization.'},
                        status=status.HTTP_403_FORBIDDEN)
    try:
        views._check_mapping_lock(mapping, request.user)
    except views.MappingLocked as exc:
        return exc.as_response()
    try:
        overrides = loinc_axes.clean_overrides(request.data.get('loinc_axes'))
    except ValueError as exc:
        return Response({'loinc_axes': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    mapping.loinc_axes = overrides
    mapping.updated_by = request.user
    mapping.save(update_fields=['loinc_axes', 'updated_by', 'updated_at'])
    return Response(_payload(mapping))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def code_mapping_axis_variant(request, mapping_id):
    """Set one variant's destination: ``{axis_key, target_concept_id, approve}``.

    Variants are stored from the current evidence first, so a curator can act
    on one the dialog detected but no refresh has saved yet.
    """
    views = _views()
    if not views._can_manage_field_mappings(request.user):
        return Response({'detail': 'Organization admin access required.'}, status=status.HTTP_403_FORBIDDEN)
    with transaction.atomic():
        mapping = (SourceCodeConceptMapping.objects.select_for_update(of=('self',))
                   .filter(pk=mapping_id).first())
        if mapping is None:
            return Response({'detail': 'Mapping not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not views._can_administer_code_mapping(request.user, mapping):
            return Response({'detail': 'You do not administer this mapping organization.'},
                            status=status.HTTP_403_FORBIDDEN)
        try:
            views._check_mapping_lock(mapping, request.user)
        except views.MappingLocked as exc:
            return exc.as_response()
        approve = request.data.get('approve') is True
        reopen = request.data.get('reopen') is True
        if approve and not views._can_approve_mappings(request.user):
            return Response({'detail': 'Approving a mapping requires approver access.'},
                            status=status.HTTP_403_FORBIDDEN)
        axis_key = str(request.data.get('axis_key') or '')
        raw_target = request.data.get('target_concept_id')
        concept = None
        if raw_target not in (None, ''):
            try:
                concept = Concept.objects.filter(pk=int(raw_target), invalid_reason__isnull=True).first()
            except (TypeError, ValueError):
                concept = None
            if concept is None:
                return Response({'target_concept_id': 'No valid concept with that id.'},
                                status=status.HTTP_400_BAD_REQUEST)
            if not axis_variants.usable_destination(concept, mapping):
                return Response({'target_concept_id': (
                    'A variant destination must be an active standard concept'
                    + (f' in the {mapping.domain_id} domain.' if mapping.domain_id else '.')
                )}, status=status.HTTP_400_BAD_REQUEST)
        if approve and concept is None:
            return Response({'target_concept_id': 'Choose a destination before approving.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if reopen and not views._can_approve_mappings(request.user):
            # Reopening rewrites stored rows, like approving.
            return Response({'detail': 'Reopening an approved variant requires approver access.'},
                            status=status.HTTP_403_FORBIDDEN)
        stored = {v.axis_key: v for v in axis_variants.refresh(mapping)}
        variant = stored.get(axis_key)
        if variant is None:
            return Response({'axis_key': 'This code has no such axis variant.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            repoint = axis_variants.set_destination(variant, concept, approve=approve,
                                                    user=request.user, reopen=reopen)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    payload = _payload(mapping)
    payload['variant_status'] = variant.status
    if repoint is not None:
        payload['repoint'] = {key: value for key, value in repoint.items() if key != 'person_ids'}
    return Response(payload)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def code_mapping_axis_variant_suggest(request, mapping_id):
    """Queue a run that suggests and stores a proposed LOINC per variant (#2012).

    ``{strategies?, ranking_model?, loinc_axes?}``. ``loinc_axes`` are the
    dialog's possibly unsaved curator entries, applied under each variant's own
    axis values as Suggest for one variant does. Returns 202 with the run, as
    ``suggest-one`` does; the run's ``result`` event carries ``suggestions``
    and the variants as stored. A variant that already has a destination is
    never re-suggested.
    """
    from omop_core.mapping.suggestions import (
        ALL_STRATEGIES, DEFAULT_RANKING_MODEL, DEFAULT_STRATEGIES, RANKING_MODELS,
        SUGGESTION_MODEL_VERSION,
    )
    from omop_core.services.suggest_jobs import get_dispatcher

    views = _views()
    if not views._can_manage_field_mappings(request.user):
        return Response({'detail': 'Organization admin access required.'}, status=status.HTTP_403_FORBIDDEN)
    mapping = SourceCodeConceptMapping.objects.filter(pk=mapping_id).first()
    if mapping is None:
        return Response({'detail': 'Mapping not found.'}, status=status.HTTP_404_NOT_FOUND)
    if not views._can_administer_code_mapping(request.user, mapping):
        return Response({'detail': 'You do not administer this mapping organization.'},
                        status=status.HTTP_403_FORBIDDEN)
    try:
        views._check_mapping_lock(mapping, request.user)
    except views.MappingLocked as exc:
        return exc.as_response()
    strategies = request.data.get('strategies') or list(DEFAULT_STRATEGIES)
    if not isinstance(strategies, list) or not strategies or any(s not in ALL_STRATEGIES for s in strategies):
        return Response({'strategies': 'Choose at least one retrieval strategy.'},
                        status=status.HTTP_400_BAD_REQUEST)
    ranking_model = str(request.data.get('ranking_model') or DEFAULT_RANKING_MODEL)
    if ranking_model not in RANKING_MODELS:
        return Response({'ranking_model': f'Must be one of {sorted(RANKING_MODELS)}.'},
                        status=status.HTTP_400_BAD_REQUEST)
    try:
        overrides = (loinc_axes.clean_overrides(request.data.get('loinc_axes'))
                     if 'loinc_axes' in request.data else None)
    except ValueError as exc:
        return Response({'loinc_axes': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    dispatcher = get_dispatcher()
    # Inline (no broker) the run executes in this request: as many pipelines
    # as that dispatcher allows a run, never more.
    limit = min(axis_variants.SUGGEST_MAX, dispatcher.max_codes)
    spec = {'mapping_id': mapping.pk, 'strategies': strategies, 'ranking_model': ranking_model,
            'overrides': overrides, 'limit': limit}
    with transaction.atomic():
        run = SuggestRun.objects.create(
            source_vocabulary_id=mapping.source_vocabulary_id, total=1,
            model_version=SUGGESTION_MODEL_VERSION, ranking_model=ranking_model,
            created_by=request.user,
            selection={'mode': 'axis-variants', 'mapping_id': mapping.pk, 'limit': limit,
                       'strategies': strategies, 'source_vocabulary_id': mapping.source_vocabulary_id,
                       'order': 'Each axis variant of the code open in the mapping dialog.'},
        )
        dispatcher.dispatch(run, {'variants': spec, 'ranking_model': ranking_model})
    run.refresh_from_db()
    return Response(views._serialize_suggest_run(run, include_activity=True), status=status.HTTP_202_ACCEPTED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def loinc_axis_values(request):
    """The commonest LOINC values of one axis containing ``q``."""
    from django.db.models import Count

    if not _views()._can_manage_field_mappings(request.user):
        return Response({'detail': 'Organization admin access required.'}, status=status.HTTP_403_FORBIDDEN)
    axis = request.query_params.get('axis', '')
    if axis not in loinc_axes.AXES:
        return Response({'axis': f'Must be one of {", ".join(loinc_axes.AXES)}.'},
                        status=status.HTTP_400_BAD_REQUEST)
    rows = LoincCodeClass.objects.exclude(**{axis: ''})
    query = (request.query_params.get('q') or '').strip()
    if query:
        rows = rows.filter(**{f'{axis}__icontains': query})
    values = (rows.values(axis).annotate(codes=Count('loinc_num'))
              .order_by('-codes', axis)[:AXIS_VALUE_LIMIT])
    return Response({'axis': axis, 'values': [
        {'value': row[axis], 'codes': row['codes']} for row in values
    ]})


def _loinc_evidence(mapping, domain):
    """Why the source's destination is a LOINC, or None (#2012, #2014).

    Any LOINC axis evidence counts, strongest reason first: units implying
    several axis sets (variants), a lab signal (a LOINC code or coding, a
    laboratory category, a property or scale the records settle), then any
    other settled axis, a curator's included. SNOMED hierarchy and attribute
    judging has nothing to add to such a code.
    """
    axes, _hints, detected, _notes = _axes_and_variants(mapping, domain)
    if axes is None:
        return None
    if len(detected) >= axis_variants.FLAG_THRESHOLD:
        return f'Its units imply {len(detected)} LOINC axis sets'
    reason = loinc_axes.lab_signal(mapping.source_vocabulary_id, mapping.source_metadata, axes)
    if reason:
        return reason
    for axis in loinc_axes.AXES:
        entry = axes.get(axis)
        if entry:
            value = 'methodless' if axis == 'method_type' and entry['value'] == '' else entry['value']
            return (f"LOINC axis evidence: {loinc_axes.AXIS_LABELS[axis]} {value}"
                    + (f" ({entry['evidence']})" if entry.get('evidence') else ''))
    return None


def _snomed_payload(mapping):
    from omop_core.mapping import snomed_attributes
    from omop_core.mapping.suggestions import _find_source_concept, source_snomed

    domain = mapping.domain_id or {
        'condition_occurrence': 'Condition', 'procedure_occurrence': 'Procedure',
        'observation': 'Observation', 'measurement': 'Measurement',
    }.get(mapping.omop_table, '')
    source_concept = mapping.source_concept or _find_source_concept(
        mapping.source_vocabulary_id, mapping.source_code)
    source = source_snomed(
        domain_id=domain, source_concept=source_concept,
        description=mapping.source_code_description, metadata=mapping.source_metadata,
        overrides=mapping.snomed_attributes,
    )
    # A code with LOINC axis evidence has a LOINC destination: SNOMED judging
    # does not apply and the panel is hidden, however the source would read
    # as SNOMED (#2012, #2014).
    loinc_reason = _loinc_evidence(mapping, domain) if source is not None else None
    return {
        'mapping_id': mapping.pk,
        'applies': source is not None and loinc_reason is None,
        'loinc_evidence': loinc_reason,
        'snomed_attributes': mapping.snomed_attributes or {},
        'source': snomed_attributes.source_summary(source) if source is not None else None,
        'hierarchies': snomed_attributes.HIERARCHY_TAGS,
        'lateralities': list(snomed_attributes.LATERALITIES),
    }


@api_view(['GET', 'PUT'])
@permission_classes([IsAuthenticated])
def code_mapping_snomed_attributes(request, mapping_id):
    """A source code's SNOMED evidence; ``PUT`` saves the curator's settings
    ``{reference_concept_id, hierarchy, laterality}``."""
    from omop_core.mapping import snomed_attributes

    views = _views()
    if not views._can_manage_field_mappings(request.user):
        return Response({'detail': 'Organization admin access required.'}, status=status.HTTP_403_FORBIDDEN)
    mapping = SourceCodeConceptMapping.objects.filter(pk=mapping_id).first()
    if mapping is None:
        return Response({'detail': 'Mapping not found.'}, status=status.HTTP_404_NOT_FOUND)
    if request.method == 'GET':
        return Response(_snomed_payload(mapping))

    if not views._can_administer_code_mapping(request.user, mapping):
        return Response({'detail': 'You do not administer this mapping organization.'},
                        status=status.HTTP_403_FORBIDDEN)
    try:
        views._check_mapping_lock(mapping, request.user)
    except views.MappingLocked as exc:
        return exc.as_response()
    try:
        settings_ = snomed_attributes.clean_overrides(request.data.get('snomed_attributes'))
    except ValueError as exc:
        return Response({'snomed_attributes': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    reference = settings_.get('reference_concept_id')
    if reference and not Concept.objects.filter(
            pk=reference, vocabulary_id='SNOMED', standard_concept='S', invalid_reason__isnull=True).exists():
        return Response({'snomed_attributes': 'The reference must be an active standard SNOMED concept.'},
                        status=status.HTTP_400_BAD_REQUEST)
    mapping.snomed_attributes = settings_
    mapping.updated_by = request.user
    mapping.save(update_fields=['snomed_attributes', 'updated_by', 'updated_at'])
    return Response(_snomed_payload(mapping))
