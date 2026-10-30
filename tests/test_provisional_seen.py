"""A lookup's Seen bump is provisional until the code's per-patient rows exist (#2123)."""
import pytest

from omop_core.mapping import axis_variants
from omop_core.mapping.code_resolution import _record_proposal
from omop_core.models import PatientSourceCode, SourceCodeConceptMapping
from tests.factories import OrganizationFactory, PatientRecordFactory, PersonFactory

pytestmark = pytest.mark.django_db

LAB = 'urn:oid:1.2.840.114350.1.13.412.2.7.5.737384.134'


def sighting(code='5090', organization=None):
    return _record_proposal(source_vocabulary_id=LAB, source_code=code, source_text='beta2 microglob',
                            concept=None, omop_table='measurement', source_system='etl',
                            organization=organization)


def test_sightings_count_while_no_patient_rows_exist_and_stop_once_they_do():
    first = sighting()
    assert first.occurrence_count == 1
    assert sighting().occurrence_count == 2, 'a lookup is the only count before any patient upload'
    PatientSourceCode.objects.create(person=PersonFactory(), source_value='5090', source_vocabulary_id=LAB,
                                     omop_table='measurement', occurrence_count=40)
    again = sighting()
    assert again.occurrence_count == 2, 'with patient rows on the instance a sighting adds nothing'
    assert again.last_seen is not None and again.last_seen > first.last_seen
    assert axis_variants.rebuild_seen_counts([(LAB, '5090')]) == 1
    again.refresh_from_db()
    assert again.occurrence_count == 40


def test_a_hospital_scoped_proposal_asks_about_its_own_patients():
    ours, theirs = OrganizationFactory(), OrganizationFactory()
    PatientSourceCode.objects.create(person=PatientRecordFactory(organization=theirs).person, source_value='5090',
                                     source_vocabulary_id=LAB, omop_table='measurement', occurrence_count=9)
    scoped = sighting(organization=ours)
    assert scoped.organization_id == ours.pk
    assert sighting(organization=ours).occurrence_count == 2, "another hospital's rows do not count for ours"
    PatientSourceCode.objects.create(person=PatientRecordFactory(organization=ours).person, source_value='5090',
                                     source_vocabulary_id=LAB, omop_table='measurement', occurrence_count=3)
    assert sighting(organization=ours).occurrence_count == 2


def test_a_limited_rebuild_touches_only_the_keys_given():
    a = SourceCodeConceptMapping.objects.create(source_vocabulary_id=LAB, source_code='A', omop_table='measurement',
                                                occurrence_count=100)
    b = SourceCodeConceptMapping.objects.create(source_vocabulary_id=LAB, source_code='B', omop_table='measurement',
                                                occurrence_count=100)
    for code in ('a', 'B'):
        PatientSourceCode.objects.create(person=PersonFactory(), source_value=code, source_vocabulary_id=LAB,
                                         omop_table='measurement', occurrence_count=5)
    assert axis_variants.rebuild_seen_counts([(LAB, 'A')]) == 1
    a.refresh_from_db(); b.refresh_from_db()
    assert (a.occurrence_count, b.occurrence_count) == (5, 100)
    assert axis_variants.rebuild_seen_counts([]) == 0
