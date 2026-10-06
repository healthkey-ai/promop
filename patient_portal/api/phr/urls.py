from django.urls import path

from .entries import (
    ConditionEntryView,
    MedicationNoteView,
    MedicationsCreateView,
    OwnMedicationView,
    ProcedureEntryView,
)
from .statements import MedicationStatementView, TherapyReasonView

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
    path('medications/add/', MedicationsCreateView.as_view(), name='phr-medication-add'),
    path('medications/<str:medication_id>/', MedicationDetailView.as_view(), name='phr-medication'),
    path('medications/<str:item_id>/statement/', MedicationStatementView.as_view(), name='phr-medication-statement'),
    path('medications/<str:item_id>/note/', MedicationNoteView.as_view(), name='phr-medication-note'),
    path('medications/<str:item_id>/own/', OwnMedicationView.as_view(), name='phr-medication-own'),
    path('conditions/', ConditionEntryView.as_view(), name='phr-condition-add'),
    path('conditions/<str:item_id>/', ConditionEntryView.as_view(), name='phr-condition'),
    path('procedures/', ProceduresView.as_view(), name='phr-procedures'),
    path('procedures/add/', ProcedureEntryView.as_view(), name='phr-procedure-add'),
    path('procedures/<str:item_id>/', ProcedureEntryView.as_view(), name='phr-procedure'),
    path('genetics/', GeneticsView.as_view(), name='phr-genetics'),
    path('therapy/', TherapyView.as_view(), name='phr-therapy'),
    path('therapy/<str:item_id>/reason/', TherapyReasonView.as_view(), name='phr-therapy-reason'),
    path('whats-new/', WhatsNewView.as_view(), name='phr-whats-new'),
    path('imaging/', ImagingView.as_view(), name='phr-imaging'),
]
