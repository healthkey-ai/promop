from django.urls import path

from .sync import SyncView
from .views import MeasurementDetailView, ResultsSummaryView, ValuesView, VisitDeleteView

urlpatterns = [
    path('summary/', ResultsSummaryView.as_view(), name='lab-results-summary'),
    path('values/', ValuesView.as_view(), name='lab-results-values'),
    path('measurements/<int:measurement_id>/', MeasurementDetailView.as_view(), name='measurement-detail'),
    path('visits/<int:visit_id>/', VisitDeleteView.as_view(), name='visit-delete'),
    path('sync/', SyncView.as_view(), name='lab-results-sync'),
]
