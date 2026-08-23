"""The unmapped-lab fallback must not sit on an id a vocabulary can claim.

A placeholder was seeded at concept_id 3000963 named "Generic Lab Measurement",
reasoning that vocabulary_id='None' and concept_code='0' kept it out of LOINC
lookups. That was true and beside the point: Athena owns 3000963 as "Hemoglobin
[Mass/volume] in Blood". Loading a real vocabulary turned every unmapped lab into
a haemoglobin result — 3,773 rows on staging, 116,219 on a dev box — and
derivation keys on the concept, so it projected them as haemoglobin.

Found by running the app: a patient's haemoglobin derived as 0.0 while their real
newest result was 14.361.
"""
from io import StringIO

import pytest
from django.core.management import call_command

from omop_core.models import Measurement
from omop_core.services.mappings import CONCEPT_GENERIC_LAB
from tests.factories import (
    ConceptFactory, MeasurementFactory, PersonFactory, VocabularyFactory,
)

pytestmark = pytest.mark.django_db

STRANDED = 3000963


@pytest.fixture(autouse=True)
def base_concepts():
    """The placeholder and OMOP's sentinel must both exist for the FKs to hold."""
    ConceptFactory(
        concept_id=0, concept_code='0', vocabulary_id='None',
        concept_name='No matching concept',
    )
    ConceptFactory(
        concept_id=STRANDED, concept_code='0', vocabulary_id='None',
        concept_name='Generic Lab Measurement',
    )


def _stranded_row(person, source_value):
    """A row written against the placeholder before the fix."""
    m = MeasurementFactory(person=person, measurement_source_value=source_value)
    Measurement.objects.filter(pk=m.pk).update(measurement_concept_id=STRANDED)
    return m


class TestTheConstant:
    def test_fallback_is_omop_no_matching_concept(self):
        """0 is the only id no vocabulary can redefine."""
        assert CONCEPT_GENERIC_LAB == 0

    def test_no_code_path_writes_the_stranded_id(self):
        """Structural guard: the id must not reappear as a fallback anywhere.

        Checks numeric literals in the parsed source rather than raw text, so
        prose about the incident in a docstring does not register — only a number
        the code would actually use.
        """
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        offenders = []
        for rel in ('patient_portal/api/views.py',
                    'omop_core/services/mappings.py',
                    # The retired seeder's data, now a test-only fixture. It is
                    # where the placeholder was minted, so still worth watching.
                    'omop_core/concept_fixtures.py'):
            tree = ast.parse((root / rel).read_text())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant)
                        and isinstance(node.value, int)
                        and not isinstance(node.value, bool)
                        and node.value == STRANDED):
                    offenders.append(f'{rel}:{node.lineno}')
        assert not offenders, offenders

