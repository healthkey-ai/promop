from django.urls import path

from .sync import FhirSyncView, FhirPatientSyncView, FhirPatientDeleteView

urlpatterns = [
    path('sync/', FhirSyncView.as_view(), name='fhir-sync'),
    path('patient-sync/', FhirPatientSyncView.as_view(), name='fhir-patient-sync'),
    path('patient-delete/', FhirPatientDeleteView.as_view(), name='fhir-patient-delete'),
]
