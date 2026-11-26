"""audit_regimen_vocabulary reports resolver ids the vocabulary cannot serve (#2764)."""
from io import StringIO

import pytest
from django.core.management import call_command

from omop_core.models import Concept, ConceptRelationship, Relationship
from omop_core.services import lot_regimens, regimen_vocabulary_audit
from omop_core.services.regimen_vocabulary_audit import (
    declared_regimen_targets,
    regimen_target_problems,
)
from tests.factories import ConceptClassFactory, ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db

BR = 35804570           # Bendamustine and Rituximab, REGIMEN_CONCEPT_IDS
BR_SOURCE = 'REGIMEN_CONCEPT_IDS[bendamustine + rituximab]'
DARA_KRD = 905602       # MYELOMA_REGIMEN_CONCEPT_IDS and the 'dkrd' alias
COMPONENT = 99_000_000
RETIRED = 99_000_010    # test-owned, so fixing the real tables cannot empty the test


def _concept(concept_id, vocabulary='HemOnc', concept_class='Regimen', **fields):
    return ConceptFactory(
        concept_id=concept_id, concept_code=str(concept_id),
        concept_name=fields.pop('concept_name', f'Regimen {concept_id}'),
        vocabulary=VocabularyFactory(vocabulary_id=vocabulary),
        domain=DomainFactory(domain_id='Regimen'),
        concept_class=ConceptClassFactory(concept_class_id=concept_class, concept_class_name=concept_class),
        **fields,
    )


def _edge(concept_1_id, concept_2_id, relationship_id):
    relationship, _ = Relationship.objects.get_or_create(relationship_id=relationship_id, defaults=dict(
        relationship_name=relationship_id, is_hierarchical=0, defines_ancestry=0,
        reverse_relationship_id='', relationship_concept_id=0))
    ConceptRelationship.objects.create(
        concept_1_id=concept_1_id, concept_2_id=concept_2_id, relationship=relationship,
        valid_start_date='2000-01-01', valid_end_date='2099-12-31')


def _regimen(concept_id, **fields):
    """A live, standard, named HemOnc Regimen with one component edge."""
    concept = _concept(concept_id, **fields)
    _edge(concept_id, COMPONENT, 'Has cytotoxic chemo')
    return concept


@pytest.fixture
def installed():
    _concept(COMPONENT, vocabulary='HemOnc', concept_class='Component', concept_name='a drug')
    for concept_id in declared_regimen_targets():
        _regimen(concept_id)


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
    assert targets[BR] == [BR_SOURCE]
    assert "_GENERATOR_ALIASES['dkrd']" in targets[DARA_KRD]
    assert any(source.startswith('MYELOMA_REGIMEN_CONCEPT_IDS[') for source in targets[DARA_KRD])


def test_a_fully_installed_vocabulary_passes(installed):
    assert regimen_target_problems() == []
    code, out = _run()
    assert code == 0
    assert f'All {len(declared_regimen_targets())} regimen ids' in out


def _update(**fields):
    return lambda: Concept.objects.filter(pk=BR).update(**fields)


@pytest.mark.parametrize('break_it, reason', [
    (lambda: (ConceptRelationship.objects.filter(concept_1_id=BR).delete(),
              Concept.objects.filter(pk=BR).delete()), 'not in the vocabulary'),
    (lambda: Concept.objects.filter(pk=BR).update(
        concept_class=ConceptClassFactory(concept_class_id='Brand Name', concept_class_name='Brand Name')),
     'is HemOnc Brand Name, not a HemOnc Regimen'),
    (lambda: Concept.objects.filter(pk=BR).update(vocabulary=VocabularyFactory(vocabulary_id='RxNorm')),
     'is RxNorm Regimen, not a HemOnc Regimen'),
    (_update(invalid_reason='D'), "is retired (invalid_reason 'D')"),
    (_update(standard_concept=None), 'is not a standard concept'),
    (_update(concept_name=''), 'has no usable name'),
    (_update(concept_name='   '), 'has no usable name'),
    (_update(concept_name='No matching concept'), 'has no usable name'),
    (lambda: ConceptRelationship.objects.filter(concept_1_id=BR).delete(),
     'has no component edge the derivation reads'),
])
def test_each_defect_is_reported_against_its_id_and_sources(installed, break_it, reason):
    break_it()

    [problem] = regimen_target_problems()
    assert problem.concept_id == BR
    assert problem.reason.startswith(reason)
    code, out = _run()
    assert code == 1
    assert f'{BR} {reason}' in out
    assert f'returned by {BR_SOURCE}' in out


