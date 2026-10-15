"""Read-only curator diagnostics for observed hospital source-system names."""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from omop_core.models import HospitalCodeImport, SourceCodeConceptMapping, SourceSystemFacility
from omop_core.services.access import has_professional_access


PAGE_SIZE = 50


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def source_system_facilities(request):
    """List observed system/name associations without asserting hospital ownership."""
    if not (request.user.is_staff or has_professional_access(request.user)):
        return Response({'detail': 'Organization admin access required.'},
                        status=status.HTTP_403_FORBIDDEN)

    system = request.query_params.get('system', '').strip()
    facility = request.query_params.get('facility', '').strip()
    if len(system) > 255 or len(facility) > 255:
        return Response({'detail': 'Filter is too long.'}, status=status.HTTP_400_BAD_REQUEST)
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
    total = associations.count()
    start = (page - 1) * PAGE_SIZE
    results = list(associations.order_by(
        'source_vocabulary_id', 'facility_name', 'id',
    ).values(
        'source_vocabulary_id', 'facility_name', 'facility_id', 'level',
        'confidence', 'attribution_method', 'provenance',
    )[start:start + PAGE_SIZE])

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
        'page_size': PAGE_SIZE, 'seed_import': receipt,
        'system_diagnostics': diagnostics,
    })
