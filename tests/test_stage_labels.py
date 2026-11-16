"""A stage reads as a stage, not as a SNOMED display (#2618)."""
import pytest

from omop_core.models import Measurement
from omop_core.services.patient_record_service import _get_staging_data
from omop_core.services.stage_labels import normalise_stage_label
from tests.factories import (
    ConceptFactory, MeasurementFactory, PersonFactory, VocabularyFactory,
)

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('stored, reviewed', [
    ('Stage 1 (qualifier value)', 'Stage I'),
    ('Stage 2 (qualifier value)', 'Stage II'),
    ('Stage 3 (qualifier value)', 'Stage III'),
    ('Stage 4 (qualifier value)', 'Stage IV'),
    ('Stage IV', 'Stage IV'),
    ('stage 2', 'Stage II'),
    ('Stage 2A (qualifier value)', 'Stage IIA'),
])
def test_a_stage_value_has_a_reviewed_label(stored, reviewed):
    assert normalise_stage_label(stored) == reviewed


@pytest.mark.parametrize('other', ['ISS II', 'Rai 0', 'T2N1M0', 'II', ''])
def test_other_staging_systems_are_left_alone(other):
    assert normalise_stage_label(other) == other


def test_the_derivation_does_not_store_a_semantic_tag():
    loinc = VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC')
    stage_concept = ConceptFactory(
        concept_name='Stage 4 (qualifier value)', vocabulary=loinc,
        concept_code='258219007',
    )
    concept = ConceptFactory(concept_code='21908-9', concept_name='Stage group', vocabulary=loinc)
    person = PersonFactory()
    MeasurementFactory(person=person, measurement_concept=concept,
                       measurement_source_value='21908-9',
                       value_as_concept=stage_concept)

    assert Measurement.objects.filter(person=person).count() == 1
    assert _get_staging_data(person)['stage'] == 'Stage IV'
