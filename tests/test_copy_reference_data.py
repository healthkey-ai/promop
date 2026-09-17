"""copy_reference_data: reference rows cross instances by natural key, links resolve on arrival.

Both instances are the test database here: read, wipe, apply. That is the same
round trip a real copy makes between two databases.
"""
from collections.abc import Callable

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from omop_core.management.commands import copy_reference_data
from omop_core.models import (
    Concept,
    Disease,
    Ethnicity,
    MappingDestinationCandidate,
    MutationCode,
    MutationGene,
    SourceCodeConceptMapping,
    TherapyComponent,
    TherapyOutcome,
    TherapyRegimen,
    TherapyRegimenComponent,
    Vocabulary,
)
from omop_core.services.reference_transfer import apply_reference, read_reference
from omop_core.services.field_curation_transfer import TransferStats
from omop_core.services.instance_copy import register_source_connection
from tests.factories import ConceptFactory, ConceptClassFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


def _hk_concept(code: str = 'hkd:revlimid', concept_id: int = 2_000_000_500) -> Concept:
    """A HealthKey-minted drug placeholder."""
    return ConceptFactory(
        concept_id=concept_id, concept_code=code, concept_name='Revlimid placeholder',
        vocabulary=VocabularyFactory(vocabulary_id='HK-Drug', vocabulary_name='HK-Drug'),
        domain=DomainFactory(domain_id='Drug', domain_name='Drug'),
        concept_class=ConceptClassFactory(concept_class_id='Drug', concept_class_name='Drug'),
        standard_concept=None, source='HealthKey',
    )


def _hemonc() -> Vocabulary:
    """The HemOnc vocabulary row, so HemOnc concepts have a real vocabulary FK."""
    return VocabularyFactory(vocabulary_id='HemOnc', vocabulary_name='HemOnc')


def _roundtrip(wipe: Callable[[], None]) -> TransferStats:
    """Read reference data, run wipe, then write it back."""
    payload = read_reference('default')
    wipe()
    return apply_reference(payload)


def test_lookups_and_their_links_are_recreated():
    gene = MutationGene.objects.create(code='TP53', title='TP53')
    MutationCode.objects.create(code='TP53-R175H', title='TP53 R175H', gene=gene)
    Ethnicity.objects.create(code='hispanic', title='Hispanic or Latino')

    def wipe():
        MutationCode.objects.all().delete()
        MutationGene.objects.all().delete()
        Ethnicity.objects.all().delete()

    _roundtrip(wipe)

    assert MutationCode.objects.get(code='TP53-R175H').gene.code == 'TP53'
    assert Ethnicity.objects.filter(code='hispanic').exists()


def test_existing_rows_are_overwritten_by_natural_key():
    Ethnicity.objects.create(code='hispanic', title='Hispanic or Latino', llm_hint='source hint')
    payload = read_reference('default')
    Ethnicity.objects.filter(code='hispanic').update(llm_hint='local hint')

    stats = apply_reference(payload)

    assert Ethnicity.objects.get(code='hispanic').llm_hint == 'source hint'
    assert stats.updated['Ethnicity'] == 1
    assert Ethnicity.objects.count() == 1


def test_therapy_links_and_outcome_diseases_follow_codes_not_ids():
    concept = ConceptFactory(concept_id=35_100_001, concept_code='R-CHOP', vocabulary=_hemonc())
    regimen = TherapyRegimen.objects.create(code='r-chop', title='R-CHOP', concept=concept)
    component = TherapyComponent.objects.create(code='rituximab', title='Rituximab')
    TherapyRegimenComponent.objects.create(regimen=regimen, component=component)
    disease = Disease.objects.create(code='fl', title='Follicular lymphoma')
    TherapyOutcome.objects.create(code='cr', title='Complete response', value='CR').diseases.add(disease)

    def wipe():
        TherapyRegimenComponent.objects.all().delete()
        TherapyOutcome.objects.all().delete()
        for model in (TherapyRegimen, TherapyComponent, Disease):
            model.objects.all().delete()
        # Recreated with a different pk, as on another instance.
        Disease.objects.create(code='other', title='Other')

    _roundtrip(wipe)

    link = TherapyRegimenComponent.objects.get()
    assert (link.regimen.code, link.component.code) == ('r-chop', 'rituximab')
    assert link.regimen.concept_id == concept.concept_id
    assert list(TherapyOutcome.objects.get(code='cr').diseases.values_list('code', flat=True)) == ['fl']


