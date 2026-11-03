"""A code first seen bare gets its name when a later import sends one (#2168).

Two writers left codes unnamed: an importer that sent no name the first time
(the mapping row and the HK concept minted for it were named after the code),
and FHIR sync, which stored a first coding's bare code even when a later
coding had a display.
"""
import pytest

from omop_core.mapping.code_resolution import resolve_source_code
from omop_core.models import Concept, DrugExposure, ProcedureOccurrence, SourceCodeConceptMapping
from patient_portal.api.fhir.sync import FhirSyncView, _source_text
from tests.factories import ConceptFactory, PersonFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

CPT = 'CPT4'


def minted(code, *, description='', name=None, status='proposed'):
    concept = ConceptFactory(concept_name=name or code, concept_code=f'hkp:{code.lower()}',
                             vocabulary=VocabularyFactory(vocabulary_id='HK-Procedure', vocabulary_name='HK'),
                             standard_concept=None)
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=CPT, source_code=code, omop_table='procedure', status=status,
        target_concept=concept, source_code_description=description, origin='import',
        origin_system='code-mapping-api')
    return mapping, concept


def sighting(code, text):
    return resolve_source_code(source_code=code, source_vocabulary_id=CPT, source_text=text,
                               omop_table='procedure', source_system='code-mapping-api')


def test_a_later_sighting_names_a_code_first_seen_bare():
    mapping, concept = minted('85025')

    sighting('85025', 'CBC with differential')

    mapping.refresh_from_db()
    concept.refresh_from_db()
    assert mapping.source_code_description == 'CBC with differential'
    assert concept.concept_name == 'CBC with differential'
    assert mapping.occurrence_count == 1, 'the sighting still counts'


def test_a_description_that_repeats_the_code_counts_as_unnamed():
    mapping, _ = minted('85025', description='85025')

    sighting('85025', 'CBC with differential')

    mapping.refresh_from_db()
    assert mapping.source_code_description == 'CBC with differential'


def test_a_name_already_held_is_kept():
    mapping, concept = minted('85025', description='Curated name', name='Curated name')

    sighting('85025', 'Another spelling')

    mapping.refresh_from_db()
    concept.refresh_from_db()
    assert mapping.source_code_description == 'Curated name'
    assert concept.concept_name == 'Curated name'


def test_a_bare_code_sent_again_names_nothing():
    mapping, concept = minted('85025')

    sighting('85025', '85025')
    sighting('85025', '')

    mapping.refresh_from_db()
    concept.refresh_from_db()
    assert mapping.source_code_description == ''
    assert concept.concept_name == '85025'


def test_a_minted_concept_another_mapping_points_at_keeps_its_name():
    mapping, concept = minted('85025')
    SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='urn:oid:1.2.840.114350.1.13.90.2.7.2.696580', source_code='85025',
        omop_table='procedure', status='proposed', target_concept=concept)

    sighting('85025', 'CBC with differential')

    mapping.refresh_from_db()
    concept.refresh_from_db()
    assert mapping.source_code_description == 'CBC with differential'
    assert concept.concept_name == '85025', 'the other code is not a CBC'


def test_an_approved_mapping_is_left_alone():
    mapping, concept = minted('85025', status='approved')

    sighting('85025', 'CBC with differential')

    mapping.refresh_from_db()
    assert mapping.source_code_description == ''


# ---------------------------------------------------------------- FHIR sync

RXNORM = 'http://www.nlm.nih.gov/research/umls/rxnorm'
NDC = 'http://hl7.org/fhir/sid/ndc'
SNOMED = 'http://snomed.info/sct'


def test_a_later_coding_s_display_beats_a_bare_first_code():
    codeable = {'coding': [{'system': RXNORM, 'code': '2762'},
                           {'system': NDC, 'code': '00093-0832', 'display': 'clonazepam 0.5 mg tablet'}]}
    assert _source_text(codeable) == 'clonazepam 0.5 mg tablet'
    assert _source_text({'text': 'Clonazepam', 'coding': codeable['coding']}) == 'Clonazepam'
    assert _source_text({'coding': [{'system': RXNORM, 'code': '2762'}]}) == '2762'
    assert _source_text({'coding': [{'system': RXNORM}]}) == ''


@pytest.fixture
def sync():
    person = PersonFactory()
    ehr = ConceptFactory(concept_name='EHR', concept_code='EHR-2168')
    no_match = ConceptFactory(concept_name='No matching concept', concept_code='NM-2168')
    found = ConceptFactory(concept_name='Bone marrow biopsy', concept_code='86273004')
    # Every code below resolves from the cache, so nothing reaches the resolver.
    cache = {('SNOMED', '86273004'): found, ('*', '86273004'): found}

    def run(procedures):
        return FhirSyncView()._ingest_procedures(person, procedures, ehr, no_match, cache, 'iss|sub', None, {})
    run.person = person
    return run


