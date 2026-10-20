"""`prior_therapy` must not be manufactured from missing data (#1925).

`None` means "no evidence either way"; `0` is the assertion "no prior therapy".
Folding the first into the second makes a matcher compare a clinical claim where
the record only lacks information.
"""
import pytest

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
