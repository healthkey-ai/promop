"""The packaged API as a host that overrides the therapy-line policy mounts it.

``DenyingHost`` refuses every per-person write and names itself as the source,
so a test can tell whether an endpoint built from ``urlpatterns_for`` inherited
the host's seams or fell back to the packaged "any authenticated caller".
"""
from django.urls import include, path
from rest_framework import status
from rest_framework.response import Response

from omop_core.api.urls import urlpatterns_for
from omop_core.api.views import TherapyLineViewSet


class DenyingHost(TherapyLineViewSet):
    def patient_write_denied(self, request, person):
        if request.headers.get('X-Host-Allow') == 'yes':
            return None
        return Response({'detail': 'host says no'}, status=status.HTTP_403_FORBIDDEN)

    def patient_info_payload(self, record):
        return {'host_shape': record.person_id}

    def attestation_source(self, request):
        return 'host'


urlpatterns = [
    path('federation/', include(urlpatterns_for(DenyingHost))),
]
