"""Closing an account, and withdrawing the permission a record depends on
(patient_portal.erasure, docs/member_erasure_architecture.md)."""
import json

import pytest
from django.core.files.base import ContentFile
from django.core.management import call_command
from rest_framework.test import APIClient

from omop_core.models import PatientDocument, PatientRecord, Person
from omop_core.services.field_write_service import write_person_location
from omop_oncology.models import Episode
from patient_portal import consent as svc
from patient_portal.models import (
    AuditEvent, ConsentEvent, ConsentLedgerEntry, ConsentPurpose, ConsentText, Identity, PatientUser,
)
from prolog_surveys.models import SurveyAnswer, SurveyResponse
from tests.test_consent_v2 import EARLIER
from tests.test_survey_record_mapping import ANSWERS, mapped, patient, submit  # noqa: F401 (fixtures)

pytestmark = pytest.mark.django_db


def purpose(key, **kw):
    p = ConsentPurpose.objects.create(key=key, **kw)
    ConsentText.objects.create(purpose=p, version='1.0', language='en', title=key, body='...', published_at=EARLIER)
    return p


@pytest.fixture
def member(mapped, patient, django_capture_on_commit_callbacks):  # noqa: F811
    """A member who joined (country, year), granted their record's permission
    and answered a questionnaire that wrote their record and a treatment line."""
    record, identity = patient
    write_person_location(record.person, {'country': 'GB'})
    purpose('my_record', erases_record_on_withdrawal=True)
    purpose('updates', kind='contact_preference')
    svc.record_consent(record.person, 'my_record', ConsentEvent.GRANTED, version='1.0')
    svc.record_consent(record.person, 'updates', ConsentEvent.GRANTED, version='1.0')
    submit(mapped, record, ANSWERS, django_capture_on_commit_callbacks)
    record.refresh_from_db()
    assert record.stage == 'III' and Episode.objects.filter(person=record.person).exists()
    client = APIClient()
    client.force_authenticate(identity)
    return {'record': record, 'person': record.person, 'identity': identity, 'client': client}


def withdraw(client, key):
    return client.post('/api/v1/phr/consent/', {'decisions': [{'purpose': key, 'decision': 'withdrawn', 'version': '1.0'}]},
                       format='json')


def erasures():
    """The deletion log: (who asked, the entry's detail), oldest first."""
    return [(e.user_id, e.detail) for e in AuditEvent.objects.filter(method='ERASE').order_by('id')]


def test_a_member_who_answered_a_questionnaire_can_close_their_account(member):
    person_id, identity_id = member['person'].person_id, member['identity'].pk
    ledger = ConsentLedgerEntry.objects.count()

    r = member['client'].delete('/api/v1/patient-records/me/', {'confirm': 'DELETE'}, format='json')

    assert r.status_code == 200
    assert not Person.objects.filter(person_id=person_id).exists()
    assert not PatientRecord.objects.filter(person_id=person_id).exists()
    assert not SurveyResponse.objects.filter(participant_id=person_id).exists()
    assert not SurveyAnswer.objects.exists()
    assert not Episode.objects.filter(person_id=person_id).exists()
    assert not Identity.objects.filter(pk=identity_id).exists()
    # Kept: the permission history in the ledger, and the deletion log naming
    # who, not what.
    assert ConsentLedgerEntry.objects.count() == ledger
    assert erasures() == [(str(identity_id), {'erasure': 'account', 'person_id': person_id, 'identity_id': identity_id})]
    assert AuditEvent.objects.get(method='ERASE').signature_valid()


def test_withdrawing_the_records_permission_deletes_the_health_answers_and_keeps_the_account(member):
    person, organization = member['person'], member['record'].organization

    r = withdraw(member['client'], 'my_record')

    assert r.status_code == 200
    record = PatientRecord.objects.get(person=person)
    assert record.stage in (None, '') and record.diagnosis_date is None
    assert not Episode.objects.filter(person=person).exists()
    assert not SurveyResponse.objects.filter(participant_id=person.person_id).exists()
    # The account, the membership and the sign-up facts stay; so do the
    # member's other permissions and the whole history.
    assert PatientUser.objects.filter(person=person, identity=member['identity']).exists()
    assert record.organization == organization and record.country == 'GB'
    assert svc.has_consent(person, 'updates') is True
    assert [e.decision for e in ConsentEvent.objects.filter(person=person, purpose__key='my_record').order_by('id')] == [
        'granted', 'withdrawn']
    assert erasures() == [(str(member['identity'].pk), {'erasure': 'record', 'person_id': person.person_id, 'purpose': 'my_record'})]
    assert member['client'].get('/api/v1/phr/consent/').status_code == 200


