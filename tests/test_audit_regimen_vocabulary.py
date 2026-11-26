"""audit_regimen_vocabulary reports resolver ids the vocabulary cannot serve (#2764)."""
from io import StringIO

import pytest
from django.core.management import call_command

from omop_core.models import Concept, ConceptRelationship, Relationship
from omop_core.services import lot_regimens
from omop_core.services.regimen_vocabulary_audit import (
    declared_regimen_targets,
    regimen_target_problems,
)
from tests.factories import ConceptClassFactory, ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

BR = 35804570           # Bendamustine and Rituximab, REGIMEN_CONCEPT_IDS
DARA_KRD = 905602       # MYELOMA_REGIMEN_CONCEPT_IDS and the 'dkrd' alias
PD = 35806066           # the 'pd' alias
PD_SUCCESSOR = 42542407


def _concept(concept_id, vocabulary='HemOnc', concept_class='Regimen', **fields):
    return ConceptFactory(
        concept_id=concept_id, concept_code=str(concept_id),
        concept_name=fields.pop('concept_name', f'Regimen {concept_id}'),
        vocabulary=VocabularyFactory(vocabulary_id=vocabulary),
        domain=DomainFactory(domain_id='Regimen'),
        concept_class=ConceptClassFactory(concept_class_id=concept_class, concept_class_name=concept_class),
        **fields,
    )


@pytest.fixture
def installed():
    """Every declared id installed as a live, standard, named HemOnc Regimen."""
    for concept_id in declared_regimen_targets():
        _concept(concept_id)


def _run():
    out = StringIO()
    try:
        call_command('audit_regimen_vocabulary', stdout=out)
        code = 0
    except SystemExit as exit_:
        code = exit_.code
    return code, out.getvalue()


def test_every_id_a_resolver_can_return_is_declared():
    targets = declared_regimen_targets()
    returned = {lot_regimens.get_regimen_concept_id(drugs)
                for table in (lot_regimens.MYELOMA_REGIMEN_CONCEPT_IDS, lot_regimens.REGIMEN_CONCEPT_IDS)
                for drugs in table}
    returned |= {lot_regimens.get_regimen_concept_id_by_name(name)
                 for name in lot_regimens.MYELOMA_REGIMEN_NAME_TO_CONCEPT_ID}
    returned.discard(None)
    assert returned <= targets.keys()
    assert None not in targets
    assert targets[BR] == ['REGIMEN_CONCEPT_IDS[bendamustine + rituximab]']
    assert "_GENERATOR_ALIASES['dkrd']" in targets[DARA_KRD]
    assert any(source.startswith('MYELOMA_REGIMEN_CONCEPT_IDS[') for source in targets[DARA_KRD])


def test_a_fully_installed_vocabulary_passes(installed):
    assert regimen_target_problems() == []
    code, out = _run()
    assert code == 0
    assert f'All {len(declared_regimen_targets())} regimen ids' in out


@pytest.mark.parametrize('break_it, reason', [
    (lambda c: c.delete(), 'not in the vocabulary'),
    (lambda c: Concept.objects.filter(pk=c.pk).update(
        concept_class=ConceptClassFactory(concept_class_id='Brand Name', concept_class_name='Brand Name')),
     'is HemOnc Brand Name, not a HemOnc Regimen'),
    (lambda c: Concept.objects.filter(pk=c.pk).update(vocabulary=VocabularyFactory(vocabulary_id='RxNorm')),
     'is RxNorm Regimen, not a HemOnc Regimen'),
    (lambda c: Concept.objects.filter(pk=c.pk).update(invalid_reason='D'), "is retired (invalid_reason 'D')"),
    (lambda c: Concept.objects.filter(pk=c.pk).update(standard_concept=None), 'is not a standard concept'),
    (lambda c: Concept.objects.filter(pk=c.pk).update(concept_name=''), 'has no usable name'),
    (lambda c: Concept.objects.filter(pk=c.pk).update(concept_name='No matching concept'), 'has no usable name'),
])
def test_each_defect_is_reported_against_its_id_and_sources(installed, break_it, reason):
    break_it(Concept.objects.get(pk=BR))

    [problem] = regimen_target_problems()
    assert (problem.concept_id, problem.reason) == (BR, reason)
    code, out = _run()
    assert code == 1
    assert f'{BR} {reason}; returned by REGIMEN_CONCEPT_IDS[bendamustine + rituximab]' in out
    assert f'1 of {len(declared_regimen_targets())} regimen ids' in out


def test_an_empty_invalid_reason_reads_as_live(installed):
    Concept.objects.filter(pk=BR).update(invalid_reason='')
    assert regimen_target_problems() == []


def test_a_retired_id_shows_its_live_maps_to_target(installed):
    Concept.objects.filter(pk=PD).update(invalid_reason='D', standard_concept=None)
    successor = _concept(PD_SUCCESSOR, concept_name='Pomalidomide and Dexamethasone (Pd)')
    retired_target = _concept(99_000_001, invalid_reason='D')
    maps_to, _ = Relationship.objects.get_or_create(relationship_id='Maps to', defaults=dict(
        relationship_name='Maps to', is_hierarchical=0, defines_ancestry=0,
        reverse_relationship_id='Mapped from', relationship_concept_id=0))
    for target in (successor, retired_target):
        ConceptRelationship.objects.create(
            concept_1_id=PD, concept_2=target, relationship=maps_to,
            valid_start_date='2000-01-01', valid_end_date='2099-12-31')

    [problem] = regimen_target_problems()
    assert problem.maps_to == ((PD_SUCCESSOR, 'Pomalidomide and Dexamethasone (Pd)'),)
    _code, out = _run()
    assert f"{PD} is retired (invalid_reason 'D'); returned by _GENERATOR_ALIASES['pd']; " \
           f'maps to {PD_SUCCESSOR} "Pomalidomide and Dexamethasone (Pd)"' in out


def test_a_value_that_is_not_an_integer_id_is_reported(installed, monkeypatch):
    monkeypatch.setitem(lot_regimens.REGIMEN_CONCEPT_IDS, frozenset({'olaparib'}), 'Olaparib')

    reasons = {p.concept_id: p.reason for p in regimen_target_problems()}
    assert reasons == {'Olaparib': 'is not an integer concept_id'}