def test_an_empty_invalid_reason_reads_as_live(installed):
    Concept.objects.filter(pk=BR).update(invalid_reason='')
    assert regimen_target_problems() == []


def test_the_derivations_own_regimen_test_is_applied(installed, monkeypatch):
    monkeypatch.setattr(regimen_vocabulary_audit, '_is_asserted_regimen', lambda c: c.concept_id != BR)

    [problem] = regimen_target_problems()
    assert (problem.concept_id, problem.reason) == (BR, 'is not accepted as a HemOnc regimen by the derivation')


def test_components_are_judged_by_the_derivations_own_expansion(installed, monkeypatch):
    real = regimen_vocabulary_audit._expand_component_ids
    monkeypatch.setattr(regimen_vocabulary_audit, '_expand_component_ids',
                        lambda regimens, drugs: set() if list(regimens) == [BR] else real(regimens, drugs))

    [problem] = regimen_target_problems()
    assert problem.concept_id == BR
    assert problem.reason.startswith('has no component edge the derivation reads')


def test_a_retired_id_lists_only_successors_that_would_pass(installed, monkeypatch):
    monkeypatch.setitem(lot_regimens.REGIMEN_CONCEPT_IDS, frozenset({'drug x', 'drug y'}), RETIRED)
    monkeypatch.setitem(lot_regimens._GENERATOR_ALIASES, 'xy', RETIRED)
    _regimen(RETIRED, invalid_reason='U', standard_concept=None)
    _regimen(RETIRED + 1, concept_name='Replaced by')
    _regimen(RETIRED + 2, concept_name='Maps to')
    _regimen(RETIRED + 3, invalid_reason='D')
    _concept(RETIRED + 4, concept_class='Brand Name')
    _regimen(RETIRED + 5, concept_name='Obsolete edge')
    _edge(RETIRED, RETIRED + 1, 'Concept replaced by')
    for target in (RETIRED + 2, RETIRED + 3, RETIRED + 4):
        _edge(RETIRED, target, 'Maps to')
    _edge(RETIRED, RETIRED + 5, 'Concept replaced by')
    ConceptRelationship.objects.filter(concept_1_id=RETIRED, concept_2_id=RETIRED + 5).update(invalid_reason='D')

    [problem] = regimen_target_problems()
    assert problem.replacements == ((RETIRED + 1, 'Replaced by'), (RETIRED + 2, 'Maps to'))
    _code, out = _run()
    assert (f"{RETIRED} is retired (invalid_reason 'U'); returned by "
            "REGIMEN_CONCEPT_IDS[drug x + drug y], _GENERATOR_ALIASES['xy']; "
            f'live successor {RETIRED + 1} "Replaced by", {RETIRED + 2} "Maps to"') in out


@pytest.mark.parametrize('value', [float(DARA_KRD), True, [DARA_KRD], str(DARA_KRD)])
def test_a_value_that_is_not_an_integer_id_is_reported_against_its_source(installed, monkeypatch, value):
    monkeypatch.setitem(lot_regimens._GENERATOR_ALIASES, 'dkrd', value)

    problems = regimen_target_problems()
    assert [(p.concept_id, p.sources, p.reason) for p in problems] == [
        (value, ("_GENERATOR_ALIASES['dkrd']",), 'is not an integer concept_id')]
    code, out = _run()
    assert code == 1
    assert f"{value!r} is not an integer concept_id; returned by _GENERATOR_ALIASES['dkrd']" in out
