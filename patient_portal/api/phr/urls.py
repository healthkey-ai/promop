from django.urls import path

from .views import AboutView, DiagnosesView, LabHistoryView, LabsView, StatusView

urlpatterns = [
    path('status/', StatusView.as_view(), name='phr-status'),
    path('about/', AboutView.as_view(), name='phr-about'),
    path('diagnoses/', DiagnosesView.as_view(), name='phr-diagnoses'),
    path('labs/', LabsView.as_view(), name='phr-labs'),
    path('labs/<int:test_id>/', LabHistoryView.as_view(), name='phr-lab-history'),
]
