"""Curator review of exact source-system / observed-facility relationships."""

import hashlib
import json

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from omop_core.models import HospitalCodeImport, SourceCodeConceptMapping, SourceSystemFacility
from omop_core.services.access import has_professional_access


PAGE_SIZE = 50


@api_view(['GET', 'POST', 'PATCH'])
@permission_classes([IsAuthenticated])
def source_system_facilities(request):
    """Browse and review associations without rewriting imported FHIR identity."""
    if not (request.user.is_staff or has_professional_access(request.user)):
        return Response({'detail': 'Organization admin access required.'},
                        status=status.HTTP_403_FORBIDDEN)

    if request.method == 'POST':
        source_system = str(request.data.get('source_vocabulary_id') or '').strip()
        name = str(request.data.get('facility_name') or '').strip()
        facility_id = str(request.data.get('facility_id') or '').strip()
        if (not source_system.startswith(('urn:oid:', 'http://', 'https://'))
                or len(source_system) > 255 or not name or len(name) > 255
                or len(facility_id) > 255):
            return Response({'detail': 'A valid exact system URI and facility name are required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if SourceSystemFacility.objects.filter(
                source_vocabulary_id=source_system, facility_name__iexact=name,
                facility_id=facility_id).exists():
            return Response({'detail': 'This association already exists; review the existing row.'},
                            status=status.HTTP_409_CONFLICT)
        fingerprint = hashlib.sha256(json.dumps(
            [source_system, name, facility_id, 'curator-added'],
            ensure_ascii=False,
        ).encode('utf-8')).hexdigest()
        try:
            association = SourceSystemFacility.objects.create(
                source_vocabulary_id=source_system, facility_name=name,
                facility_id=facility_id, level='', confidence='curated',
                attribution_method='curator', provenance='curator-added',
                fingerprint=fingerprint, review_status='confirmed',
                reviewed_by=request.user.email, reviewed_at=timezone.now(),
            )
        except IntegrityError:
            return Response({'detail': 'This association already exists.'},
                            status=status.HTTP_409_CONFLICT)
        return Response({'id': association.id}, status=status.HTTP_201_CREATED)

    if request.method == 'PATCH':
        decision = request.data.get('review_status')
        if decision not in ('confirmed', 'rejected', 'unreviewed'):
            return Response({'detail': 'Invalid review status.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            association_id = int(request.data.get('id'))
        except (TypeError, ValueError):
            return Response({'detail': 'A numeric association id is required.'},
                            status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            association = SourceSystemFacility.objects.select_for_update().filter(
                id=association_id,
            ).first()
            if association is None:
                return Response({'detail': 'Association not found.'},
                                status=status.HTTP_404_NOT_FOUND)
            association.review_status = decision
            association.reviewed_by = request.user.email
            association.reviewed_at = timezone.now()
            association.save(update_fields=['review_status', 'reviewed_by', 'reviewed_at'])
        return Response({'id': association.id, 'review_status': decision})

    system = request.query_params.get('system', '').strip()
    facility = request.query_params.get('facility', '').strip()
    sort = request.query_params.get('sort', 'system_asc')
    if len(system) > 255 or len(facility) > 255:
        return Response({'detail': 'Filter is too long.'}, status=status.HTTP_400_BAD_REQUEST)
    if sort not in ('system_asc', 'system_desc'):
        return Response({'detail': 'Invalid sort.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        page = int(request.query_params.get('page', '1'))
    except (TypeError, ValueError):
        page = 0
    if page < 1:
        return Response({'detail': 'page must be a positive integer.'},
                        status=status.HTTP_400_BAD_REQUEST)

    associations = SourceSystemFacility.objects.all()
    if system:
        associations = associations.filter(source_vocabulary_id=system)
    if facility:
        associations = associations.filter(facility_name__icontains=facility)
    # Include exact custom systems with source codes even if the asynchronous
    # facility-registry replay has not produced an association for them yet.
    systems = set(associations.order_by().values_list(
        'source_vocabulary_id', flat=True,
    ).distinct())
    if not facility:
        custom_systems = SourceCodeConceptMapping.objects.filter(
            Q(source_vocabulary_id__startswith='urn:oid:')
            | Q(source_vocabulary_id__startswith='http://open.epic.com/')
            | Q(source_vocabulary_id__startswith='https://open.epic.com/')
            | Q(source_vocabulary_id__startswith='http://fhir.cerner.com/')
            | Q(source_vocabulary_id__startswith='https://fhir.cerner.com/')
        )
        if system:
            custom_systems = SourceCodeConceptMapping.objects.filter(
                source_vocabulary_id=system,
            )
        systems.update(custom_systems.order_by().values_list(
            'source_vocabulary_id', flat=True,
        ).distinct())
    if system:
        systems.intersection_update({system})
    ordered_systems = sorted(systems, key=lambda value: (value.casefold(), value),
                             reverse=sort == 'system_desc')
    total = len(ordered_systems)
    start = (page - 1) * PAGE_SIZE
    page_systems = ordered_systems[start:start + PAGE_SIZE]
    facilities = associations.filter(source_vocabulary_id__in=page_systems).order_by(
        'source_vocabulary_id', 'facility_name', 'id',
    ).values(
        'id', 'source_vocabulary_id', 'facility_name', 'facility_id', 'level',
        'confidence', 'attribution_method', 'provenance', 'review_status',
        'reviewed_by', 'reviewed_at',
    )
    results = [{'source_vocabulary_id': value, 'facilities': []}
               for value in page_systems]
    by_system = {row['source_vocabulary_id']: row for row in results}
    for facility_row in facilities:
        by_system[facility_row['source_vocabulary_id']]['facilities'].append(facility_row)

    receipt = HospitalCodeImport.objects.order_by('-created_at', '-id').values(
        'artifact_identity', 'outcome', 'expected_rows', 'completed_at', 'failure_reason',
    ).first()
    if receipt:
        receipt['failure_reason'] = receipt['failure_reason'][:300]

    diagnostics = None
    if system:
        source_codes = SourceCodeConceptMapping.objects.filter(source_vocabulary_id=system)
        diagnostics = {
            'source_code_rows': source_codes.count(),
            'codes_with_facility_evidence': source_codes.filter(
                source_metadata__facilities__0__isnull=False,
            ).count(),
        }

    return Response({
        'results': results, 'total': total, 'page': page,
        'page_size': PAGE_SIZE, 'sort': sort,
        'association_total': associations.count(), 'seed_import': receipt,
        'system_diagnostics': diagnostics,
    })
