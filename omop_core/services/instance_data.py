"""Which kind of data each model holds, for moving data between PRomop instances.

* **system**: what makes this deployment run. Accounts, tokens, sessions,
  tenancy, audit and caches. Never copied.
* **reference**: domain knowledge needed to import patient data, without any
  patient in it. Vocabularies, mappings, lookup lists.
* **patient**: what is known about one person.

``tests/test_instance_data.py`` fails when a model is added without a class,
so a new table forces the decision instead of silently staying behind.
"""
from __future__ import annotations

from django.apps import apps
from django.db.models import Model

SYSTEM: frozenset[str] = frozenset({
    'admin.LogEntry', 'auth.Group', 'auth.Group_permissions', 'auth.Permission',
    'contenttypes.ContentType', 'sessions.Session',
    'oauth2_provider.AccessToken', 'oauth2_provider.Application',
    'oauth2_provider.DeviceGrant', 'oauth2_provider.Grant',
    'oauth2_provider.IDToken', 'oauth2_provider.RefreshToken',
    'patient_portal.AuditEvent', 'patient_portal.BreakGlassGrant',
    'patient_portal.Identity', 'patient_portal.Identity_groups',
    'patient_portal.Identity_user_permissions', 'patient_portal.PasswordHistory',
    'patient_portal.ServiceAccessToken', 'patient_portal.ServiceApplication',
    # Tied to a login account, which is system data.
    'patient_portal.PatientConsent', 'patient_portal.PatientInvitation',
    'patient_portal.PatientMessage', 'patient_portal.PatientUser',
    # Tenancy and access.
    'omop_core.ApplicationOrganization', 'omop_core.GroupAccess',
    'omop_core.InterchangeAgreement', 'omop_core.OrgInvitation', 'omop_core.OrgTrust',
    'omop_core.Organization', 'omop_core.PatientGroup',
    'omop_core.PatientGroupMembership', 'omop_core.PersonalRepresentative',
    'omop_core.FhirConnection', 'omop_core.FhirOauthState',
    # Holds this deployment's own SMART client registration.
    'omop_core.Institution',
    # Bookkeeping and derived caches, rebuilt by their own commands.
    'omop_core.CdmSource', 'omop_core.ConceptEmbedding', 'omop_core.RegimenMappingGap',
    'omop_core.SuggestEmbeddingSnapshot', 'omop_core.SuggestRun',
    'omop_core.VocabularyRelease', 'omop_core.VocabularyVersionHistory',
    # Outreach state, not answers.
    'prolog_surveys.ParticipantMergeCandidate', 'prolog_surveys.SurveyAdministration',
    'prolog_surveys.SurveyContact', 'prolog_surveys.SurveyInvitation',
    # Webhooks are this deployment's integrations, not knowledge about a person.
    # A subscription names where *this* instance sends events and carries its own
    # signing secret; a delivery is an outbox row for one such subscription, and
    # an inbound event is a replay key. Copying any of them to another instance
    # would either duplicate deliveries or hand over a credential.
    'patient_portal.InboundWebhookEvent', 'patient_portal.WebhookDelivery',
    'patient_portal.WebhookSubscription',
})

# Reference data that comes from a versioned external release through its own loader.
# Copying it row by row would bypass the release bookkeeping, and Concept alone
# is millions of rows. copy_reference_data still brings HealthKey-minted concepts,
# because no release has them.
REFERENCE_FROM_RELEASE: dict[str, str] = {
    **dict.fromkeys((
        'omop_core.Concept', 'omop_core.ConceptAncestor', 'omop_core.ConceptClass',
        'omop_core.ConceptRelationship', 'omop_core.ConceptSynonym', 'omop_core.Domain',
        'omop_core.DrugStrength', 'omop_core.Relationship', 'omop_core.SourceToConceptMap',
        'omop_core.Vocabulary',
    ), 'load_athena_vocabularies'),
    **dict.fromkeys((
        'omop_core.UmlsConcept', 'omop_core.UmlsRelease', 'omop_core.UmlsSourceCode',
    ), 'load_umls_release'),
    **dict.fromkeys(('omop_core.LoincClass', 'omop_core.LoincCodeClass'), 'load_loinc_classes'),
    **dict.fromkeys((
        'prolog_surveys.Survey', 'prolog_surveys.SurveyOption',
        'prolog_surveys.SurveyQuestion', 'prolog_surveys.SurveyVersion',
    ), 'load_definitions'),
}