def test_missing_concept_is_cleared_and_reported():
    concept = ConceptFactory(concept_id=35_100_002, concept_code='BR', vocabulary=_hemonc())
    TherapyRegimen.objects.create(code='br', title='BR', concept=concept)

    def wipe():
        TherapyRegimen.objects.all().delete()
        Concept.objects.filter(pk=concept.pk).delete()

    stats = _roundtrip(wipe)

    assert TherapyRegimen.objects.get(code='br').concept_id is None
    assert any('HemOnc:BR' in w for w in stats.warnings)


def test_healthkey_concept_gets_a_local_id_and_mappings_resolve_to_it():
    placeholder = _hk_concept()
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='RxNorm', source_code='337535', target_concept=placeholder,
        destination_vocabulary_id='HK-Drug', omop_table='drug_exposure', status='proposed',
        domain_id='Drug',
    )
    MappingDestinationCandidate.objects.create(
        mapping=mapping, target_vocabulary_id='HK-Drug', target_concept_code='hkd:revlimid',
        target_concept=placeholder,
    )

    def wipe():
        SourceCodeConceptMapping.objects.all().delete()
        Concept.objects.filter(pk=placeholder.pk).delete()
        # Another instance already used that id for something else.
        _hk_concept(code='hkd:unrelated', concept_id=placeholder.pk)

    _roundtrip(wipe)

    copied = Concept.objects.get(vocabulary_id='HK-Drug', concept_code='hkd:revlimid')
    assert copied.concept_id != placeholder.pk
    assert SourceCodeConceptMapping.objects.get(source_code='337535').target_concept_id == copied.concept_id
    candidate = MappingDestinationCandidate.objects.get()
    assert (candidate.mapping.source_code, candidate.target_concept_id) == ('337535', copied.concept_id)


def test_dry_run_writes_nothing():
    Ethnicity.objects.create(code='hispanic', title='Hispanic or Latino')
    payload = read_reference('default')
    Ethnicity.objects.all().delete()

    stats = apply_reference(payload, dry_run=True)

    assert stats.created['Ethnicity'] == 1
    assert not Ethnicity.objects.exists()


def test_command_requires_a_source_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv('SOURCE_DATABASE_URL', raising=False)
    with pytest.raises(CommandError, match='SOURCE_DATABASE_URL'):
        call_command('copy_reference_data')


def test_source_failure_is_reported_as_a_command_error(monkeypatch: pytest.MonkeyPatch):
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError('connection lost mid-stream')

    monkeypatch.setattr(copy_reference_data.Command, '_warn_on_release_mismatch', lambda self: None)
    monkeypatch.setattr(copy_reference_data, 'read_reference', lambda *a, **k: {})
    monkeypatch.setattr(copy_reference_data, 'apply_reference', boom)
    with pytest.raises(CommandError, match='Could not copy from the source database'):
        call_command('copy_reference_data', '--source-url', 'postgresql://u:p@localhost/db')


def test_source_connection_is_registered_read_only():
    from django.db import connections

    alias = 'test_copy_source'
    try:
        register_source_connection('postgresql://u:p@example.invalid:5432/instance_a', alias)
        config = connections.databases[alias]
        assert (config['NAME'], config['HOST']) == ('instance_a', 'example.invalid')
        assert config['OPTIONS']['options'] == '-c default_transaction_read_only=on'
    finally:
        connections.databases.pop(alias, None)


def test_source_connection_carries_the_defaults_django_applies_at_startup():
    """Without them the first query on a late alias dies on KeyError: 'TIME_ZONE'."""
    from django.db import connections
    from django.db.utils import ConnectionHandler

    alias = 'test_copy_source_defaults'
    try:
        register_source_connection('postgresql://u:p@example.invalid:5432/instance_a', alias)
        config = connections.databases[alias]
        default = ConnectionHandler({'default': {
            'ENGINE': config['ENGINE'], 'NAME': 'instance_a',
        }}).settings['default']
        assert set(default) <= set(config)
        assert set(default['TEST']) <= set(config['TEST'])
    finally:
        connections.databases.pop(alias, None)
