"""A record edit never rewrites a result it did not write (#2431).

Dated edits let a correction target any past date, and the projector rewrote
the newest same-date fact in place: an EHR import's value and unit were
replaced and the source result was gone. A changed answer over a foreign fact is
appended (the higher id wins the date); the projector's own row is still
corrected in place so autosave and typo fixes do not accumulate.
"""
from datetime import date

import pytest
from django.utils import timezone

from omop_core.models import Measurement, ProvenanceRecord
from omop_core.services.omop_projection import project_single_value
from tests.factories import ConceptFactory, MeasurementFactory, PersonFactory

pytestmark = pytest.mark.django_db

DAY = date(2026, 10, 1)


@pytest.fixture
def setup():
    person = PersonFactory()
    concept = ConceptFactory(concept_id=3000963, concept_code='718-7', concept_name='Hemoglobin')
    ConceptFactory(concept_id=32817, concept_code='OMOP4976890', concept_name='EHR')
    projection = {'omop_table': 'measurement', 'concept_id': concept.pk, 'source_value': '718-7',
                  'value_kind': 'number', 'unit': 'g/dL', 'type_concept_id': 32817}
    return person, concept, projection


def _import(person, concept, day=DAY, value=8.0):
    return MeasurementFactory(person=person, measurement_concept=concept,
                              measurement_source_value='718-7', measurement_date=day,
                              measurement_type_concept_id=32817, value_as_number=value,
                              unit_source_value='g/dL')


def _values(person):
    return list(Measurement.objects.filter(person=person).order_by('measurement_id')
                .values_list('value_as_number', flat=True))


def test_a_dated_correction_leaves_the_imported_result_and_appends(setup):
    person, concept, projection = setup
    imported = _import(person, concept)

    assert project_single_value(person, 'hemoglobin_g_dl', 12.0, projection, fact_date=DAY)

    imported.refresh_from_db()
    assert float(imported.value_as_number) == 8.0
    assert imported.unit_source_value == 'g/dL'
    # Retired, not rewritten: no reader counts the corrected value any more.
    assert imported.is_erroneous is True
    correction = Measurement.objects.filter(person=person).order_by('-measurement_id').first()
    assert correction.pk > imported.pk and correction.measurement_date == DAY
    assert float(correction.value_as_number) == 12.0
    assert ProvenanceRecord.objects.filter(object_id=correction.pk, source='ADMIN_CORRECTION').exists()
    assert not ProvenanceRecord.objects.filter(object_id=imported.pk).exists()


def test_a_second_correction_rewrites_our_own_row_not_a_new_one(setup):
    person, concept, projection = setup
    _import(person, concept)
    project_single_value(person, 'hemoglobin_g_dl', 12.0, projection, fact_date=DAY)

    project_single_value(person, 'hemoglobin_g_dl', 12.5, projection, fact_date=DAY)

    assert [float(v) for v in _values(person)] == [8.0, 12.5]


def test_an_unchanged_answer_writes_nothing(setup):
    person, concept, projection = setup
    _import(person, concept, value=12.0)

    assert not project_single_value(person, 'hemoglobin_g_dl', 12.0, projection, fact_date=DAY)

    assert [float(v) for v in _values(person)] == [12.0]


def test_today_keeps_the_same_day_rule_from_dev(setup):
    """Only dated (past) corrections are new in #2418; today's in-place update of
    a same-day fact is dev's existing behaviour and is unchanged here."""
    person, concept, projection = setup
    today = timezone.localdate()
    imported = _import(person, concept, day=today)

    project_single_value(person, 'hemoglobin_g_dl', 11.0, projection)

    imported.refresh_from_db()
    assert float(imported.value_as_number) == 11.0
    assert Measurement.objects.filter(person=person).count() == 1


def test_a_patient_self_provenanced_import_is_not_ours_to_rewrite(setup):
    """HealthKit and lab uploads write PATIENT_SELF provenance too; only the
    projector's own reason marks a row it may correct in place."""
    from django.contrib.contenttypes.models import ContentType
    person, concept, projection = setup
    imported = _import(person, concept)
    ProvenanceRecord.objects.create(
        content_type=ContentType.objects.get_for_model(Measurement), object_id=imported.pk,
        source='PATIENT_SELF', source_user_id='healthkit|1', modification_reason='measurement_update')

    project_single_value(person, 'hemoglobin_g_dl', 12.0, projection, fact_date=DAY)

    imported.refresh_from_db()
    assert float(imported.value_as_number) == 8.0 and imported.is_erroneous is True
    assert [float(v) for v in _values(person)] == [8.0, 12.0]


def test_a_corrected_low_hemoglobin_no_longer_meets_crab(setup):
    """The all-history CRAB fallback took min(hemoglobin); an appended correction
    beside a live import kept the import's 8 g/dL counting."""
    from omop_core.services.patient_record_service import _get_mm_specific_data
    person, concept, projection = setup
    _import(person, concept)
    assert _get_mm_specific_data(person).get('meets_crab') is True

    project_single_value(person, 'hemoglobin_g_dl', 12.0, projection, fact_date=DAY)

    assert _get_mm_specific_data(person).get('meets_crab') is None
