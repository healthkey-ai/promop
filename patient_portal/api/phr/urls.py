from django.urls import path

from .views import (
    AboutView,
    DiagnosesView,
    GeneticsView,
    LabHistoryView,
    LabsView,
    MedicationDetailView,
    MedicationsView,
    ProceduresView,
    StatusView,
    TherapyView,
    ImagingView,
    WhatsNewView,
)

urlpatterns = [
    path('status/', StatusView.as_view(), name='phr-status'),
    path('about/', AboutView.as_view(), name='phr-about'),
    path('diagnoses/', DiagnosesView.as_view(), name='phr-diagnoses'),
    path('labs/', LabsView.as_view(), name='phr-labs'),
    path('labs/<int:test_id>/', LabHistoryView.as_view(), name='phr-lab-history'),
    path('medications/', MedicationsView.as_view(), name='phr-medications'),
    path('medications/<str:medication_id>/', MedicationDetailView.as_view(), name='phr-medication'),
    path('procedures/', ProceduresView.as_view(), name='phr-procedures'),
    path('genetics/', GeneticsView.as_view(), name='phr-genetics'),
    path('therapy/', TherapyView.as_view(), name='phr-therapy'),
    path('whats-new/', WhatsNewView.as_view(), name='phr-whats-new'),
    path('imaging/', ImagingView.as_view(), name='phr-imaging'),
]
