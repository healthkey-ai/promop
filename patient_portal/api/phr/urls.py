from django.urls import path

from .views import AboutView, DiagnosesView, StatusView

urlpatterns = [
    path('status/', StatusView.as_view(), name='phr-status'),
    path('about/', AboutView.as_view(), name='phr-about'),
    path('diagnoses/', DiagnosesView.as_view(), name='phr-diagnoses'),
]