def procedure(day='2025-10-24'):
    return {'resourceType': 'Procedure', 'performedDateTime': day, 'code': {'coding': [
        {'system': SNOMED, 'code': '86273004'},
        {'system': 'urn:oid:1.2.840.114350.1.13.90.2.7.2.696580', 'code': '23521',
         'display': 'Bone marrow biopsy, needle'}]}}


def test_a_synced_procedure_stores_the_display(sync):
    sync([procedure()])

    (row,) = ProcedureOccurrence.objects.filter(person=sync.person)
    assert row.procedure_source_value == 'Bone marrow biopsy, needle'


def test_a_resync_renames_a_row_stored_under_the_bare_code_instead_of_adding_one(sync):
    sync([procedure()])
    ProcedureOccurrence.objects.filter(person=sync.person).update(procedure_source_value='86273004')

    sync([procedure()])
    sync([procedure()])

    (row,) = ProcedureOccurrence.objects.filter(person=sync.person)
    assert row.procedure_source_value == 'Bone marrow biopsy, needle'


def test_a_resync_of_a_vaccine_stored_under_the_bare_code_renames_it():
    person = PersonFactory()
    ehr = ConceptFactory(concept_name='EHR', concept_code='EHR-2168-V')
    no_match = ConceptFactory(concept_name='No matching concept', concept_code='NM-2168-V')
    vaccine = ConceptFactory(concept_name='Hib (PRP-T)', concept_code='48')
    cache = {('CVX', '48'): vaccine}
    imm = {'resourceType': 'Immunization', 'occurrenceDateTime': '2022-06-24', 'vaccineCode': {'coding': [
        {'system': 'http://hl7.org/fhir/sid/cvx', 'code': '48'},
        {'system': NDC, 'code': '49281-0545-03', 'display': 'ActHIB'}]}}
    view = FhirSyncView()
    view._ingest_immunizations(person, [imm], ehr, no_match, cache, 'iss|sub', None, {})
    DrugExposure.objects.filter(person=person).update(drug_source_value='48')

    view._ingest_immunizations(person, [imm], ehr, no_match, cache, 'iss|sub', None, {})

    (row,) = DrugExposure.objects.filter(person=person)
    assert (row.drug_source_value, row.route_source_value) == ('ActHIB', 'VACCINE')
    assert Concept.objects.filter(pk=row.drug_concept_id).first() == vaccine


# ---------------------------------------------------------------- measurements

EPIC_LAB = 'urn:oid:1.2.840.114350.1.13.90.2.7.5.737384.26'


def lab(value, day='2025-10-24T08:00:00Z', daily=False):
    obs = {'resourceType': 'Observation', 'status': 'final', 'effectiveDateTime': day,
           'code': {'coding': [{'system': 'http://loinc.org', 'code': '2823-3'},
                               {'system': EPIC_LAB, 'code': '1527', 'display': 'Potassium'}]},
           'valueQuantity': {'value': value, 'unit': 'mmol/L'}}
    if daily:
        from patient_portal.api.fhir.sync import AGGREGATION_EXT_URL
        obs['extension'] = [{'url': AGGREGATION_EXT_URL, 'valueCode': 'daily'}]
    return obs


@pytest.fixture
def labs():
    from omop_core.models import Measurement
    person = PersonFactory()
    ehr = ConceptFactory(concept_name='EHR', concept_code='EHR-2168-M')
    view = FhirSyncView()

    def run(observations):
        # The LOINC code is not loaded, so each result stays at concept 0 and
        # its source value is all that identifies it.
        view._ingest_observations(person, observations, ehr, {}, 'iss|sub', None, {})
        return list(Measurement.objects.filter(person=person).order_by('measurement_id'))
    return run


def bare_code(rows):
    from omop_core.models import Measurement
    Measurement.objects.filter(pk__in=[r.pk for r in rows]).update(measurement_source_value='2823-3')


def test_a_resynced_lab_result_stored_under_the_bare_code_is_not_added_again(labs):
    (first,) = labs([lab(4.1)])
    assert (first.measurement_concept_id, first.measurement_source_value) == (0, 'Potassium')
    bare_code([first])

    rows = labs([lab(4.1)])

    assert [r.pk for r in rows] == [first.pk]


def test_a_new_lab_result_beside_one_stored_under_the_bare_code_is_still_added(labs):
    (first,) = labs([lab(4.1)])
    bare_code([first])

    rows = labs([lab(4.1), lab(4.6)])

    assert sorted(float(r.value_as_number) for r in rows) == [4.1, 4.6]


def test_a_resynced_daily_rollup_stored_under_the_bare_code_is_updated_in_place(labs):
    (first,) = labs([lab(4.1, daily=True)])
    bare_code([first])

    rows = labs([lab(4.4, daily=True)])

    assert [r.pk for r in rows] == [first.pk]
    assert float(rows[0].value_as_number) == 4.4
