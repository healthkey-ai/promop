"""Review answer mappings using the existing mapping-admin authorization."""
from django.core.exceptions import ValidationError as ModelValidationError
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from omop_core.models import FieldChoice, FieldValueConceptMapping
from omop_core.services.field_values import mapping_data, save_mapping


class MappingInput(serializers.ModelSerializer):
    class Meta:
        model = FieldValueConceptMapping
        fields = ['target_concept', 'question_concept', 'role', 'status', 'outcome', 'notes', 'vocabulary_release']


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def field_value_mapping(request, choice_pk):
    from .views import _can_manage_field_mappings
    if not _can_manage_field_mappings(request.user):
        return Response({'detail': 'Organization admin access required.'}, status=403)
    choice = get_object_or_404(FieldChoice, pk=choice_pk)
    mapping = getattr(choice, 'value_mapping', None)
    if request.method == 'GET':
        return Response({'mapping': mapping_data(mapping), 'history':
            list(mapping.revisions.order_by('-revision').values('revision', 'decision', 'created_at')) if mapping else []})
    serializer = MappingInput(mapping, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    try:
        mapping = save_mapping(choice, serializer.validated_data, request.user)
    except ModelValidationError as exc:
        raise serializers.ValidationError(exc.message_dict)
    return Response(mapping_data(mapping))