def test_withdrawing_any_other_permission_deletes_nothing(member):
    person = member['person']

    assert withdraw(member['client'], 'updates').status_code == 200

    assert PatientRecord.objects.get(person=person).stage == 'III'
    assert SurveyResponse.objects.filter(participant_id=person.person_id).exists()
    assert erasures() == []


def test_staff_deleting_a_member_who_answered_a_questionnaire(member):
    staff = Identity.objects.create_user(email='staff@example.test', is_staff=True)
    client = APIClient()
    client.force_authenticate(staff)
    person_id = member['person'].person_id

    r = client.delete(f'/api/v1/patient-records/{person_id}/admin-delete/', {'confirm': 'DELETE'}, format='json')

    assert r.status_code == 200
    assert not Person.objects.filter(person_id=person_id).exists()
    assert not SurveyResponse.objects.filter(participant_id=person_id).exists()
    assert not Identity.objects.filter(pk=member['identity'].pk).exists()
    assert erasures() == [(str(staff.pk), {'erasure': 'account', 'person_id': person_id, 'identity_id': member['identity'].pk})]


def test_load_consent_texts_sets_which_purpose_erases_the_record(tmp_path):
    spec = {'purposes': [{'key': 'my_record', 'erases_record_on_withdrawal': True,
                          'texts': [{'version': '1.0', 'language': 'en', 'title': 'My record', 'body': '...'}]},
                         {'key': 'research',
                          'texts': [{'version': '1.0', 'language': 'en', 'title': 'Research', 'body': '...'}]}]}
    path = tmp_path / 'consent.json'
    path.write_text(json.dumps(spec))

    call_command('load_consent_texts', str(path))

    assert dict(ConsentPurpose.objects.values_list('key', 'erases_record_on_withdrawal')) == {
        'my_record': True, 'research': False}


def test_declining_a_granted_record_permission_erases_the_record_too(member):
    """Any answer that ends the grant ends the record, not only "withdrawn"."""
    person = member['person']

    r = member['client'].post('/api/v1/phr/consent/', {'decisions': [
        {'purpose': 'my_record', 'decision': 'declined', 'version': '1.0'}]}, format='json')

    assert r.status_code == 200 and svc.has_consent(person, 'my_record') is False
    assert PatientRecord.objects.get(person=person).stage in (None, '')
    assert not SurveyResponse.objects.filter(participant_id=person.person_id).exists()
    assert [d['erasure'] for _, d in erasures()] == ['record']


def test_a_withdrawal_recorded_by_staff_is_logged_too(member):
    staff = Identity.objects.create_user(email='support@example.test', is_staff=True)

    svc.record_consent(member['person'], 'my_record', ConsentEvent.WITHDRAWN, source=ConsentEvent.STAFF,
                       recorded_by=staff)

    assert not SurveyResponse.objects.filter(participant_id=member['person'].person_id).exists()
    assert erasures() == [(str(staff.pk), {'erasure': 'record', 'person_id': member['person'].person_id,
                                           'purpose': 'my_record'})]


@pytest.mark.parametrize('how', ['close', 'withdraw'])
def test_uploaded_documents_files_are_deleted_once_the_erasure_commits(member, how, settings, tmp_path,
                                                                        django_capture_on_commit_callbacks):
    settings.MEDIA_ROOT = str(tmp_path)
    doc = PatientDocument(person=member['person'], doc_type='FISH')
    doc.file.save('report.pdf', ContentFile(b'%PDF-1.4 the report'), save=True)
    stored = tmp_path / doc.file.name
    assert stored.exists()

    with django_capture_on_commit_callbacks(execute=True):
        if how == 'close':
            r = member['client'].delete('/api/v1/patient-records/me/', {'confirm': 'DELETE'}, format='json')
        else:
            r = withdraw(member['client'], 'my_record')

    assert r.status_code == 200
    assert not PatientDocument.objects.filter(pk=doc.pk).exists()
    assert not stored.exists()


