"""`prior_therapy` must not be manufactured from missing data (#1925).

`None` means "no evidence either way"; `0` is the assertion "no prior therapy".
Folding the first into the second makes a matcher compare a clinical claim where
the record only lacks information.
"""
import pytest

from omop_core.models import PatientRecord
from omop_core.services.patient_matching import MatchingPatient
from omop_core.services.patient_record_service import refresh_patient_record
from tests.factories import OrganizationFactory, PatientRecordFactory, PersonFactory

pytestmark = pytest.mark.django_db


def _record(**kwargs):
    return PatientRecordFactory(
        person=PersonFactory(), organization=OrganizationFactory(), **kwargs)


def test_no_evidence_leaves_the_history_unknown():
    record = _record()
    # A stale 'None' from an earlier save, with no evidence behind it.
    record.prior_therapy = 'None'
    record.therapy_lines_count = None
    record.save()

    record.refresh_from_db()
    assert record.therapy_lines_count is None
    assert record.prior_therapy is None


def test_refresh_clears_a_stale_asserted_none_without_manufacturing_it_again():
    record = _record()
    # Simulate a persisted value manufactured by the old save hook, not a user
    # assertion. Repeated refresh must not resurrect it from absent OMOP data.
    PatientRecord.objects.filter(pk=record.pk).update(
        therapy_lines_count=0, prior_therapy='None')
    for _ in range(2):
        refreshed = refresh_patient_record(record.person)
        refreshed.refresh_from_db()
        assert refreshed.therapy_lines_count is None
        assert refreshed.prior_therapy is None
        assert MatchingPatient(refreshed).prior_therapy is None


@pytest.mark.parametrize('count,prior', [(0, 'None'), (2, 'Two lines')])
def test_refresh_preserves_a_user_reported_count_without_omop_backing(count, prior):
    record = _record(therapy_lines_count=count, user_edited_fields=['therapy_lines_count'])
    for _ in range(2):
        refreshed = refresh_patient_record(record.person)
        refreshed.refresh_from_db()
        assert refreshed.therapy_lines_count == count
        assert refreshed.prior_therapy == prior


def test_unknown_history_does_not_skip_other_computed_fields():
    record = _record()
    record.relapse_count = None
    record.treatment_refractory_status = 'Multi-Refractory'
    record.save()

    record.refresh_from_db()
    assert record.therapy_lines_count is None
    assert record.prior_therapy is None
    assert record.relapse_count == 0
    assert record.treatment_refractory_status == 'Unknown'


def test_explicit_zero_still_means_no_prior_therapy():
    record = _record()
    record.therapy_lines_count = 0
    record.save()

    record.refresh_from_db()
    assert record.therapy_lines_count == 0
    assert record.prior_therapy == 'None'


def test_a_reported_count_survives_a_save():
    record = _record()
    record.therapy_lines_count = 2
    record.save()

    record.refresh_from_db()
    assert record.prior_therapy == 'Two lines'


def test_text_lines_still_bump_the_count():
    record = _record()
    record.first_line_therapy = 'VRd'
    record.save()

    record.refresh_from_db()
    assert record.therapy_lines_count == 1
    assert record.prior_therapy == 'One line'
