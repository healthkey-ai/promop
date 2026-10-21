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

router = SimpleRouter()
router.register(r'v1/therapy-lines', views.TherapyLineViewSet, basename='therapy-lines')

_read = api_view(['GET'])
_read_only = permission_classes([IsAuthenticated])

urlpatterns = [
    path('v1/therapy-outcomes/', _read(_read_only(views.therapy_outcomes)),
         name='therapy-outcomes'),
    path('v1/therapy-regimens/', _read(_read_only(views.therapy_regimen_list)),
         name='therapy-regimens'),
    path('v1/therapy-regimens/<str:code>/', _read(_read_only(views.therapy_regimen_detail)),
         name='therapy-regimen-detail'),
    path('v1/concepts/search/', _read(_read_only(views.concept_search)),
         name='concept-search'),
] + router.urls
