"""A stage reads as a stage, not as a SNOMED display (#2618)."""
import pytest

from omop_core.models import Measurement, PatientRecord
from omop_core.services.patient_record_service import _get_staging_data
from omop_core.services.stage_labels import normalise_stage_label
from tests.factories import (
    ConceptFactory, MeasurementFactory, PatientRecordFactory, PersonFactory,
    VocabularyFactory,
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
    ('Stage 4 (qualifier value) (finding)', 'Stage IV'),
    ('Stage 0 (qualifier value)', 'Stage 0'),
    ('american joint committee on cancer stage ia (qualifier value)', 'Stage IA'),
    ('American Joint Committee on Cancer stage IA (qualifier val', 'Stage IA'),
    ('stage iiia', 'Stage IIIA'),
    ('Stage 2 A (qualifier value)', 'Stage IIA'),
])
def test_a_stage_value_has_a_reviewed_label(stored, reviewed):
    assert normalise_stage_label(stored) == reviewed


@pytest.mark.parametrize('unchanged', ['Stage III/IV', 'Stage II-III', 'Stage X'])
def test_a_range_or_unknown_stage_is_not_truncated(unchanged):
    assert normalise_stage_label(unchanged) == unchanged


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


def test_a_refresh_does_not_restore_the_semantic_tag():
    """A source that writes after the staging extractor must not undo the fix."""
    from omop_core.models import FieldConceptMapping
    from omop_core.services.patient_record_service import refresh_patient_record

    loinc = VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC')
    concept = ConceptFactory(concept_code='21908-9', concept_name='Stage group', vocabulary=loinc)
    record = PatientRecordFactory()
    MeasurementFactory(person=record.person, measurement_concept=concept,
                       measurement_source_value='21908-9',
                       value_as_string='Stage 4 (qualifier value)')
    FieldConceptMapping.objects.create(
        field_name='stage', concept=concept, omop_table='measurement',
        status='approved', source_value='21908-9',
    )

    refresh_patient_record(record.person)

    record.refresh_from_db()
    assert record.stage == 'Stage IV'


def test_the_migration_normalises_stored_values_and_is_idempotent():
    """The deployed rows are rewritten by a frozen copy; run it twice."""
    from omop_core.services.stage_labels import normalise_stage_label
    from tests.migration_utils import run_data_migration

    seen = {}
    for value in ('Stage 4 (qualifier value)', 'stage iv', 'Stage 2A', 'Stage 2 A',
                  'American Joint Committee on Cancer stage IA (qualifier val',
                  'ISS II', 'Rai 0', 'III', 'Stage 1 (qualifier value)'):
        record = PatientRecordFactory(stage=value)
        seen[record.pk] = value

    run_data_migration('0344_normalise_stage_labels', 'normalise_stages')
    by_value = {value: pk for pk, value in seen.items()}
    after_first = {r.pk: r.stage for r in PatientRecord.objects.filter(pk__in=seen)}
    assert after_first[by_value['Stage 4 (qualifier value)']] == 'Stage IV'
    assert after_first[by_value['stage iv']] == 'Stage IV'
    assert after_first[by_value['Stage 2A']] == 'Stage IIA'
    assert after_first[by_value['Stage 2 A']] == 'Stage IIA'
    assert after_first[by_value['American Joint Committee on Cancer stage IA (qualifier val']] == 'Stage IA'
    assert after_first[by_value['Stage 1 (qualifier value)']] == 'Stage I'
    # Other staging systems and already-plain roman values are untouched.
    for value in ('ISS II', 'Rai 0', 'III'):
        assert after_first[by_value[value]] == value

    run_data_migration('0344_normalise_stage_labels', 'normalise_stages')
    after_second = {r.pk: r.stage for r in PatientRecord.objects.filter(pk__in=seen)}
    assert after_second == after_first
    # The service and the frozen copy agree on every stored value.
    for pk, value in after_first.items():
        assert normalise_stage_label(value) == value


def test_a_linked_stage_fact_reads_as_the_reviewed_label():
    """The PHR card and its export overlay the linked fact, not record.stage."""
    from types import SimpleNamespace

    from patient_portal.api.phr.views import _apply_cancer_details

    row = SimpleNamespace(pk=1)
    out = _apply_cancer_details(
        {}, {'stage': ('Stage 4 (qualifier value)', row)},
        {(type(row), row.pk): {'kind': 'record'}}, 'BC',
    )

    assert out['stage']['value'] == 'Stage IV'


def test_a_pending_stage_edit_survives_the_refresh():
    """A reader's stage edit wins over the derived fact, normalisation included."""
    from omop_core.services.patient_record_service import refresh_patient_record

    loinc = VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC')
    concept = ConceptFactory(concept_code='21908-9', concept_name='Stage group', vocabulary=loinc)
    record = PatientRecordFactory(stage='Stage II A', user_edited_fields=['stage'])
    MeasurementFactory(person=record.person, measurement_concept=concept,
                       measurement_source_value='21908-9',
                       value_as_string='Stage 4 (qualifier value)')

    refresh_patient_record(record.person)

    record.refresh_from_db()
    assert record.stage == 'Stage II A'
