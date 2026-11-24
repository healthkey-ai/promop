"""Renaming a therapy line to a therapy with no concept must drop the old concept (#2763).

CancerBot re-sends a whole line on every edit, with a null concept for an unmapped
therapy, and lists every field of the line as changed. The writer keeps a stored
concept on such a re-send, so an outcome-only edit cannot wipe a concept an
import asserted. But when the therapy itself was replaced, the stored concept
names the old therapy, and the derivation reads it first. Neither changed_fields
nor the stored name can tell the two apart (an imported line stores its own
label), so the caller says it explicitly with `{prefix}_therapy_replaced`.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from omop_core.services.episode_service import upsert_therapy_line_episode
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

REGIMEN_ID = 35806260          # a HemOnc Regimen ("VRd")
TAMOXIFEN_RXNORM = 1436678     # RxNorm ingredient (CB's Therapy.omop_concept_id)
START = date(2024, 1, 10)


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
        'regimen': _concept(REGIMEN_ID, 'VRd', 'HemOnc', 'Regimen', 'Regimen'),
        'tamoxifen': _concept(TAMOXIFEN_RXNORM, 'tamoxifen', 'RxNorm', 'Drug', 'Ingredient'),
    }


WHOLE_LINE = {'first_line_therapy', 'first_line_start_date', 'first_line_outcome'}   # what CB lists today


def _send(person, changed=WHOLE_LINE, *, name, concept_id=None, outcome=None, edited=None):
    """One CB write of line 1: the whole line, the fields CB lists, and its replaced-therapy flag."""
    pi = SimpleNamespace(person=person, first_line_therapy=name, first_line_start_date=START,
                         first_line_therapy_concept_id=concept_id, first_line_outcome=outcome,
                         first_line_therapy_replaced=edited)
    sync_to_omop(pi, set(changed), today=START)
    return refresh_patient_record(person)


def _episode(person):
    return Episode.objects.get(person=person, episode_number=1)


def _import_line(person, vocab, label):
    upsert_therapy_line_episode(
        person, line_number=1, regimen_concept=vocab['regimen'], regimen_source_concept=vocab['regimen'],
        start_date=START, source_value=label, today=START, asserted_by='import',
    )


@pytest.mark.parametrize('first_concept', [REGIMEN_ID, TAMOXIFEN_RXNORM])
def test_replacing_the_therapy_with_an_unmapped_one_drops_the_old_concept(vocab, first_concept):
    person = PersonFactory()
    record = _send(person, name='first', concept_id=first_concept, edited=True)
    assert record.first_line_therapy_id == first_concept

    record = _send(person, name='something unmapped', edited=True)

    assert record.first_line_therapy_id is None
    assert record.first_line_therapy == 'something unmapped'
    # A single-drug line lists its own drug as a component (#2760); the old drug must go with the concept.
    assert first_concept not in (record.first_line_component_ids or [])
    ep = _episode(person)
    assert ep.episode_source_concept_id is None
    assert ep.episode_object_concept_id == 0


@pytest.mark.parametrize('label', ['LOT-1', 'ARTEMIS-LOT-1', 'Bortezomib, lenalidomide, dexamethasone'])
def test_a_whole_line_re_send_without_the_flag_keeps_an_imported_concept(vocab, label):
    """Today's CB payload on an outcome edit: every line field listed, an unmapped slug, no flag."""
    person = PersonFactory()
    _import_line(person, vocab, label)

    record = _send(person, name='vrd-slug', outcome='CR')

    assert record.first_line_therapy_id == REGIMEN_ID
    ep = _episode(person)
    assert ep.episode_source_concept_id == REGIMEN_ID
    assert ep.episode_object_concept_id == REGIMEN_ID


def test_a_whole_line_re_send_of_the_same_name_keeps_the_concept(vocab):
    person = PersonFactory()
    _send(person, name='vrd', concept_id=REGIMEN_ID, edited=True)

    record = _send(person, name='vrd', outcome='CR')

    assert record.first_line_therapy_id == REGIMEN_ID


