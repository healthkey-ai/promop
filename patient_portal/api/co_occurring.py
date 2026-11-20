"""Other results in the same panel or order as a source code (#2702).

``GET /api/v1/code-mappings/<id>/co-occurring/`` -- how many of the code's
source records name an order, the orders' names, and the other codes resulted
in those orders with each one's current mapping. Read-only, and separate from
the mapping's own GET because it walks patients' inventories: the dialog opens
without waiting for it. See ``omop_core/services/co_occurring_labs.py``.
"""
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from omop_core.models import SourceCodeConceptMapping
from omop_core.services.co_occurring_labs import co_occurring


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def code_mapping_co_occurring(request, mapping_id):
    # Imported late: views.py is the module that routes to this one.
    from patient_portal.api import views

    # Evidence drawn from patients' source records is for the people who
    # curate mappings; a machine reads the mapping row alone, as on its GET.
    if views.is_service_token(request) or not views._can_view_mappings(request.user):
        return Response({'detail': 'Professional access required.'}, status=status.HTTP_403_FORBIDDEN)
    mapping = SourceCodeConceptMapping.objects.filter(pk=mapping_id).first()
    if mapping is None:
        return Response({'detail': 'Mapping not found.'}, status=status.HTTP_404_NOT_FOUND)
    return Response(co_occurring(mapping))