# ------------------------------------------------------------ review round 2


def _token(identity, scope):
    from datetime import timedelta

    from django.utils import timezone
    from oauth2_provider.models import AccessToken, Application

    app = Application.objects.create(
        user=identity, name='a smart app', client_id=f'app-{len(scope)}', client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE, redirect_uris='https://app.example.com/cb',
    )
    return AccessToken.objects.create(user=identity, application=app, token=f'tok-{len(scope)}', scope=scope,
                                      expires=timezone.now() + timedelta(hours=1))


@pytest.mark.parametrize('scope,allowed', [('openid patient/*.read', False), ('openid patient/*.read patient/*.write', True)])
def test_a_token_that_may_only_read_cannot_withdraw(member, scope, allowed):
    client = APIClient()
    # Signed in by a SMART app's token, as OAuth2Authentication leaves the request.
    client.force_authenticate(member['identity'], token=_token(member['identity'], scope))

    assert client.get('/api/v1/phr/consent/').status_code == 200
    r = withdraw(client, 'my_record')

    assert r.status_code == (200 if allowed else 403)
    assert svc.has_consent(member['person'], 'my_record') is (not allowed)
    assert SurveyResponse.objects.filter(participant_id=member['person'].person_id).exists() is (not allowed)


def test_a_back_dated_or_migrated_answer_erases_nothing(member):
    from datetime import timedelta

    from django.utils import timezone

    person = member['person']
    svc.record_consent(person, 'my_record', ConsentEvent.DECLINED, source=ConsentEvent.STAFF,
                       recorded_at=timezone.now() - timedelta(days=400))
    svc.record_consent(person, 'my_record', ConsentEvent.WITHDRAWN, source=ConsentEvent.MIGRATION)

    assert PatientRecord.objects.get(person=person).stage == 'III'
    assert SurveyResponse.objects.filter(participant_id=person.person_id).exists()
    assert erasures() == []


def test_ask_me_later_after_a_grant_ends_it_and_erases(member):
    from patient_portal.models import ConsentPurpose as P

    P.objects.filter(key='my_record').update(answers=P.YES_NO_ASK_LATER)

    svc.record_consent(member['person'], 'my_record', ConsentEvent.ASK_LATER, version='1.0')

    assert PatientRecord.objects.get(person=member['person']).stage in (None, '')
    assert [d['erasure'] for _, d in erasures()] == ['record']


def test_withdrawal_keeps_exactly_the_account_and_permission_tables(member):
    """What a withdrawal keeps and what it deletes, row by row."""
    from omop_core.models import (
        PatientGroup, PatientGroupMembership, PersonalRepresentative, PersonLanguageSkill, ProvenanceRecord,
    )
    from django.contrib.contenttypes.models import ContentType
    from patient_portal.models import PatientConsent, PatientInvitation, PatientMessage
    from tests.factories import ConceptFactory

    person, record = member['person'], member['record']
    user = PatientUser.objects.get(person=person)
    helper = Identity.objects.create_user(email='daughter@example.test')
    group = PatientGroup.objects.create(organization=record.organization, name='Pilot', slug='pilot')
    rule_group = PatientGroup.objects.create(organization=record.organization, name='Relapsed', slug='relapsed')
    PersonalRepresentative.objects.create(representative=helper, person_id=person.person_id, relationship='child')
    PatientGroupMembership.objects.create(group=group, person_id=person.person_id, source='manual')
    PatientGroupMembership.objects.create(group=rule_group, person_id=person.person_id, source='rule')
    PersonLanguageSkill.objects.create(person=person, language_concept=ConceptFactory(), skill_level='speak')
    PatientInvitation.objects.create(person=person, email='p@example.test', token='t-erase',
                                     expires_at=EARLIER)
    PatientConsent.objects.create(patient_user=user, consent_type='research')
    PatientMessage.objects.create(patient_user=user, subject='my chemo', message='nausea after cycle 3')
    on_record = ProvenanceRecord.objects.create(source='PATIENT_SELF', target_patient_id=str(person.person_id),
                                                content_type=ContentType.objects.get_for_model(PatientRecord),
                                                object_id=record.pk)
    on_consent = ProvenanceRecord.objects.create(
        source='PATIENT_SELF', content_type=ContentType.objects.get_for_model(ConsentEvent),
        object_id=ConsentEvent.objects.filter(person=person).first().pk)

    assert withdraw(member['client'], 'my_record').status_code == 200

    pid = person.person_id
    # Access to a record that is gone goes with it: representatives and every
    # group membership, placed by hand or by rule.
    assert not PersonalRepresentative.objects.filter(person_id=pid).exists()
    assert not PatientGroupMembership.objects.filter(person_id=pid).exists()
    assert PersonLanguageSkill.objects.filter(person=person).count() == 1
    assert PatientInvitation.objects.filter(person=person).count() == 1
    assert PatientConsent.objects.filter(patient_user=user).count() == 1
    assert not PatientMessage.objects.filter(patient_user=user).exists()
    assert not ProvenanceRecord.objects.filter(pk=on_record.pk).exists()
    assert ProvenanceRecord.objects.filter(pk=on_consent.pk).exists()


