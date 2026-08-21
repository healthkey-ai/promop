"""Gender, race and ethnicity are correctable, and write a coded answer.

`Person.gender_concept` / `race_concept` / `ethnicity_concept` are standard OMOP
FKs, and derivation reads the concept **before** falling back to `*_source_value`.
Two consequences drive every test here:

  - a correction must write both, or the stale concept keeps winning and the edit
    silently appears not to have taken;
  - when the new value is not a curated answer the concept must be **cleared**,
    not left pointing at the value that was just corrected.

The pickers are curated. OMOP's `Race` holds 1,409 concepts and `Ethnicity` 150
nationality-style entries (`Afghan`, `Albanian`), which is not the question a
clinical form asks. Whatever is sent is still preserved verbatim in the source
value, so curation narrows the coded answer without discarding what was recorded.
"""
import pytest
from rest_framework.test import APIClient

from omop_core.models import Concept, PatientRecord, Person
from omop_core.services.demographics import (
    ETHNICITY_CHOICES, GENDER_CHOICES, RACE_CHOICES, resolve_concept,
    resolve_concept_code,
)
from omop_core.services.patient_record_service import refresh_patient_record
from tests.factories import (
    ConceptFactory, OrganizationFactory, PatientRecordFactory, PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def demographic_vocabularies():
    """The three vocabularies, curated members only — enough to resolve against."""
    for vocab, pairs in (
        ('Gender', GENDER_CHOICES),
        ('Race', RACE_CHOICES),
        ('Ethnicity', ETHNICITY_CHOICES),
    ):
        VocabularyFactory(vocabulary_id=vocab, vocabulary_name=vocab)
        for code, display in pairs:
            ConceptFactory(
                vocabulary_id=vocab, concept_code=code, concept_name=display,
                standard_concept='S',
            )


@pytest.fixture
def staff_client():
    from patient_portal.models import Identity

    user = Identity.objects.create_user(email='demo-staff@test.com', password='x')
    user.is_staff = True
    user.save(update_fields=['is_staff'])
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def person():
    p = PersonFactory()
    PatientRecordFactory(person=p, organization=OrganizationFactory())
    return p


def _patch(client, person, payload):
    return client.patch(
        f'/api/v1/persons/{person.person_id}/', payload, format='json'
    )


class TestResolver:
    @pytest.mark.parametrize('value,expected', [
        ('White', '5'), ('white', '5'), ('caucasian', '5'), ('5', '5'),
        ('Black or African American', '3'), ('african american', '3'),
        ('Asian', '2'), ('American Indian or Alaska Native', '1'),
    ])
    def test_race_spellings_resolve(self, value, expected):
        assert resolve_concept_code('race', value) == expected

    @pytest.mark.parametrize('value,expected', [
        ('Hispanic or Latino', 'Hispanic'), ('latino', 'Hispanic'),
        ('Not Hispanic or Latino', 'Not Hispanic'), ('non-hispanic', 'Not Hispanic'),
    ])
    def test_ethnicity_spellings_resolve(self, value, expected):
        assert resolve_concept_code('ethnicity', value) == expected

    @pytest.mark.parametrize('value,expected', [
        ('F', 'F'), ('female', 'F'), ('Male', 'M'), ('m', 'M'), ('unknown', 'U'),
    ])
    def test_gender_spellings_resolve(self, value, expected):
        assert resolve_concept_code('gender', value) == expected

    def test_an_uncurated_value_resolves_to_nothing(self):
        """Not an error — it means 'recorded, but not one of the coded options'."""
        assert resolve_concept_code('race', 'Anyvak') is None
        assert resolve_concept('race', 'Anyvak') is None

    def test_resolution_is_by_natural_key_not_a_hardcoded_id(self):
        """A concept_id belongs to a vocabulary release; the code is what endures."""
        concept = resolve_concept('race', 'White')
        assert concept.vocabulary_id == 'Race'
        assert concept.concept_code == '5'

    def test_missing_vocabulary_resolves_to_nothing_rather_than_raising(self):
        Concept.objects.filter(vocabulary_id='Race').delete()
        assert resolve_concept('race', 'White') is None


class TestWriting:
    def test_a_correction_writes_both_concept_and_source_value(self, staff_client, person):
        resp = _patch(staff_client, person, {'race': 'White'})

        assert resp.status_code == 200
        person.refresh_from_db()
        assert person.race_source_value == 'White'
        assert person.race_concept.concept_code == '5'

    def test_an_existing_value_is_replaced_not_preserved(self, staff_client, person):
        """The whole point: unlike the fill-if-empty fields, a wrong value must be
        correctable."""
        _patch(staff_client, person, {'gender': 'Male'})

        _patch(staff_client, person, {'gender': 'Female'})

        person.refresh_from_db()
        assert person.gender_source_value == 'Female'
        assert person.gender_concept.concept_code == 'F'

    def test_an_uncurated_value_is_kept_as_text_and_clears_the_concept(self, staff_client, person):
        """Leaving the old concept would let derivation keep reporting the value
        that was just corrected."""
        _patch(staff_client, person, {'race': 'White'})

        _patch(staff_client, person, {'race': 'Anyvak'})

        person.refresh_from_db()
        assert person.race_source_value == 'Anyvak'
        assert person.race_concept_id is None

    def test_clearing_a_value_clears_both(self, staff_client, person):
        _patch(staff_client, person, {'ethnicity': 'Hispanic or Latino'})

        _patch(staff_client, person, {'ethnicity': None})

        person.refresh_from_db()
        assert person.ethnicity_source_value is None
        assert person.ethnicity_concept_id is None

    def test_a_field_not_sent_is_left_alone(self, staff_client, person):
        _patch(staff_client, person, {'race': 'Asian', 'gender': 'Female'})

        _patch(staff_client, person, {'race': 'White'})

        person.refresh_from_db()
        assert person.gender_source_value == 'Female'


class TestProjection:
    def test_the_correction_reaches_the_projection(self, staff_client, person):
        _patch(staff_client, person, {'gender': 'Female', 'race': 'Asian'})

        # refresh_patient_record derives from the instance it is given, so a stale
        # one would report pre-PATCH values. The API path loads Person fresh.
        person.refresh_from_db()
        refresh_patient_record(person)

        pr = PatientRecord.objects.get(person=person)
        assert pr.gender == 'F'
        assert pr.race == 'Asian'

    def test_correcting_a_wrong_gender_actually_changes_the_projection(
        self, staff_client, person
    ):
        """The failure this fixes: derivation reads the concept first, so a
        text-only write left the projection reporting the old value."""
        _patch(staff_client, person, {'gender': 'Male'})
        person.refresh_from_db()
        refresh_patient_record(person)
        assert PatientRecord.objects.get(person=person).gender == 'M'

        _patch(staff_client, person, {'gender': 'Female'})
        person.refresh_from_db()
        refresh_patient_record(person)

        assert PatientRecord.objects.get(person=person).gender == 'F'


class TestDescriptor:
    def test_the_three_fields_are_writable_with_curated_options(self):
        from omop_core.services.write_descriptor import build_writable_field_descriptor

        d = build_writable_field_descriptor()
        for field, count in (('gender', 3), ('race', 5), ('ethnicity', 2)):
            assert d[field]['kind'] == 'profile', field
            assert d[field]['writable'] is True, field
            assert len(d[field]['options']) == count, field
            assert 'fill_if_empty' not in d[field], field

    def test_the_options_are_curated_not_the_whole_vocabulary(self):
        """Race has 1,409 concepts and Ethnicity 150; a picker offers neither."""
        from omop_core.services.write_descriptor import build_writable_field_descriptor

        d = build_writable_field_descriptor()
        assert [o['value'] for o in d['ethnicity']['options']] == [
            'Hispanic or Latino', 'Not Hispanic or Latino',
        ]

    def test_date_of_birth_remains_fill_if_empty(self):
        """Not swept along: overwriting a recorded birth date is a separate call."""
        from omop_core.services.write_descriptor import build_writable_field_descriptor

        entry = build_writable_field_descriptor()['date_of_birth']
        assert entry['writable'] is False
        assert entry['fill_if_empty'] is True
