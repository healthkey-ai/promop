from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    CurrentUserViewSet, PatientRecordViewSet,
    PatientRecordV1ViewSet, login_view, logout_view, auth_test,
    change_password,
    PersonViewSet,
    ConditionOccurrenceViewSet, DrugExposureViewSet, MeasurementViewSet,
    ObservationViewSet, ProcedureOccurrenceViewSet, EpisodeViewSet, EpisodeEventViewSet,
    PatientDocumentViewSet,
    PatientTrialEnrollmentViewSet,
    ImmunizationListViewSet, AllergyListViewSet,
    SurveyViewSet, PatientSurveyResponseViewSet,
    PatientConsentViewSet,
    PatientMessageViewSet,
    vocabulary_list, concept_lookup, concept_search, concept_list,
    concept_ancestors, concept_descendants, concept_graph_batch,
    concept_synonyms, concept_synonym_search, concept_replacement,
    vocab_release_list, vocab_release_detail, vocab_release_latest,
    VocabSnapshotView,
    org_disease_stats,
    InterchangeAgreementViewSet,
)
from .org_views import (
    OrgListCreateView, OrgDetailView,
    OrgInviteView, OrgInvitationListView, OrgInvitationDetailView,
    OrgTrustListCreateView, OrgTrustDetailView,
    OrgAccessListView, OrgAccessDetailView, OrgVocabularyUsageView,
    confirm_invitation, org_invitation_lookup, org_public_info, org_signup_directory,
    OrgPatientSignupView,
)
from .patient_invitations import (
    PatientInviteView, accept_patient_invitation, patient_invitation_lookup,
)
from .patient_signup import PatientSignupView
from .audit_views import AuditEventViewSet
from .representatives import PersonalRepresentativeViewSet
from .password_reset import request_password_reset, reset_password
from .break_glass import break_glass

router = DefaultRouter()

router.register(r'user', CurrentUserViewSet, basename='v1-user')
router.register(r'patient-records', PatientRecordV1ViewSet, basename='v1-patient-records')
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
router.register(r'consents', PatientConsentViewSet, basename='v1-consents')
router.register(r'messages', PatientMessageViewSet, basename='v1-messages')
router.register(r'immunizations', ImmunizationListViewSet, basename='v1-immunizations')
router.register(r'allergies', AllergyListViewSet, basename='v1-allergies')
router.register(r'audit-events', AuditEventViewSet, basename='v1-audit-events')
router.register(r'personal-representatives', PersonalRepresentativeViewSet, basename='v1-personal-representatives')
router.register(r'interchange-agreements', InterchangeAgreementViewSet, basename='v1-interchange-agreements')

urlpatterns = [
    path('', include(router.urls)),
    path('auth/login/', login_view, name='v1-login'),
    path('auth/logout/', logout_view, name='v1-logout'),
    path('auth/change-password/', change_password, name='v1-change-password'),
    path('auth/request-reset/', request_password_reset, name='v1-request-reset'),
    path('auth/reset-password/', reset_password, name='v1-reset-password'),
    path('break-glass/', break_glass, name='v1-break-glass'),
    path('auth/test/', auth_test, name='v1-auth-test'),
    path('patients/signup/', PatientSignupView.as_view(), name='v1-patient-signup'),
    path('patients/<int:person_id>/invite/', PatientInviteView.as_view(), name='v1-patient-invite'),
    path('patient-invitations/lookup/', patient_invitation_lookup, name='v1-patient-invitation-lookup'),
    path('patient-invitations/accept/', accept_patient_invitation, name='v1-patient-invitation-accept'),
    path('vocab-releases/', vocab_release_list, name='v1-vocab-release-list'),
    path('vocab-releases/latest/', vocab_release_latest, name='v1-vocab-release-latest'),
    path('vocab-releases/<int:release_id>/', vocab_release_detail, name='v1-vocab-release-detail'),
    path('vocab-releases/<int:release_id>/snapshot/<str:table>/', VocabSnapshotView.as_view(), name='v1-vocab-snapshot'),
    path('vocab-releases/latest/snapshot/<str:table>/', VocabSnapshotView.as_view(), {'release_id': None}, name='v1-vocab-snapshot-latest'),
    path('vocabularies/<str:model_name>/', vocabulary_list, name='v1-vocabulary-list'),
    path('concepts/lookup/', concept_lookup, name='v1-concept-lookup'),
    path('concepts/search/', concept_search, name='v1-concept-search'),
    path('concepts/synonyms/', concept_synonym_search, name='v1-concept-synonym-search'),
    path('concepts/graph/', concept_graph_batch, name='v1-concept-graph-batch'),
    path('concepts/<int:concept_id>/ancestors/', concept_ancestors, name='v1-concept-ancestors'),
    path('concepts/<int:concept_id>/descendants/', concept_descendants, name='v1-concept-descendants'),
    path('concepts/<int:concept_id>/synonyms/', concept_synonyms, name='v1-concept-synonyms'),
    path('concepts/<int:concept_id>/replacement/', concept_replacement, name='v1-concept-replacement'),
    path('concepts/', concept_list, name='v1-concept-list'),
    path('stats/org-disease/', org_disease_stats, name='v1-stats-org-disease'),
    path('orgs/', OrgListCreateView.as_view(), name='v1-org-list'),
    path('orgs/confirm-invitation/', confirm_invitation, name='v1-org-confirm-invitation'),
    path('orgs/invitation-lookup/', org_invitation_lookup, name='v1-org-invitation-lookup'),
    # Must precede orgs/<slug>/ — "signup-directory" is itself a valid slug.
    path('orgs/signup-directory/', org_signup_directory, name='v1-org-signup-directory'),
    path('orgs/<slug:slug>/', OrgDetailView.as_view(), name='v1-org-detail'),
    path('orgs/<slug:slug>/invite/', OrgInviteView.as_view(), name='v1-org-invite'),
    path('orgs/<slug:slug>/invitations/', OrgInvitationListView.as_view(), name='v1-org-invitation-list'),
    path('orgs/<slug:slug>/invitations/<int:invitation_id>/', OrgInvitationDetailView.as_view(), name='v1-org-invitation-detail'),
    path('orgs/<slug:slug>/trusts/', OrgTrustListCreateView.as_view(), name='v1-org-trust-list'),
    path('orgs/<slug:slug>/trusts/<int:trust_id>/', OrgTrustDetailView.as_view(), name='v1-org-trust-detail'),
    path('orgs/<slug:slug>/access/', OrgAccessListView.as_view(), name='v1-org-access-list'),
    path('orgs/<slug:slug>/access/<int:access_id>/', OrgAccessDetailView.as_view(), name='v1-org-access-detail'),
    path('orgs/<slug:slug>/vocabulary/', OrgVocabularyUsageView.as_view(), name='v1-org-vocabulary-usage'),
    path('orgs/<slug:slug>/public/', org_public_info, name='v1-org-public-info'),
    path('orgs/<slug:slug>/patient-signup/', OrgPatientSignupView.as_view(), name='v1-org-patient-signup'),
]