def test_withdrawal_takes_sex_race_ethnicity_and_birth_day_off_the_person_but_keeps_the_year(member):
    from omop_core.models import Person

    person = member['person']
    Person.objects.filter(pk=person.pk).update(
        gender_source_value='F', race_source_value='asian', ethnicity_source_value='hispanic',
        year_of_birth=1980, month_of_birth=4, day_of_birth=9)

    assert withdraw(member['client'], 'my_record').status_code == 200

    person.refresh_from_db()
    assert (person.gender_source_value, person.race_source_value, person.ethnicity_source_value) == (None, None, None)
    assert (person.gender_concept_id, person.race_concept_id, person.ethnicity_concept_id) == (None, None, None)
    assert (person.month_of_birth, person.day_of_birth, person.birth_datetime) == (None, None, None)
    assert person.year_of_birth == 1980
    assert PatientRecord.objects.get(person=person).country == 'GB'


def test_closing_an_account_takes_representatives_and_group_memberships(member):
    from omop_core.models import PatientGroup, PatientGroupMembership, PersonalRepresentative

    pid = member['person'].person_id
    helper = Identity.objects.create_user(email='mum@example.test')
    group = PatientGroup.objects.create(organization=member['record'].organization, name='G', slug='g')
    PersonalRepresentative.objects.create(representative=helper, person_id=pid, relationship='parent')
    PatientGroupMembership.objects.create(group=group, person_id=pid, source='manual')

    member['client'].delete('/api/v1/patient-records/me/', {'confirm': 'DELETE'}, format='json')

    assert not PersonalRepresentative.objects.filter(person_id=pid).exists()
    assert not PatientGroupMembership.objects.filter(person_id=pid).exists()
    assert Identity.objects.filter(pk=helper.pk).exists()  # the representative's own account stays


def test_every_table_a_withdrawal_reaches_is_a_decision():
    """A new table under the person, or under a kept table, fails here until
    it is added to this list or to KEPT_ON_WITHDRAWAL: deleted by default is
    safe for privacy, but should never be by accident."""
    from patient_portal.erasure import withdrawal_tables

    erased = {model._meta.label for model, _ in withdrawal_tables()}

    assert erased == {
        'omop_core.ConditionEra', 'omop_core.ConditionOccurrence', 'omop_core.Death', 'omop_core.DoseEra',
        'omop_core.DrugEra', 'omop_core.DrugExposure', 'omop_core.FhirConnection', 'omop_core.FhirOauthState',
        'omop_core.ImageOccurrence', 'omop_core.Measurement', 'omop_core.Note', 'omop_core.Observation',
        'omop_core.ObservationPeriod', 'omop_core.PatientDocument', 'omop_core.PatientRecord',
        'omop_core.PatientRecordConfidence', 'omop_core.PatientRecordProvisional', 'omop_core.PatientSourceCode',
        'omop_core.PatientSourceCodeUpload', 'omop_core.PatientTrialEnrollment', 'omop_core.ProcedureOccurrence',
        'omop_core.ProposalRejection', 'omop_core.Specimen', 'omop_core.SupportiveTherapyCourse', 'omop_core.TherapyHistoryAttestation',
        'omop_core.TrialSearchPreferences', 'omop_core.VisitDetail', 'omop_core.VisitOccurrence',
        'omop_core.WearableUpload', 'omop_oncology.CancerModifier', 'omop_oncology.Episode',
        'omop_oncology.Histology', 'omop_oncology.StemTable', 'patient_portal.AiExplanation',
        'patient_portal.PatientMessage', 'patient_portal.PatientStatement', 'patient_portal.RecordShare',
        # A phone's permission to add wearable values goes with the record (#2800).
        'patient_portal.WearableSyncToken',
        'prolog_surveys.MintedParticipant', 'prolog_surveys.ParticipantMergeCandidate',
        'prolog_surveys.SurveyInvitation',
    }


