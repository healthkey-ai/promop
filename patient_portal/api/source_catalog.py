"""Read-only publisher terminology search for mapping source codes."""
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from omop_core.services.source_catalog import catalog_response


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def source_catalog(request):
    from .views import _can_manage_field_mappings

    if not _can_manage_field_mappings(request.user):
        return Response({'detail': 'Organization admin access required.'}, status=403)
    vocabulary = request.query_params.get('vocabulary_id', '').strip()
    code = request.query_params.get('code', '').strip()
    query = request.query_params.get('q', '').strip()
    if not vocabulary or len(vocabulary) > 50 or len(code) > 255 or len(query) > 200:
        return Response({'detail': 'Provide a vocabulary and a code or search term of valid length.'}, status=400)
    if query and len(query) < 2:
        return Response({'detail': 'Enter at least two characters to search.'}, status=400)
    return Response(catalog_response(
        vocabulary, code=code, query=query,
        include_retired=request.query_params.get('include_retired') == '1',
    ))
