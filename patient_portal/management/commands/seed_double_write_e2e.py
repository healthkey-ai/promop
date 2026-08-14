"""Fixtures for the double-write end-to-end suite.

The suite drives the real HTTP surface — a browser-shaped client posting
clinical rows with a user's own credential — so it needs identities that
exercise each branch of `can_write_patient`: a patient writing their own
record, a second patient they must not reach, a clinician holding a grant over
the first patient only, and a staff operator.

Idempotent: running it twice leaves the same rows and re-prints the same ids,
so a suite can seed on every run without accumulating fixtures.

Refuses to run against a database that looks populated unless `--force` is
given. These identities have known passwords; creating them next to real
patient data would be handing out credentials.
"""
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from omop_core.models import (
    Concept,
    ConceptClass,
    Domain,
    GroupAccess,
    Organization,
    PatientRecord,
    PatientGroup,
    PatientGroupMembership,
    Person,
    Vocabulary,
)
from patient_portal.models import Identity, PatientUser

# The suite signs in with these, so they are fixed rather than generated.
PASSWORD = 'e2e-double-write'

# Firebase is the issuer in every deployed environment; the fixtures mirror its
# shape so `find_or_create` is exercised with a realistic (issuer, sub) pair.
ISSUER = 'https://securetoken.google.com/healthkey-e2e'

PATIENT_SUB = 'e2e-patient-a'
OTHER_SUB = 'e2e-patient-b'
CLINICIAN_SUB = 'e2e-clinician'
ORG_CLINICIAN_SUB = 'e2e-org-clinician'
STAFF_SUB = 'e2e-staff'

PATIENT_PERSON_ID = 9900001
OTHER_PERSON_ID = 9900002

# Concepts the ECOG payload references. A create fails on the foreign key when
# the vocabulary has not been loaded, which is the normal state of a fresh
# container, so the suite cannot assume Athena data is present.
CONCEPTS = [
    (36306034, 'ECOG Performance Status score', 'Observation', 'LOINC', '89247-1'),
    (4193888, 'Patient reported', 'Type Concept', 'SNOMED', 'HK-PATIENT-REPORTED'),
]


class Command(BaseCommand):
    help = 'Create the identities and concepts the double-write e2e suite needs.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force', action='store_true',
            help='Seed even when the database already holds unrelated persons.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        self._guard(options['force'])

        for concept_id, name, domain, vocab, code in CONCEPTS:
            self._concept(concept_id, name, domain, vocab, code)

        patient_person = self._person(PATIENT_PERSON_ID)
        other_person = self._person(OTHER_PERSON_ID)

        patient = self._identity(PATIENT_SUB, 'patient-a@e2e.invalid')
        other = self._identity(OTHER_SUB, 'patient-b@e2e.invalid')
        clinician = self._identity(CLINICIAN_SUB, 'clinician@e2e.invalid')
        org_clinician = self._identity(ORG_CLINICIAN_SUB, 'org-clinician@e2e.invalid')
        staff = self._identity(STAFF_SUB, 'staff@e2e.invalid', is_staff=True)

        PatientUser.objects.get_or_create(identity=patient, defaults={'person': patient_person})
        PatientUser.objects.get_or_create(identity=other, defaults={'person': other_person})

        # The clinician reaches patient A through a group, and only patient A.
        # Patient B is deliberately left out of it: the 403 that produces is the
        # point of the fixture.
        org, _ = Organization.objects.get_or_create(
            name='E2E Clinic', defaults={'slug': 'e2e-clinic'},
        )
        group, _ = PatientGroup.objects.get_or_create(
            organization=org, name='E2E Cohort', defaults={'slug': 'e2e-cohort'},
        )
        PatientGroupMembership.objects.get_or_create(
            group=group, person_id=PATIENT_PERSON_ID,
        )
        GroupAccess.objects.get_or_create(
            identity=clinician, group=group, defaults={'role': 'doctor'},
        )

        # A second clinician reaching the same patient through the organization
        # rather than the group. The two paths are separate grants and were
        # honoured inconsistently — an org grant permitted a write its holder
        # could not read back — so the suite exercises both.
        PatientRecord.objects.get_or_create(
            person=patient_person, defaults={'organization': org},
        )
        GroupAccess.objects.get_or_create(
            identity=org_clinician, org=org, defaults={'role': 'doctor'},
        )

        self.stdout.write(json.dumps({
            'password': PASSWORD,
            'issuer': ISSUER,
            'patient': {'uid': patient.uid, 'sub': PATIENT_SUB, 'person_id': patient_person.person_id},
            'other': {'uid': other.uid, 'sub': OTHER_SUB, 'person_id': other_person.person_id},
            'clinician': {'uid': clinician.uid, 'sub': CLINICIAN_SUB},
            'orgClinician': {'uid': org_clinician.uid, 'sub': ORG_CLINICIAN_SUB},
            'staff': {'uid': staff.uid, 'sub': STAFF_SUB},
        }, indent=2))

    def _guard(self, force):
        if force:
            return
        stray = Person.objects.exclude(
            person_id__in=[PATIENT_PERSON_ID, OTHER_PERSON_ID],
        ).exists()
        if stray:
            raise CommandError(
                'This database already holds persons that are not e2e fixtures. '
                'These identities have published passwords — pass --force only if '
                'you are certain this is a throwaway database.'
            )

    def _concept(self, concept_id, name, domain_id, vocabulary_id, code):
        domain, _ = Domain.objects.get_or_create(
            domain_id=domain_id, defaults={'domain_name': domain_id, 'domain_concept_id': 0},
        )
        vocabulary, _ = Vocabulary.objects.get_or_create(
            vocabulary_id=vocabulary_id,
            defaults={'vocabulary_name': vocabulary_id, 'vocabulary_concept_id': 0},
        )
        concept_class, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Undefined',
            defaults={'concept_class_name': 'Undefined', 'concept_class_concept_id': 0},
        )
        Concept.objects.update_or_create(
            concept_id=concept_id,
            defaults={
                'concept_name': name,
                'domain': domain,
                'vocabulary': vocabulary,
                'concept_class': concept_class,
                'standard_concept': 'S',
                'concept_code': code,
                'valid_start_date': '1970-01-01',
                'valid_end_date': '2099-12-31',
                'source': 'HealthKey',
            },
        )

    def _person(self, person_id):
        person, _ = Person.objects.get_or_create(
            person_id=person_id, defaults={'year_of_birth': 1980},
        )
        return person

    def _identity(self, sub, email, is_staff=False):
        identity, created = Identity.objects.get_or_create(
            issuer=ISSUER, sub=sub,
            defaults={'email': email, 'is_staff': is_staff},
        )
        if not created and identity.is_staff != is_staff:
            identity.is_staff = is_staff
            identity.save(update_fields=['is_staff'])
        # Always reset: a rerun must leave the documented password working even
        # if something else changed it.
        identity.set_password(PASSWORD)
        identity.save(update_fields=['password'])
        return identity
