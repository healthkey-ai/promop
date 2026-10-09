"""Which PatientRecord fields an extractor actually populates.

A concept mapping makes a field writable; it does not make anything read the
value back. So "nothing derives this field" is the warning that stops a curator
approving a write into a void — and a warning that fires on a field which *is*
derived teaches people to ignore it.

Counting literal ``data['x'] =`` assignments does exactly that. Roughly two in
five extractors assign through a variable taken from a lookup table, so the
literal count called 24 of BehaviorTab's 27 fields unread when all 24 are
derived. Issue #648 was filed on that mistaken reading.
"""
import pytest

from omop_core.models import PatientRecord
from omop_core.services.patient_record_service import derived_fields

pytestmark = pytest.mark.django_db


class TestTableDrivenFieldsCount:
    @pytest.mark.parametrize('field', [
        'smoking_status',          # _BEHAVIOR_MEASUREMENT_FIELDS, LOINC 72166-2
        'alcohol_use',
        'education_level',
        'marital_status',
        'sleep_hours_per_night',
        'contraceptive_use',       # _ASSERTION_FIELDS
        'consent_capability',
        'substance_use_details',   # _ASSERTION_DETAIL_FIELDS
        'geographic_exposure_risk_details',
    ])
    def test_a_field_named_only_by_a_lookup_table_counts_as_derived(self, field):
        """The case the literal-only count got wrong.

        None of these appears as a literal in an assignment; every one is
        populated from a table the extractors iterate.
        """
        assert field in derived_fields()

    def test_fields_assigned_literally_still_count(self):
        assert {'diagnosis_date', 'death_date'} <= derived_fields()

    def test_a_field_nothing_populates_is_excluded(self):
        # Left out on purpose: these are the genuine gaps, and the warning is
        # worth something only if it stays true for them.
        for field in ('pregnancy_test_result', 'no_active_infection_status'):
            assert field not in derived_fields()


class TestTheEnumerationIsWellFormed:
    def test_every_entry_is_a_real_column(self):
        """The tables carry LOINC codes and units beside field names.

        Returning those would inflate the set with things that are not fields and
        quietly suppress the warning for real gaps.
        """
        columns = {f.name for f in PatientRecord._meta.get_fields()}
        assert derived_fields() <= columns

    def test_it_is_not_trivially_everything(self):
        # A set containing every column would make the warning meaningless, which
        # is the failure mode that matters more than being slightly incomplete.
        columns = {f.name for f in PatientRecord._meta.get_fields()}
        assert derived_fields() < columns

    def test_it_finds_substantially_more_than_the_literals_alone(self):
        import re
        from pathlib import Path

        import omop_core.services.patient_record_service as service

        literals = set(re.findall(
            r"data\[\s*'([a-z_0-9]+)'\s*\]\s*=",
            Path(service.__file__).read_text(),
        ))
        assert len(derived_fields()) > len(literals & set(derived_fields())) + 50
