"""Therapy fields are authored through episodes, not mapped to a concept.

`first_line_therapy`, `relapse_count`, `line_of_therapy` and 46 others are derived
by regimen detection across many `DrugExposure` and `Episode` rows. No concept
could describe them, because they are not one fact.

They were reported as `unmapped`, which reads as "nothing you can do here" — but
the write path exists and works. A line is an `Episode` grouping the drug exposures
given during it, and derivation reads that back. The descriptor now says so, and
the last test here proves the recipe it publishes is the one that works.
"""
from datetime import date

import pytest

from omop_core.models import Concept, DrugExposure, PatientRecord
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.pk import next_pk
from omop_core.services.write_descriptor import build_writable_field_descriptor
from omop_oncology.models import Episode, EpisodeEvent
from tests.factories import (
    ConceptFactory, OrganizationFactory, PatientRecordFactory, PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db

TREATMENT_REGIMEN = 32531
DRUG_EXPOSURE_FIELD = 1147094


class TestDescriptor:
    def test_therapy_fields_are_authored_not_unmapped(self):
        d = build_writable_field_descriptor()
        for field in ('first_line_therapy', 'second_line_therapy',
                      'later_therapies', 'line_of_therapy', 'relapse_count'):
            assert d[field]['kind'] == 'authored', field

    def test_each_carries_the_recipe_for_authoring_a_line(self):
        entry = build_writable_field_descriptor()['first_line_therapy']

        recipe = entry['authored_via']
        assert recipe['target'] == 'episode'
        assert 'episodes' in recipe['endpoint']
        assert len(recipe['steps']) == 3
        assert any('episode-events' in s for s in recipe['steps'])

    def test_the_recipe_names_the_field_that_asserts_a_regimen(self):
        """Without it the regimen is inferred from the drug set and *_therapy_id
        stays null; with it the regimen is an asserted fact."""
        recipe = build_writable_field_descriptor()['first_line_therapy']['authored_via']

        assert recipe['asserted_regimen_field'] == 'episode_source_concept'

    def test_no_therapy_field_is_counted_as_needing_a_concept_set(self):
        """They need a different design, not a code — #595 must not count them."""
        d = build_writable_field_descriptor()
        needing = [
            f for f, e in d.items()
            if e.get('group') == 'needs-concept-set'
            and f.startswith(('first_line', 'second_line', 'later_'))
        ]
        assert needing == []


class TestTheRecipeActuallyWorks:
    """The published steps, executed. A recipe nobody has run is a guess."""

    @pytest.fixture
    def patient(self):
        person = PersonFactory()
        PatientRecordFactory(person=person, organization=OrganizationFactory())
        return person

    @pytest.fixture
    def concepts(self):
        VocabularyFactory(vocabulary_id='Episode', vocabulary_name='OMOP Episode')
        VocabularyFactory(vocabulary_id='CDM', vocabulary_name='OMOP CDM')
        return {
            'regimen': ConceptFactory(
                concept_id=TREATMENT_REGIMEN, vocabulary_id='Episode',
                concept_code='OMOP4822256', concept_name='Treatment Regimen'),
            'field': ConceptFactory(
                concept_id=DRUG_EXPOSURE_FIELD, vocabulary_id='CDM',
                concept_code='CDM150',
                concept_name='drug_exposure.drug_exposure_id'),
            'type': ConceptFactory(concept_name='EHR', concept_code='EHR'),
            'drug': ConceptFactory(
                concept_name='cyclophosphamide', concept_code='RX-CYCLO'),
        }

    def _author_line(self, person, concepts, number, start, end, drug_name):
        de = DrugExposure.objects.create(
            drug_exposure_id=next_pk(DrugExposure, 'drug_exposure_id'),
            person=person, drug_concept=concepts['drug'],
            drug_exposure_start_date=start, drug_exposure_end_date=end,
            drug_type_concept=concepts['type'], drug_source_value=drug_name,
        )
        episode = Episode.objects.create(
            episode_id=next_pk(Episode, 'episode_id'), person=person,
            episode_concept=concepts['regimen'],
            episode_start_date=start, episode_end_date=end,
            episode_number=number,
            episode_object_concept=concepts['regimen'],
            episode_type_concept=concepts['type'],
        )
        EpisodeEvent.objects.create(
            episode_id=episode.episode_id, event_id=de.drug_exposure_id,
            episode_event_field_concept=concepts['field'],
        )
        return episode

    def test_authoring_a_line_populates_the_first_line_fields(self, patient, concepts):
        self._author_line(patient, concepts, 1,
                          date(2025, 3, 1), date(2025, 6, 1), 'cyclophosphamide')

        refresh_patient_record(patient)

        pr = PatientRecord.objects.get(person=patient)
        assert pr.first_line_therapy is not None
        assert pr.first_line_start_date == date(2025, 3, 1)
        assert pr.first_line_end_date == date(2025, 6, 1)
        assert str(pr.line_of_therapy) == '1'
        assert pr.therapy_lines_count == 1

    def test_a_second_line_lands_in_the_second_line_fields(self, patient, concepts):
        self._author_line(patient, concepts, 1,
                          date(2025, 1, 1), date(2025, 3, 1), 'cyclophosphamide')
        self._author_line(patient, concepts, 2,
                          date(2025, 4, 1), date(2025, 7, 1), 'cyclophosphamide')

        refresh_patient_record(patient)

        pr = PatientRecord.objects.get(person=patient)
        assert pr.second_line_start_date == date(2025, 4, 1)
        assert pr.therapy_lines_count == 2
        assert str(pr.line_of_therapy) == '2'

    def test_removing_the_episode_falls_back_to_the_drugs(self, patient, concepts):
        """The episode groups a line; it is not the only evidence one happened.

        Deleting it leaves the drug exposures behind, and derivation still infers
        a line from them. That is the ARTEMIS-style path the episode path takes
        precedence over — so removing the grouping loses the assertion, not the
        therapy.
        """
        episode = self._author_line(patient, concepts, 1,
                                    date(2025, 3, 1), date(2025, 6, 1), 'cyclo')
        refresh_patient_record(patient)
        assert PatientRecord.objects.get(person=patient).therapy_lines_count == 1

        EpisodeEvent.objects.filter(episode_id=episode.episode_id).delete()
        episode.delete()
        refresh_patient_record(patient)

        assert PatientRecord.objects.get(person=patient).therapy_lines_count == 1

    def test_removing_the_drugs_too_clears_the_projection(self, patient, concepts):
        """Derivation is a projection, not an accumulation."""
        episode = self._author_line(patient, concepts, 1,
                                    date(2025, 3, 1), date(2025, 6, 1), 'cyclo')
        refresh_patient_record(patient)

        EpisodeEvent.objects.filter(episode_id=episode.episode_id).delete()
        episode.delete()
        DrugExposure.objects.filter(person=patient).delete()
        refresh_patient_record(patient)

        pr = PatientRecord.objects.get(person=patient)
        assert pr.therapy_lines_count == 0
        assert pr.first_line_therapy is None
