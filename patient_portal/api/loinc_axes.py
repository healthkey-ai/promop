"""The six LOINC axes of a source code, and its axis variants, for curators.

``GET  /api/v1/code-mappings/<id>/loinc-axes/`` -- what the source settles on
each axis and why, the curator's entries, and the variants its units imply.
``PUT`` saves the curator's entries. ``POST .../axis-variants/`` points one
variant at a destination and, when approving, re-points its stored rows.
``GET /api/v1/loinc/axis-values/`` suggests values for an axis as a curator
types. See ``omop_core/mapping/loinc_axes.py`` and ``axis_variants.py``.
"""
from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from omop_core.mapping import axis_variants, loinc_axes
from omop_core.models import Concept, LoincCodeClass, SourceCodeConceptMapping

AXIS_VALUE_LIMIT = 25


def _views():
    # Imported late: views.py is the module that routes to this one.
    from patient_portal.api import views
    return views


def _payload(mapping):
    from omop_core.mapping.suggestions import source_loinc_axes

    domain = mapping.domain_id or {'measurement': 'Measurement', 'observation': 'Observation'}.get(
        mapping.omop_table, '')
    counts = axis_variants.unit_counts(mapping)
    axes, hints = source_loinc_axes(
        domain_id=domain, source_vocabulary_id=mapping.source_vocabulary_id,
        source_code=mapping.source_code, metadata=mapping.source_metadata,
        overrides=mapping.loinc_axes,
        observed_units=axis_variants.observed_units(mapping, counts),
    )
    detected, notes = axis_variants.detect(mapping, counts)
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
    return {
        'mapping_id': mapping.pk,
        'applies': source is not None,
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
