"""A lookup's Seen bump is provisional until the code's per-patient rows exist (#2123)."""
import pytest

from omop_core.mapping import axis_variants, code_resolution
from omop_core.mapping.code_resolution import _record_proposal
from omop_core.models import PatientSourceCode, SourceCodeConceptMapping
from tests.factories import OrganizationFactory, PatientRecordFactory, PersonFactory

pytestmark = pytest.mark.django_db

LAB = 'urn:oid:1.2.840.114350.1.13.412.2.7.5.737384.134'


def sighting(code='5090', organization=None, provisional=True, vocabulary=LAB):
    token = code_resolution._PROVISIONAL.set(provisional)
    try:
        return _record_proposal(source_vocabulary_id=vocabulary, source_code=code, source_text='beta2 microglob',
                                concept=None, omop_table='measurement', source_system='etl',
                                organization=organization)
    finally:
        code_resolution._PROVISIONAL.reset(token)


def rows(code='5090', n=40, person=None, vocabulary=LAB, table='measurement'):
    return PatientSourceCode.objects.create(person=person or PersonFactory(), source_value=code,
                                            source_vocabulary_id=vocabulary, omop_table=table, occurrence_count=n)


def test_provisional_sightings_count_while_no_patient_rows_exist_then_read_the_rows():
    first = sighting()
    assert first.occurrence_count == 1
    assert sighting().occurrence_count == 2, 'a lookup is the only count before any patient upload'
    rows(n=40)
    again = sighting()
    assert again.occurrence_count == 40, 'with patient rows on the instance a sighting reads them'
    assert again.last_seen is not None and again.last_seen > first.last_seen


def test_a_per_record_sighting_always_counts():
    """FHIR sync resolves once per record and writes no per-patient rows."""
    rows(n=40)
    assert sighting(provisional=False).occurrence_count == 1
    assert sighting(provisional=False).occurrence_count == 2


def test_a_mapping_created_after_its_patients_were_uploaded_starts_at_their_count():
    rows(n=25)
    rows(n=5)
    assert sighting().occurrence_count == 30


def test_rows_on_another_table_or_vocabulary_spelling_do_not_count_as_this_mapping_s():
    rows(n=40, table='observation')
    assert sighting().occurrence_count == 1 and sighting().occurrence_count == 2
    # A vocabulary the rows carry as an OID and the mapping holds under its canonical id.
    oid = 'urn:oid:2.16.840.1.113883.6.96'
    rows(code='271737000', n=12, vocabulary=oid)
    snomed = sighting(code='271737000', vocabulary='SNOMED')
    assert snomed.source_vocabulary_id == 'SNOMED'
    assert code_resolution._has_patient_rows(snomed), 'the OID rows answer for the canonical vocabulary'


def test_a_hospital_scoped_proposal_asks_about_its_own_patients():
    ours, theirs = OrganizationFactory(), OrganizationFactory()
    rows(n=9, person=PatientRecordFactory(organization=theirs).person)
    scoped = sighting(organization=ours)
    assert scoped.organization_id == ours.pk
    assert sighting(organization=ours).occurrence_count == 2, "another hospital's rows do not count for ours"
    rows(n=3, person=PatientRecordFactory(organization=ours).person)
    assert sighting(organization=ours).occurrence_count == 3


def test_a_limited_rebuild_touches_only_the_keys_given():
    a = SourceCodeConceptMapping.objects.create(source_vocabulary_id=LAB, source_code='A', omop_table='measurement',
                                                occurrence_count=100)
    b = SourceCodeConceptMapping.objects.create(source_vocabulary_id=LAB, source_code='B', omop_table='measurement',
                                                occurrence_count=100)
    for code in ('A', 'B'):
        rows(code=code, n=5)
    assert axis_variants.rebuild_seen_counts([(LAB, 'A')]) == 1
    a.refresh_from_db(); b.refresh_from_db()
    assert (a.occurrence_count, b.occurrence_count) == (5, 100)
    assert axis_variants.rebuild_seen_counts([]) == 0
