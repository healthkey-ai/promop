from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    CurrentUserViewSet, PatientRecordViewSet, login_view, logout_view, auth_test,
    PersonViewSet,
    ConditionOccurrenceViewSet, DrugExposureViewSet, MeasurementViewSet,
    ObservationViewSet, ProcedureOccurrenceViewSet, EpisodeViewSet, EpisodeEventViewSet,
    PatientDocumentViewSet,
    PatientTrialEnrollmentViewSet,
    SurveyViewSet, PatientSurveyResponseViewSet,
    vocabulary_list, concept_lookup,
    org_disease_stats,
)
from .org_views import (
    OrgListCreateView, OrgDetailView,
    OrgInviteView, OrgInvitationListView, OrgInvitationDetailView,
    OrgTrustListCreateView, OrgTrustDetailView,
    OrgAccessListView, OrgAccessDetailView,
    confirm_invitation,
)

router = DefaultRouter()

router.register(r'user', CurrentUserViewSet, basename='v1-user')
router.register(r'patient-records', PatientRecordViewSet, basename='v1-patient-records')
router.register(r'persons', PersonViewSet, basename='v1-persons')
router.register(r'conditions', ConditionOccurrenceViewSet, basename='v1-conditions')
router.register(r'drug-exposures', DrugExposureViewSet, basename='v1-drug-exposures')
router.register(r'measurements', MeasurementViewSet, basename='v1-measurements')
router.register(r'observations', ObservationViewSet, basename='v1-observations')
router.register(r'procedures', ProcedureOccurrenceViewSet, basename='v1-procedures')
router.register(r'episodes', EpisodeViewSet, basename='v1-episodes')
router.register(r'episode-events', EpisodeEventViewSet, basename='v1-episode-events')
router.register(r'documents', PatientDocumentViewSet, basename='v1-documents')
router.register(r'trial-enrollments', PatientTrialEnrollmentViewSet, basename='v1-trial-enrollments')
router.register(r'surveys', SurveyViewSet, basename='v1-surveys')
router.register(r'survey-responses', PatientSurveyResponseViewSet, basename='v1-survey-responses')

urlpatterns = [
    path('', include(router.urls)),
    path('auth/login/', login_view, name='v1-login'),
    path('auth/logout/', logout_view, name='v1-logout'),
    path('auth/test/', auth_test, name='v1-auth-test'),
    path('vocabularies/<str:model_name>/', vocabulary_list, name='v1-vocabulary-list'),
    path('concepts/lookup/', concept_lookup, name='v1-concept-lookup'),
    path('stats/org-disease/', org_disease_stats, name='v1-stats-org-disease'),
    path('orgs/', OrgListCreateView.as_view(), name='v1-org-list'),
    path('orgs/confirm-invitation/', confirm_invitation, name='v1-org-confirm-invitation'),
    path('orgs/<slug:slug>/', OrgDetailView.as_view(), name='v1-org-detail'),
    path('orgs/<slug:slug>/invite/', OrgInviteView.as_view(), name='v1-org-invite'),
    path('orgs/<slug:slug>/invitations/', OrgInvitationListView.as_view(), name='v1-org-invitation-list'),
    path('orgs/<slug:slug>/invitations/<int:invitation_id>/', OrgInvitationDetailView.as_view(), name='v1-org-invitation-detail'),
    path('orgs/<slug:slug>/trusts/', OrgTrustListCreateView.as_view(), name='v1-org-trust-list'),
    path('orgs/<slug:slug>/trusts/<int:trust_id>/', OrgTrustDetailView.as_view(), name='v1-org-trust-detail'),
    path('orgs/<slug:slug>/access/', OrgAccessListView.as_view(), name='v1-org-access-list'),
    path('orgs/<slug:slug>/access/<int:access_id>/', OrgAccessDetailView.as_view(), name='v1-org-access-detail'),
]
