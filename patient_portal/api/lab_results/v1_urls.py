from django.urls import path

from .sync import SyncView
from .views import MeasurementDetailView, ResultsSummaryView, ValuesView, VisitDeleteView

urlpatterns = [
    path('summary/', ResultsSummaryView.as_view(), name='v1-lab-results-summary'),
    path('values/', ValuesView.as_view(), name='v1-lab-results-values'),
    path('measurements/<int:measurement_id>/', MeasurementDetailView.as_view(), name='v1-measurement-detail'),
    path('visits/<int:visit_id>/', VisitDeleteView.as_view(), name='v1-visit-delete'),
    path('sync/', SyncView.as_view(), name='v1-lab-results-sync'),
]