def test_replacing_the_therapy_with_a_mapped_one_takes_the_new_concept(vocab):
    person = PersonFactory()
    _send(person, name='vrd', concept_id=REGIMEN_ID, edited=True)

    record = _send(person, name='tamoxifen', concept_id=TAMOXIFEN_RXNORM, edited=True)

    assert record.first_line_therapy_id == TAMOXIFEN_RXNORM
    assert _episode(person).episode_source_concept_id == TAMOXIFEN_RXNORM


def test_a_replacement_whose_concept_is_missing_from_the_vocabulary_drops_the_old_concept(vocab):
    """The caller resolved an id the Concept table lacks: the line is name-only, not the old therapy."""
    person = PersonFactory()
    _send(person, name='vrd', concept_id=REGIMEN_ID, edited=True)

    record = _send(person, name='new regimen', concept_id=999999999, edited=True)

    assert record.first_line_therapy_id is None
    assert _episode(person).episode_source_concept_id is None


def test_a_replacement_is_not_missed_when_the_names_share_their_first_50_characters(vocab):
    """episode_source_value keeps 50 characters; the flag, not a name comparison, decides."""
    prefix = 'x' * 50
    person = PersonFactory()
    _send(person, name=prefix + ' regimen one', concept_id=REGIMEN_ID, edited=True)

    record = _send(person, name=prefix + ' regimen two', edited=True)

    assert record.first_line_therapy_id is None


def test_the_writer_default_never_clears(vocab):
    """Other callers (import, inference, authoring) keep the fill-only behaviour."""
    person = PersonFactory()
    upsert_therapy_line_episode(person, line_number=1, regimen_concept=vocab['regimen'],
                                regimen_source_concept=vocab['regimen'], start_date=START,
                                source_value='VRd', today=START)

    upsert_therapy_line_episode(person, line_number=1, start_date=START, source_value='renamed', today=START)

    ep = _episode(person)
    assert ep.episode_source_value == 'renamed'
    assert ep.episode_source_concept_id == REGIMEN_ID
    assert ep.episode_object_concept_id == REGIMEN_ID


@pytest.mark.parametrize('supply', ['object', 'source'])
def test_a_replacement_resets_each_slot_the_caller_did_not_supply(vocab, supply):
    """No CB call supplies one slot alone; the writer's contract is per slot all the same."""
    person = PersonFactory()
    upsert_therapy_line_episode(person, line_number=1, regimen_concept=vocab['regimen'],
                                regimen_source_concept=vocab['regimen'], start_date=START,
                                source_value='VRd', today=START)
    new = vocab['tamoxifen']

    upsert_therapy_line_episode(
        person, line_number=1, start_date=START, source_value='tamoxifen', today=START, therapy_replaced=True,
        regimen_concept=new if supply == 'object' else None,
        regimen_source_concept=new if supply == 'source' else None,
    )

    ep = _episode(person)
    if supply == 'object':
        assert (ep.episode_object_concept_id, ep.episode_source_concept_id) == (TAMOXIFEN_RXNORM, None)
    else:   # a replacement overwrites the source slot even without overwrite_source_concept
        assert (ep.episode_object_concept_id, ep.episode_source_concept_id) == (0, TAMOXIFEN_RXNORM)


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason='linked drug exposures still name the old drug; follow-up in #2764')
def test_a_replaced_line_with_a_linked_exposure_no_longer_names_the_old_drug(vocab):
    """The writer clears the Episode's concept, but exposures linked to the line stay linked, and the
    derivation names the line from them. Whether a replacement should unlink them is a separate decision."""
    person = PersonFactory()
    _send(person, name='tamoxifen', concept_id=TAMOXIFEN_RXNORM, edited=True)
    drug = DrugExposureFactory(person=person, drug_concept=vocab['tamoxifen'],
                               drug_exposure_start_date=START, drug_exposure_end_date=date(2024, 3, 1))
    EpisodeEvent.objects.create(episode_id=_episode(person).episode_id, event_id=drug.drug_exposure_id,
                                episode_event_field_concept_id=CONCEPT_DRUG_EXPOSURE_FIELD)

    record = _send(person, name='something unmapped', edited=True)

    assert record.first_line_therapy == 'something unmapped'
    assert record.first_line_therapy_id is None
    assert TAMOXIFEN_RXNORM not in (record.first_line_component_ids or [])
