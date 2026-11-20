"""A therapy line identified by a non-HemOnc concept must read back its id (#2760).

The writer stores whatever concept the caller resolved a line to: PROMOP's own
catalog gives surgery and transplant a SNOMED procedure as the therapy id, and
CancerBot sends single drugs as RxNorm ingredients. The derivation resolved an
id only from HemOnc, so such a line was stored and then shown with no id, and a
single-drug line offered no component to a component-level trial criterion.

These tests go through the same seam a caller uses: `sync_to_omop` writes the
line, `refresh_patient_record` derives it, and PatientRecord is read.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from omop_core.models import Concept, ConceptRelationship, Relationship
from omop_core.services.mappings import (
    CONCEPT_DRUG_EXPOSURE_FIELD,
    CONCEPT_EHR_TYPE,
    CONCEPT_TREATMENT_REGIMEN,
)
from omop_core.services.omop_write_service import sync_to_omop
from omop_core.services.patient_record_service import refresh_patient_record
from omop_oncology.models import Episode, EpisodeEvent
from tests.factories import (
    ConceptClassFactory,
    ConceptFactory,
    DomainFactory,
    DrugExposureFactory,
    PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db

TAMOXIFEN_RXNORM = 1436678      # RxNorm ingredient (CB's Therapy.omop_concept_id)
MASTECTOMY_SNOMED = 4066543     # SNOMED procedure (PROMOP catalog row 296)
GENOMIC_TESTING = 4233623       # SNOMED, domain Measurement
TAMOXIFEN_HEMONC = 35803329     # HemOnc Component
REGIMEN_ID = 35806260           # a HemOnc Regimen
CISPLATIN_SNOMED = 4301188      # SNOMED Substance, non-standard (catalog row 257)
G_CSF_CLASS = 35807374          # HemOnc Component Class, domain Drug
LENALIDOMIDE_RXNORM = 19115    # a second RxNorm ingredient (test-local id)
CISPLATIN_RXNORM = 1397599      # RxNorm ingredient cisplatin 'Maps to' (catalog row 257's component)
BRAND_RXNORM = 19023250         # an RxNorm Brand Name, non-standard, maps to nothing here


def _concept(concept_id, name, vocabulary, domain, concept_class, **extra):
    return ConceptFactory(
        concept_id=concept_id, concept_name=name, concept_code=str(concept_id),
        vocabulary=VocabularyFactory(vocabulary_id=vocabulary, vocabulary_name=vocabulary),
        domain=DomainFactory(domain_id=domain, domain_name=domain),
        concept_class=ConceptClassFactory(concept_class_id=concept_class, concept_class_name=concept_class),
        **extra,
    )


@pytest.fixture
def vocab():
    omop = VocabularyFactory(vocabulary_id='OMOP', vocabulary_name='OMOP')
    ConceptFactory(concept_id=0, concept_name='No matching concept', concept_code='0', vocabulary=omop)
    for cid in (CONCEPT_TREATMENT_REGIMEN, CONCEPT_EHR_TYPE, CONCEPT_DRUG_EXPOSURE_FIELD):
        ConceptFactory(concept_id=cid, concept_name=str(cid), concept_code=str(cid), vocabulary=omop)
    return {
        'tamoxifen': _concept(TAMOXIFEN_RXNORM, 'tamoxifen', 'RxNorm', 'Drug', 'Ingredient'),
        'mastectomy': _concept(MASTECTOMY_SNOMED, 'Mastectomy', 'SNOMED', 'Procedure', 'Procedure'),
        'genomic': _concept(GENOMIC_TESTING, 'Genomic testing', 'SNOMED', 'Measurement', 'Procedure'),
        # HemOnc Components are non-standard (they map to RxNorm), as in the loaded vocabulary.
        'tamoxifen_hemonc': _concept(TAMOXIFEN_HEMONC, 'Tamoxifen', 'HemOnc', 'Drug', 'Component',
                                     standard_concept=None),
        'regimen': _concept(REGIMEN_ID, 'VRd', 'HemOnc', 'Regimen', 'Regimen'),
        'cisplatin': _concept(CISPLATIN_SNOMED, 'Cisplatin', 'SNOMED', 'Drug', 'Substance',
                              standard_concept=None),
        'g_csf': _concept(G_CSF_CLASS, 'G-CSF', 'HemOnc', 'Drug', 'Component Class', standard_concept='C'),
        'lenalidomide': _concept(LENALIDOMIDE_RXNORM, 'lenalidomide', 'RxNorm', 'Drug', 'Ingredient'),
    }


def _maps_to(c1, c2, **extra):
    Relationship.objects.get_or_create(relationship_id='Maps to', defaults=dict(
        relationship_name='Maps to', is_hierarchical=0, defines_ancestry=0,
        reverse_relationship_id='Mapped from', relationship_concept_id=0))
    return ConceptRelationship.objects.create(
        concept_1_id=c1, concept_2_id=c2, relationship_id='Maps to',
        valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31), **extra)


def _write_line(person, concept_id, *, line=1, name='line', start=date(2024, 1, 10)):
    prefix = {1: 'first_line', 2: 'second_line'}.get(line, 'later')
    pi = SimpleNamespace(person=person, **{
        f'{prefix}_therapy': name,
        f'{prefix}_start_date': start,
        f'{prefix}_therapy_concept_id': concept_id,
    })
    if line >= 3:
        # sync_to_omop writes 'later' as line 3.
        assert line == 3
    sync_to_omop(pi, {f'{prefix}_therapy', f'{prefix}_start_date'}, today=start)
    return refresh_patient_record(person)


def test_rxnorm_ingredient_line_reads_back_its_id_and_its_drug(vocab):
    record = _write_line(PersonFactory(), TAMOXIFEN_RXNORM, name='tamoxifen')

    assert record.first_line_therapy_id == TAMOXIFEN_RXNORM
    assert TAMOXIFEN_RXNORM in record.first_line_component_ids
    assert TAMOXIFEN_RXNORM in record.therapy_component_ids
    # Supplied by the caller, not a validated HemOnc regimen: never 'asserted'.
    assert record.therapy_ids_provenance['first_line_therapy_id']['origin'] == 'inferred'


def test_snomed_procedure_line_reads_back_its_id_but_is_not_a_drug(vocab):
    record = _write_line(PersonFactory(), MASTECTOMY_SNOMED, name='breast surgery')

    assert record.first_line_therapy_id == MASTECTOMY_SNOMED
    assert MASTECTOMY_SNOMED not in (record.first_line_component_ids or [])
    # A resolved line is labelled by its concept, as a HemOnc line already is.
    assert record.first_line_therapy == 'Mastectomy'


def test_second_line_carries_its_drug_id(vocab):
    record = _write_line(PersonFactory(), TAMOXIFEN_RXNORM, line=2, name='tamoxifen')

    assert record.second_line_therapy_id == TAMOXIFEN_RXNORM
    assert TAMOXIFEN_RXNORM in record.second_line_component_ids


def test_a_non_standard_vocabulary_drug_is_still_a_therapy(vocab):
    """The catalog gives cisplatin a non-standard SNOMED substance as its id:
    being non-standard does not make a real concept a placeholder. Its drug is
    the standard concept it maps to, not the substance itself."""
    _concept(CISPLATIN_RXNORM, 'cisplatin', 'RxNorm', 'Drug', 'Ingredient')
    _maps_to(CISPLATIN_SNOMED, CISPLATIN_RXNORM)

    record = _write_line(PersonFactory(), CISPLATIN_SNOMED, name='cisplatin')

    assert record.first_line_therapy_id == CISPLATIN_SNOMED
    assert CISPLATIN_RXNORM in record.first_line_component_ids
    assert CISPLATIN_RXNORM in record.therapy_component_ids
    assert CISPLATIN_SNOMED not in record.first_line_component_ids


def test_a_retired_mapping_names_no_drug(vocab):
    _concept(CISPLATIN_RXNORM, 'cisplatin', 'RxNorm', 'Drug', 'Ingredient')
    _maps_to(CISPLATIN_SNOMED, CISPLATIN_RXNORM, invalid_reason='D')

    record = _write_line(PersonFactory(), CISPLATIN_SNOMED, name='cisplatin')

    assert record.first_line_therapy_id == CISPLATIN_SNOMED
    assert CISPLATIN_RXNORM not in (record.first_line_component_ids or [])


@pytest.mark.parametrize('target', [
    dict(domain='Device'),
    dict(standard_concept=None),
    dict(invalid_reason='D'),
])
def test_a_mapping_to_anything_but_a_live_standard_drug_names_no_drug(vocab, target):
    target = dict(target)
    domain = target.pop('domain', 'Drug')
    _concept(CISPLATIN_RXNORM, 'cisplatin', 'RxNorm', domain, 'Ingredient', **target)
    _maps_to(CISPLATIN_SNOMED, CISPLATIN_RXNORM)

    record = _write_line(PersonFactory(), CISPLATIN_SNOMED, name='cisplatin')

    assert record.first_line_therapy_id == CISPLATIN_SNOMED
    assert CISPLATIN_RXNORM not in (record.first_line_component_ids or [])


def test_a_drug_that_maps_to_nothing_keeps_its_id_and_names_no_drug(vocab):
    _concept(BRAND_RXNORM, 'Advil', 'RxNorm', 'Drug', 'Brand Name', standard_concept=None)

    record = _write_line(PersonFactory(), BRAND_RXNORM, name='Advil')

    assert record.first_line_therapy_id == BRAND_RXNORM
    assert not record.first_line_component_ids


def test_later_line_carries_its_procedure_id(vocab):
    record = _write_line(PersonFactory(), MASTECTOMY_SNOMED, line=3, name='mastectomy')

    [later] = record.later_therapies
    assert later['lineNumber'] == 3
    assert later['concept_id'] == MASTECTOMY_SNOMED
    assert record.later_therapy_ids == [MASTECTOMY_SNOMED]


def test_measurement_concept_is_not_a_treatment(vocab):
    record = _write_line(PersonFactory(), GENOMIC_TESTING, name='genomic testing')

    assert record.first_line_therapy_id is None
    assert record.first_line_therapy == 'genomic testing'   # the line itself is still listed


def test_retired_concept_stays_unresolved(vocab):
    Concept.objects.filter(concept_id=TAMOXIFEN_RXNORM).update(invalid_reason='D')

    record = _write_line(PersonFactory(), TAMOXIFEN_RXNORM, name='tamoxifen')

    assert record.first_line_therapy_id is None


@pytest.mark.parametrize('concept_id, vocabulary', [
    (2_000_000_777, 'HK-Drug'),     # quarantine, in the local id block
    (1_900_000_777, 'HK-Drug'),     # quarantine vocabulary below the block
    (500_777, 'LOCAL'),             # a legacy local placeholder without a source tag
    (2_000_000_888, 'RxNorm'),      # an id in the local block, whatever vocabulary it claims
    (392_021_009, 'SNOMED'),        # a legacy FHIR-ingest mint without a source tag
    (9_000_123, 'SNOMED'),          # a legacy seeder mint
    (600_777, 'sct'),               # a concept minted from a FHIR coding
    (600_778, 'FHIR'),              # a concept minted by the FHIR import
    (9_099_999, 'SNOMED'),          # the legacy ranges are inclusive at both ends
    (392_022_000, 'SNOMED'),
])
def test_a_local_placeholder_is_not_a_therapy(vocab, concept_id, vocabulary):
    """An unmatched name is minted under a local vocabulary as a placeholder. It
    marks the line as unresolved; it is not an id."""
    _concept(concept_id, 'unmatched drug', vocabulary, 'Drug', 'Ingredient', standard_concept=None)

    record = _write_line(PersonFactory(), concept_id, name='unmatched drug')

    assert record.first_line_therapy_id is None
    assert concept_id not in (record.first_line_component_ids or [])


def test_hemonc_component_line_also_lists_its_drug(vocab):
    record = _write_line(PersonFactory(), TAMOXIFEN_HEMONC, name='tamoxifen')

    assert record.first_line_therapy_id == TAMOXIFEN_HEMONC     # resolved before this change too
    assert TAMOXIFEN_HEMONC in record.first_line_component_ids  # its drug was missing


@pytest.mark.parametrize('concept_id, concept_class, standard', [
    (G_CSF_CLASS, 'Component Class', 'C'),
    (35_900_001, 'Brand Name', None),     # non-standard and not a classification
])
def test_a_hemonc_concept_other_than_a_component_is_not_one_of_the_lines_drugs(
        vocab, concept_id, concept_class, standard):
    if concept_id != G_CSF_CLASS:
        _concept(concept_id, 'Brandname', 'HemOnc', 'Drug', concept_class, standard_concept=standard)

    record = _write_line(PersonFactory(), concept_id, name='supportive')

    assert record.first_line_therapy_id == concept_id           # the HemOnc branch, as before
    assert concept_id not in (record.first_line_component_ids or [])


def _episode_with_exposures(person, source, *drugs):
    episode = Episode.objects.create(
        episode_id=27_600_000 + person.person_id % 1000, person=person, episode_number=1,
        episode_concept_id=CONCEPT_TREATMENT_REGIMEN, episode_type_concept_id=CONCEPT_EHR_TYPE,
        episode_object_concept_id=0, episode_source_concept=source,
        episode_start_date=date(2024, 1, 10),
    )
    for drug in drugs:
        exposure = DrugExposureFactory(
            person=person, drug_concept=drug,
            drug_exposure_start_date=date(2024, 1, 10), drug_exposure_end_date=date(2024, 3, 1))
        EpisodeEvent.objects.create(
            episode_id=episode.episode_id, event_id=exposure.drug_exposure_id,
            episode_event_field_concept_id=CONCEPT_DRUG_EXPOSURE_FIELD)
    return episode


def test_exposures_of_other_care_do_not_unname_the_line(vocab, monkeypatch):
    """The reverse-sync links exposures by an open-ended date window, so a later
    drug lands on a procedure line. The line is still the procedure."""
    import omop_core.services.lot_regimens as lot_regimens
    person = PersonFactory()
    _episode_with_exposures(person, vocab['mastectomy'], vocab['lenalidomide'])
    monkeypatch.setattr(lot_regimens, 'get_regimen_concept_id', lambda _names: None)

    record = refresh_patient_record(person)

    assert record.first_line_therapy_id == MASTECTOMY_SNOMED
    assert LENALIDOMIDE_RXNORM in record.first_line_component_ids


def test_a_line_of_its_own_drug_alone_keeps_its_id(vocab, monkeypatch):
    import omop_core.services.lot_regimens as lot_regimens
    person = PersonFactory()
    _episode_with_exposures(person, vocab['tamoxifen'], vocab['tamoxifen'])
    monkeypatch.setattr(lot_regimens, 'get_regimen_concept_id', lambda _names: None)

    record = refresh_patient_record(person)

    assert record.first_line_therapy_id == TAMOXIFEN_RXNORM


def test_a_regimen_resolved_from_drug_names_still_wins(vocab, monkeypatch):
    """The drug source is only a last resort: a line whose exposures resolve to a
    HemOnc regimen keeps that regimen, as before."""
    import omop_core.services.lot_regimens as lot_regimens

    person = PersonFactory()
    drug = DrugExposureFactory(
        person=person, drug_concept=vocab['tamoxifen'],
        drug_exposure_start_date=date(2024, 1, 10), drug_exposure_end_date=date(2024, 3, 1))
    episode = Episode.objects.create(
        episode_id=27_600_001, person=person, episode_number=1,
        episode_concept_id=CONCEPT_TREATMENT_REGIMEN, episode_type_concept_id=CONCEPT_EHR_TYPE,
        episode_object_concept_id=0, episode_source_concept=vocab['tamoxifen'],
        episode_start_date=date(2024, 1, 10),
    )
    EpisodeEvent.objects.create(
        episode_id=episode.episode_id, event_id=drug.drug_exposure_id,
        episode_event_field_concept_id=CONCEPT_DRUG_EXPOSURE_FIELD)
    monkeypatch.setattr(lot_regimens, 'get_regimen_concept_id', lambda _names: REGIMEN_ID)

    record = refresh_patient_record(person)

    assert record.first_line_therapy_id == REGIMEN_ID
