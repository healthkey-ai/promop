"""The packaged API a host mounts, exercised without patient_portal.

These are the endpoints the PROMOP patient widget calls. They must work in an
installation that has the wheel and no ``patient_portal``: that is exactly the
embedding case, and it is why the serializers, the queries and the viewset live
in ``omop_core.api``.
"""
from django.urls import include, path

urlpatterns = [
    path('federation/', include('omop_core.api.urls')),
]