class TestRepairCommand:
    def test_dry_run_reports_without_writing(self):
        person = PersonFactory()
        row = _stranded_row(person, '9279-1')

        out = StringIO()
        call_command('remap_generic_lab_fallback', stdout=out)

        assert 'Dry run' in out.getvalue()
        row.refresh_from_db()
        assert row.measurement_concept_id == STRANDED

    def test_resolvable_source_value_is_repointed_to_its_own_concept(self):
        VocabularyFactory(vocabulary_id='LOINC')
        target = ConceptFactory(
            concept_code='9279-1', vocabulary_id='LOINC',
            concept_name='Respiratory rate', standard_concept='S',
        )
        person = PersonFactory()
        row = _stranded_row(person, '9279-1')

        call_command('remap_generic_lab_fallback', apply=True, stdout=StringIO())

        row.refresh_from_db()
        assert row.measurement_concept_id == target.concept_id

    def test_unresolvable_source_value_falls_back_to_zero(self):
        """Honestly unmapped beats falsely haemoglobin."""
        person = PersonFactory()
        row = _stranded_row(person, 'NOT-A-LOINC-CODE')

        call_command('remap_generic_lab_fallback', apply=True, stdout=StringIO())

        row.refresh_from_db()
        assert row.measurement_concept_id == 0

    def test_missing_source_value_falls_back_to_zero(self):
        """No source value is also honestly unmapped, not haemoglobin."""
        person = PersonFactory()
        row = _stranded_row(person, None)

        call_command('remap_generic_lab_fallback', apply=True, stdout=StringIO())

        row.refresh_from_db()
        assert row.measurement_concept_id == 0

    def test_a_genuine_haemoglobin_row_is_left_alone(self):
        """Source value 718-7 on that concept really is haemoglobin."""
        person = PersonFactory()
        row = _stranded_row(person, '718-7')

        call_command('remap_generic_lab_fallback', apply=True, stdout=StringIO())

        row.refresh_from_db()
        assert row.measurement_concept_id == STRANDED

    def test_source_value_is_never_destroyed(self):
        """It is what a later vocabulary load needs to resolve the row properly."""
        person = PersonFactory()
        row = _stranded_row(person, 'NOT-A-LOINC-CODE')

        call_command('remap_generic_lab_fallback', apply=True, stdout=StringIO())

        row.refresh_from_db()
        assert row.measurement_source_value == 'NOT-A-LOINC-CODE'

    def test_no_row_is_deleted(self):
        person = PersonFactory()
        _stranded_row(person, '9279-1')
        _stranded_row(person, 'NOT-A-LOINC-CODE')
        before = Measurement.objects.count()

        call_command('remap_generic_lab_fallback', apply=True, stdout=StringIO())

        assert Measurement.objects.count() == before

    def test_running_twice_is_a_no_op(self):
        person = PersonFactory()
        _stranded_row(person, 'NOT-A-LOINC-CODE')
        call_command('remap_generic_lab_fallback', apply=True, stdout=StringIO())

        out = StringIO()
        call_command('remap_generic_lab_fallback', apply=True, stdout=out)

        assert 'No stranded rows' in out.getvalue()

    def test_a_row_named_after_the_concept_is_left_alone(self):
        """Staging holds 773 genuine haemoglobin results whose source value is the
        concept's display name rather than its code. Demoting those to 0 would
        destroy correct data in the course of fixing incorrect data."""
        person = PersonFactory()
        row = _stranded_row(person, 'Generic Lab Measurement')  # == concept name

        call_command('remap_generic_lab_fallback', apply=True, stdout=StringIO())

        row.refresh_from_db()
        assert row.measurement_concept_id == STRANDED

    def test_the_name_match_is_case_insensitive(self):
        person = PersonFactory()
        row = _stranded_row(person, 'generic lab MEASUREMENT')

        call_command('remap_generic_lab_fallback', apply=True, stdout=StringIO())

        row.refresh_from_db()
        assert row.measurement_concept_id == STRANDED

    def test_a_foreign_code_beside_a_named_row_is_still_repaired(self):
        """Protecting the named rows must not protect the mislabelled ones."""
        person = PersonFactory()
        named = _stranded_row(person, 'Generic Lab Measurement')
        foreign = _stranded_row(person, 'fl-transformed-dlbcl')

        call_command('remap_generic_lab_fallback', apply=True, stdout=StringIO())

        named.refresh_from_db(); foreign.refresh_from_db()
        assert named.measurement_concept_id == STRANDED
        assert foreign.measurement_concept_id == 0

    def test_the_analyte_alone_identifies_the_concept(self):
        """A LOINC name reads 'Analyte [Property] ... in System'. Staging holds 757
        genuine haemoglobin results whose source value is just 'Hemoglobin'."""
        from omop_core.models import Concept

        Concept.objects.filter(concept_id=STRANDED).update(
            concept_name='Hemoglobin [Mass/volume] in Blood',
        )
        person = PersonFactory()
        row = _stranded_row(person, 'Hemoglobin')

        call_command('remap_generic_lab_fallback', apply=True, stdout=StringIO())

        row.refresh_from_db()
        assert row.measurement_concept_id == STRANDED