def test_closing_an_account_takes_the_provenance_of_what_it_deleted(member):
    from django.contrib.contenttypes.models import ContentType
    from omop_core.models import ProvenanceRecord

    pid = member['person'].person_id
    ProvenanceRecord.objects.create(source='EHR_SYNC', target_patient_id=str(pid),
                                    content_type=ContentType.objects.get_for_model(Episode),
                                    object_id=Episode.objects.filter(person_id=pid).first().pk)

    member['client'].delete('/api/v1/patient-records/me/', {'confirm': 'DELETE'}, format='json')

    assert not ProvenanceRecord.objects.filter(target_patient_id=str(pid)).exists()


def test_an_admin_of_another_organisation_erases_nothing(member):
    from omop_core.models import GroupAccess
    from tests.factories import OrganizationFactory

    admin = Identity.objects.create_user(email='elsewhere@example.test')
    GroupAccess.objects.create(identity=admin, org=OrganizationFactory(), role='org_admin')
    client = APIClient()
    client.force_authenticate(admin)
    pid = member['person'].person_id

    r = client.delete(f'/api/v1/patient-records/{pid}/admin-delete/', {'confirm': 'DELETE'}, format='json')

    assert r.status_code == 403
    assert Person.objects.filter(person_id=pid).exists() and erasures() == []


def test_staff_deleting_a_member_who_is_also_a_provider_keeps_their_sign_in(member):
    from omop_core.models import GroupAccess

    GroupAccess.objects.create(identity=member['identity'], org=member['record'].organization, role='doctor')
    staff = Identity.objects.create_user(email='staff2@example.test', is_staff=True)
    client = APIClient()
    client.force_authenticate(staff)
    pid = member['person'].person_id

    assert client.delete(f'/api/v1/patient-records/{pid}/admin-delete/', {'confirm': 'DELETE'},
                         format='json').status_code == 200
    assert not Person.objects.filter(person_id=pid).exists()
    assert Identity.objects.filter(pk=member['identity'].pk).exists()
    assert erasures() == [(str(staff.pk), {'erasure': 'account', 'person_id': pid})]


def test_a_rolled_back_erasure_keeps_the_files(member, settings, tmp_path, django_capture_on_commit_callbacks):
    from django.db import transaction

    from patient_portal.erasure import erase_record

    settings.MEDIA_ROOT = str(tmp_path)
    doc = PatientDocument(person=member['person'], doc_type='FISH')
    doc.file.save('report.pdf', ContentFile(b'%PDF-1.4'), save=True)

    with django_capture_on_commit_callbacks(execute=True), pytest.raises(RuntimeError):
        with transaction.atomic():
            erase_record(member['person'])
            raise RuntimeError('something after the erasure failed')

    assert (tmp_path / doc.file.name).exists()
    assert PatientDocument.objects.filter(pk=doc.pk).exists() and erasures() == []


def test_a_mapping_job_for_an_erased_response_is_not_an_error():
    import uuid
    from unittest import mock

    from patient_portal import survey_record_mapping as srm

    with mock.patch.object(srm.logger, 'info') as info, mock.patch.object(srm.logger, 'exception') as error:
        srm.run_response(uuid.uuid4())

    assert error.call_count == 0 and 'no longer exists' in info.call_args.args[0]
