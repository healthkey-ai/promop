from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    SurveyViewSet, PatientSurveyResponseViewSet,
    PrologSurveyListView,
    CurrentUserViewSet, PatientRecordViewSet,
    PatientRecordV1ViewSet, login_view, logout_view, auth_test,
    change_password,
    PersonViewSet,
    derivation_status,
    ConditionOccurrenceViewSet, DrugExposureViewSet, MeasurementViewSet,
    ObservationViewSet, ProcedureOccurrenceViewSet, EpisodeViewSet, EpisodeEventViewSet,
    TherapyLineViewSet,
    PatientDocumentViewSet,
    PatientTrialEnrollmentViewSet,
    ImmunizationListViewSet, AllergyListViewSet,
    PatientConsentViewSet,
    PatientMessageViewSet,
    vocabulary_list, concept_lookup, concept_search, concept_list,
    therapy_regimen_list, therapy_regimen_detail,
    therapy_component_list, therapy_class_list,
    mapping_stats,
    therapy_regimen_components, therapy_component_classes,
    disease_therapy_regimen_list, disease_therapy_regimen_detail,
    concept_ancestors, concept_descendants, concept_detail, concept_graph_batch,
    concept_synonyms, concept_synonym_search, concept_replacement,
    vocab_release_list, vocab_release_detail, vocab_release_latest,
    VocabSnapshotView,
    org_disease_stats,
    InterchangeAgreementViewSet,
    field_mapping_list, field_mapping_detail, propose_all_mappings,
    code_mapping_list, code_mapping_detail, code_mapping_vocabularies,
    code_mapping_reference,
    code_mapping_suggest,
    code_mapping_lookup,
    code_mapping_accuracy,
    code_mapping_accuracy_dashboard,
    custom_patient_field_list,
    field_synonyms, field_synonym_detail, field_synonyms_batch,
    field_choice_list, field_choice_detail, field_choice_codes,
    field_formula_list, field_formula_detail, field_formula_test,
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
# Authoring a line of therapy, which is an Episode grouping drug exposures rather
# than any single row. See TherapyLineViewSet.
router.register(r'therapy-lines', TherapyLineViewSet, basename='v1-therapy-lines')
router.register(r'documents', PatientDocumentViewSet, basename='v1-documents')
router.register(r'trial-enrollments', PatientTrialEnrollmentViewSet, basename='v1-trial-enrollments')
router.register(r'consents', PatientConsentViewSet, basename='v1-consents')
router.register(r'messages', PatientMessageViewSet, basename='v1-messages')
router.register(r'immunizations', ImmunizationListViewSet, basename='v1-immunizations')
router.register(r'allergies', AllergyListViewSet, basename='v1-allergies')
router.register(r'audit-events', AuditEventViewSet, basename='v1-audit-events')
router.register(r'personal-representatives', PersonalRepresentativeViewSet, basename='v1-personal-representatives')
router.register(r'interchange-agreements', InterchangeAgreementViewSet, basename='v1-interchange-agreements')
router.register(r'surveys', SurveyViewSet, basename='v1-surveys')
router.register(r'survey-responses', PatientSurveyResponseViewSet, basename='v1-survey-responses')

urlpatterns = [
    # The surveys the PROlog runner serves, for the portal's Surveys tab.
    path('prolog-surveys/', PrologSurveyListView.as_view(), name='v1-prolog-surveys'),
    path('', include(router.urls)),
    path('auth/login/', login_view, name='v1-login'),
    path('auth/logout/', logout_view, name='v1-logout'),
    path('auth/change-password/', change_password, name='v1-change-password'),
    path('auth/request-reset/', request_password_reset, name='v1-request-reset'),
    path('auth/reset-password/', reset_password, name='v1-reset-password'),
    path('break-glass/', break_glass, name='v1-break-glass'),
    path('derivation-status/<str:task_id>/', derivation_status,
         name='v1-derivation-status'),
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
    path('concepts/<int:concept_id>/', concept_detail, name='v1-concept-detail'),
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
    path('field-mappings/', field_mapping_list, name='v1-field-mapping-list'),
    path('code-mappings/', code_mapping_list, name='v1-code-mapping-list'),
    path('code-mappings/reference/', code_mapping_reference, name='v1-code-mapping-reference'),
    path('code-mappings/suggest/', code_mapping_suggest, name='v1-code-mapping-suggest'),
    path('code-mappings/lookup/', code_mapping_lookup, name='v1-code-mapping-lookup'),
    path('code-mappings/accuracy/', code_mapping_accuracy, name='v1-code-mapping-accuracy'),
    path('code-mappings/accuracy/dashboard/', code_mapping_accuracy_dashboard, name='v1-code-mapping-accuracy-dashboard'),
    path('code-mappings/vocabularies/', code_mapping_vocabularies, name='v1-code-mapping-vocabularies'),
    path('code-mappings/<int:mapping_id>/', code_mapping_detail, name='v1-code-mapping-detail'),
    path('custom-patient-fields/', custom_patient_field_list, name='v1-custom-patient-field-list'),
    path('field-mappings/propose-all/', propose_all_mappings, name='v1-field-mapping-propose-all'),
    path('field-mappings/<int:pk>/', field_mapping_detail, name='v1-field-mapping-detail'),
    path('field-mappings/<str:field_name>/synonyms/', field_synonyms, name='v1-field-synonyms'),
    path('field-synonyms/<int:pk>/', field_synonym_detail, name='v1-field-synonym-detail'),
    path('field-synonyms/batch/', field_synonyms_batch, name='v1-field-synonyms-batch'),
    path('field-choices/', field_choice_list, name='v1-field-choice-list'),
    path('field-choices/<int:pk>/', field_choice_detail, name='v1-field-choice-detail'),
    path('field-choices/<int:choice_pk>/codes/', field_choice_codes, name='v1-field-choice-codes'),
    path('field-formulas/', field_formula_list, name='v1-field-formula-list'),
    path('field-formulas/test/', field_formula_test, name='v1-field-formula-test'),
    path('field-formulas/<int:pk>/', field_formula_detail, name='v1-field-formula-detail'),
    # Therapy reference endpoints
    path('therapy-regimens/', therapy_regimen_list, name='v1-therapy-regimen-list'),
    path('therapy-regimens/<str:code>/', therapy_regimen_detail, name='v1-therapy-regimen-detail'),
    path('therapy-regimens/<str:regimen_code>/components/', therapy_regimen_components, name='v1-therapy-regimen-components'),
    path('therapy-regimens/<str:regimen_code>/components/<str:component_code>/', therapy_regimen_components, name='v1-therapy-regimen-component-detail'),
    path('therapy-components/', therapy_component_list, name='v1-therapy-component-list'),
    path('therapy-components/<str:component_code>/classes/', therapy_component_classes, name='v1-therapy-component-classes'),
    path('therapy-components/<str:component_code>/classes/<str:class_code>/', therapy_component_classes, name='v1-therapy-component-class-detail'),
    path('therapy-classes/', therapy_class_list, name='v1-therapy-class-list'),
    # Mapping hub
    path('mapping-stats/', mapping_stats, name='v1-mapping-stats'),
    path('disease-therapy-regimens/', disease_therapy_regimen_list, name='v1-disease-therapy-regimen-list'),
    path('disease-therapy-regimens/<int:pk>/', disease_therapy_regimen_detail, name='v1-disease-therapy-regimen-detail'),
]
