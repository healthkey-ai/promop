"""URLs the PROMOP patient widget calls, mountable by an embedding host.

A host includes this under the same base path it gives the widget's clinical
transport, so the widget's ``/v1/...`` calls resolve here instead of falling
through to a catch-all:

    path('api/v1/federation/', include('omop_core.api.urls'))
"""
from django.urls import path
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.routers import SimpleRouter

from omop_core.api import views


def urlpatterns_for(therapy_line_viewset=None, completeness_viewset=None):
    """The widget's URLs, with an optional host viewset override.

    A host mounts ``include(urlpatterns_for(MyViewSet))``. Without the override it
    gets the packaged viewset, whose only policy is "authenticated" -- suitable
    only when the host has already scoped the request to the caller's record.
    """
    viewset = therapy_line_viewset or views.TherapyLineViewSet
    completeness = completeness_viewset or views.TherapyHistoryCompletenessViewSet
    router = SimpleRouter()
    router.register(r'v1/therapy-lines', viewset, basename='therapy-lines')
    router.register(r'v1/therapy-history/completeness', completeness,
                    basename='therapy-history-completeness')

    read = api_view(['GET'])
    authenticated = permission_classes([IsAuthenticated])

    return [
        path('v1/therapy-outcomes/', read(authenticated(views.therapy_outcomes)),
             name='therapy-outcomes'),
        path('v1/therapy-regimens/', read(authenticated(views.therapy_regimen_list)),
             name='therapy-regimens'),
        path('v1/therapy-regimens/<str:code>/', read(authenticated(views.therapy_regimen_detail)),
             name='therapy-regimen-detail'),
        path('v1/concepts/search/', read(authenticated(views.concept_search)),
             name='concept-search'),
    ] + router.urls


urlpatterns = urlpatterns_for()
