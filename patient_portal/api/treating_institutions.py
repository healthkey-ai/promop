from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from omop_core.services.treating_institutions import institution_directory


class InstitutionSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    label = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    state_code = serializers.CharField()
    country = serializers.CharField()
    designation = serializers.CharField()
    pediatric_only = serializers.BooleanField()
    source_url = serializers.URLField()


class InstitutionDirectorySerializer(serializers.Serializer):
    source_url = serializers.URLField()
    retrieved_on = serializers.DateField()
    institutions = InstitutionSerializer(many=True)


class TreatingInstitutionListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=InstitutionDirectorySerializer)
    def get(self, request):
        return Response(institution_directory())
