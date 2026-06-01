from django.urls import path

from .sync import FhirSyncView

urlpatterns = [
    path('sync/', FhirSyncView.as_view(), name='fhir-sync'),
]
