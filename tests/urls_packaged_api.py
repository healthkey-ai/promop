"""The packaged API a host mounts, exercised without patient_portal.

These are the endpoints the PROMOP patient widget calls. They must work in an
installation that has the wheel and no ``patient_portal``: that is exactly the
embedding case, and it is why the serializers, the queries and the viewset live
in ``omop_core.api``.
"""
from django.urls import include, path
from omop_core.api.urls import urlpatterns_for
from omop_core.api.views import TherapyHistoryCompletenessViewSet


class TestHostCompletenessViewSet(TherapyHistoryCompletenessViewSet):
    """Explicit authorization supplied by this isolated test host."""

    def patient_write_denied(self, request, person):
        return None

urlpatterns = [
    path('federation/', include(urlpatterns_for(completeness_viewset=TestHostCompletenessViewSet))),
]