# Hand-curated reference data with no release behind it, so copying is the only way to move it.
_REFERENCE_COPIED_EXPLICIT: frozenset[str] = frozenset({
    'omop_core.CustomPatientField', 'omop_core.FieldChoice', 'omop_core.FieldChoiceCode',
    'omop_core.FieldConceptMapping', 'omop_core.FieldFormula', 'omop_core.FieldSynonym',
    'omop_core.MappingDestinationCandidate',
    'omop_core.SourceCodeConceptMapping', 'omop_core.DiseaseTherapyRegimen',
    'omop_core.TherapyComponentClassLink', 'omop_core.TherapyOutcome',
    'omop_core.TherapyOutcome_diseases', 'omop_core.TherapyRegimenComponent',
    # A lookup list, but with integer codes it cannot extend VocabularyLookup.
    'omop_core.ToxicityGrade',
})

PATIENT: frozenset[str] = frozenset({
    'omop_core.ConditionEra', 'omop_core.ConditionOccurrence', 'omop_core.Death',
    'omop_core.DoseEra', 'omop_core.DrugEra', 'omop_core.DrugExposure',
    'omop_core.Location', 'omop_core.Measurement', 'omop_core.MeasurementOwnership',
    'omop_core.Note', 'omop_core.NoteNlp', 'omop_core.Observation',
    'omop_core.ObservationPeriod', 'omop_core.PatientDocument', 'omop_core.PatientRecord',
    'omop_core.PatientTrialEnrollment', 'omop_core.Person', 'omop_core.PersonLanguageSkill',
    'omop_core.ProcedureOccurrence', 'omop_core.ProvenanceRecord', 'omop_core.RecordRevision',
    'omop_core.Specimen', 'omop_core.SupportiveTherapyCourse', 'omop_core.TrialSearchPreferences',
    'omop_core.VisitDetail', 'omop_core.VisitOccurrence', 'omop_core.WearableUpload',
    # Where care happened. No clinical row on any instance references them yet.
    'omop_core.CareSite', 'omop_core.Provider',
    'omop_oncology.AILineOfTherapySummary', 'omop_oncology.CancerModifier',
    'omop_oncology.Episode', 'omop_oncology.EpisodeEvent', 'omop_oncology.Histology',
    'omop_oncology.StemTable',
    'prolog_surveys.MintedParticipant', 'prolog_surveys.SurveyAnswer',
    'prolog_surveys.SurveyConsent', 'prolog_surveys.SurveyResponse',
    # An address kept beside a response, and the consents given with one:
    # both name the person, so they go where the answers go.
    'prolog_surveys.SurveyCaptureConsent', 'prolog_surveys.SurveyLinkedContact',
})


def lookup_models() -> list[type[Model]]:
    """Every concrete controlled-vocabulary list, all keyed on ``code``."""
    from omop_core.models import VocabularyLookup

    return sorted(
        (m for m in apps.get_models() if issubclass(m, VocabularyLookup)),
        key=lambda m: m._meta.label,
    )


def reference_copied() -> frozenset[str]:
    """Reference labels that copy_reference_data moves."""
    return _REFERENCE_COPIED_EXPLICIT | {m._meta.label for m in lookup_models()}


def reference() -> frozenset[str]:
    """Every reference label, copied or loaded from a release."""
    return reference_copied() | frozenset(REFERENCE_FROM_RELEASE)
