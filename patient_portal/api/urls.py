from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    CurrentUserViewSet, PatientInfoViewSet, login_view, logout_view, auth_test,
    # OMOP clinical event ViewSets
    ConditionOccurrenceViewSet, DrugExposureViewSet, MeasurementViewSet,
    ObservationViewSet, ProcedureOccurrenceViewSet, EpisodeViewSet, EpisodeEventViewSet,
    # Document storage
    PatientDocumentViewSet,
    # Clinical trial enrollment tracker (metadata from EXACT)
    PatientTrialEnrollmentViewSet,
    # Patient surveys
    SurveyViewSet, PatientSurveyResponseViewSet,
    # Controlled vocabulary
    vocabulary_list,
)

router = DefaultRouter()

# Core PatientInfo
router.register(r'user', CurrentUserViewSet, basename='user')
router.register(r'patient-info', PatientInfoViewSet, basename='patient-info')

# OMOP clinical event tables
# Filter by person: /api/conditions/?person_id=42
router.register(r'conditions', ConditionOccurrenceViewSet, basename='conditions')
router.register(r'drug-exposures', DrugExposureViewSet, basename='drug-exposures')
router.register(r'measurements', MeasurementViewSet, basename='measurements')
router.register(r'observations', ObservationViewSet, basename='observations')
router.register(r'procedures', ProcedureOccurrenceViewSet, basename='procedures')
router.register(r'episodes', EpisodeViewSet, basename='episodes')
router.register(r'episode-events', EpisodeEventViewSet, basename='episode-events')

# Document storage (no OMOP equivalent)
router.register(r'documents', PatientDocumentViewSet, basename='documents')

# Clinical trial enrollment status tracker (trial metadata from EXACT)
router.register(r'trial-enrollments', PatientTrialEnrollmentViewSet, basename='trial-enrollments')

# Patient surveys
router.register(r'surveys', SurveyViewSet, basename='surveys')
router.register(r'survey-responses', PatientSurveyResponseViewSet, basename='survey-responses')

urlpatterns = [
    path('', include(router.urls)),
    path('auth/login/', login_view, name='login'),
    path('auth/logout/', logout_view, name='logout'),
    path('auth/test/', auth_test, name='auth_test'),
    path('vocabularies/<str:model_name>/', vocabulary_list, name='vocabulary-list'),
]
