"""Copying field curation between instances.

There is no second test database, so the two halves are exercised separately:
``read_payload`` runs against the local database (it is read-only, so pointing
it at 'default' is faithful), the tables are then wiped, and ``apply_payload``
rebuilds them. A round trip that reconstructs what was there is the same
assertion the real command makes across two hosts.
"""
from datetime import datetime, timezone

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from omop_core.models import (
    CustomPatientField,
    FieldChoice,
    FieldChoiceCode,
    FieldConceptMapping,
    FieldFormula,
    FieldSynonym,
)
from omop_core.services.field_curation_transfer import (
    apply_payload,
    read_payload,
)
from tests.factories import ConceptFactory

pytestmark = pytest.mark.django_db


def _wipe():
    CustomPatientField.objects.all().delete()
    FieldConceptMapping.objects.all().delete()
    FieldChoice.objects.all().delete()
    FieldFormula.objects.all().delete()
    FieldSynonym.objects.all().delete()


def _seed_curation():
    """Build one row in each curated table, as instance A would have."""
    concept = ConceptFactory(
        concept_id=3016723,
        vocabulary_id='LOINC',
        concept_code='2160-0',
        concept_name='Creatinine [Mass/volume] in Serum or Plasma',
    )
    mapping = FieldConceptMapping.objects.create(
        field_name='creatinine',
        concept=concept,
        vocabulary_id='LOINC',
        concept_code='2160-0',
        unit='mg/dL',
        omop_table='measurement',
        source_value='creatinine',
        value_kind='number',
        type_concept_id=32856,
        status='approved',
        reviewed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        notes='reviewed on instance A',
    )
    custom_mapping = FieldConceptMapping.objects.create(
        field_name='frailty_index',
        concept=concept,
        vocabulary_id='LOINC',
        concept_code='2160-0',
        omop_table='observation',
        source_value='frailty_index',
        value_kind='number',
        status='approved',
    )
    CustomPatientField.objects.create(
        field_name='frailty_index',
        display_name='Frailty Index',
        tab='general',
        field_type='number',
        mapping=custom_mapping,
    )
    choice = FieldChoice.objects.create(
        field_name='stage', display='Stage III', sort_order=3,
    )
    FieldChoiceCode.objects.create(
        choice=choice, code='261641005', vocabulary_id='SNOMED',
        display='Stage 3', is_primary=True,
    )
    FieldFormula.objects.create(
        field_name='bmi', formula='weight / (height/100)^2', is_active=True,
    )
    FieldSynonym.objects.create(
        field_name='creatinine', synonym_text='serum creatinine',
    )
    return mapping


# ── Round trip ────────────────────────────────────────────────────────────

def test_round_trip_reconstructs_every_curated_table():
    _seed_curation()
    payload = read_payload('default')

    _wipe()
    stats = apply_payload(payload)

    assert stats.warnings == []
    assert stats.total(stats.created) == 6  # 2 mappings, custom field, choice, formula, synonym

    mapping = FieldConceptMapping.objects.get(field_name='creatinine')
    assert mapping.concept_id == 3016723
    assert mapping.unit == 'mg/dL'
    assert mapping.omop_table == 'measurement'
    assert mapping.source_value == 'creatinine'
    assert mapping.value_kind == 'number'
    assert mapping.type_concept_id == 32856
    assert mapping.status == 'approved'
    assert mapping.notes == 'reviewed on instance A'
    assert mapping.reviewed_at == datetime(2026, 1, 2, tzinfo=timezone.utc)

    custom = CustomPatientField.objects.get(field_name='frailty_index')
    assert custom.display_name == 'Frailty Index'
    assert custom.mapping.field_name == 'frailty_index'

    choice = FieldChoice.objects.get(field_name='stage', display='Stage III')
    assert choice.sort_order == 3
    code = choice.codes.get()
    assert (code.vocabulary_id, code.code, code.is_primary) == ('SNOMED', '261641005', True)

    assert FieldFormula.objects.get(field_name='bmi').formula == 'weight / (height/100)^2'
    assert FieldSynonym.objects.get(field_name='creatinine').synonym_text == 'serum creatinine'


def test_apply_is_idempotent():
    _seed_curation()
    payload = read_payload('default')

    stats = apply_payload(payload)

    # Everything already present, so the second application updates in place
    # rather than duplicating.
    assert stats.total(stats.created) == 0
    assert stats.total(stats.updated) == 6
    assert FieldConceptMapping.objects.count() == 2
    assert FieldChoice.objects.count() == 1
    assert FieldChoiceCode.objects.count() == 1


# ── Overwrite semantics ───────────────────────────────────────────────────

def test_existing_local_mapping_is_overwritten_from_source():
    _seed_curation()
    payload = read_payload('default')
    _wipe()

    ConceptFactory(
        concept_id=3016723, vocabulary_id='LOINC', concept_code='2160-0',
        concept_name='Creatinine [Mass/volume] in Serum or Plasma',
    )
    FieldConceptMapping.objects.create(
        field_name='creatinine',
        unit='umol/L',
        omop_table='observation',
        status='proposed',
        notes='local guess',
    )

    apply_payload(payload)

    mapping = FieldConceptMapping.objects.get(field_name='creatinine')
    assert mapping.unit == 'mg/dL'
    assert mapping.omop_table == 'measurement'
    assert mapping.status == 'approved'
    assert mapping.notes == 'reviewed on instance A'


def test_local_only_rows_survive_without_prune_and_go_with_it():
    _seed_curation()
    payload = read_payload('default')
    _wipe()

    FieldConceptMapping.objects.create(field_name='local_only', status='proposed')
    FieldSynonym.objects.create(field_name='local_only', synonym_text='mine')

    apply_payload(payload)
    assert FieldConceptMapping.objects.filter(field_name='local_only').exists()
    assert FieldSynonym.objects.filter(field_name='local_only').exists()

    stats = apply_payload(payload, prune=True)
    assert not FieldConceptMapping.objects.filter(field_name='local_only').exists()
    assert not FieldSynonym.objects.filter(field_name='local_only').exists()
    assert stats.deleted['mappings'] == 1
    # The copied rows are untouched by the prune.
    assert FieldConceptMapping.objects.filter(field_name='creatinine').exists()


def test_prune_removes_a_custom_field_before_its_protected_mapping():
    _seed_curation()
    payload = read_payload('default')
    # Drop the custom field and its mapping from the source's view of the world.
    payload['custom_fields'] = []
    payload['mappings'] = [
        row for row in payload['mappings'] if row['field_name'] != 'frailty_index'
    ]

    apply_payload(payload, prune=True)

    assert not CustomPatientField.objects.filter(field_name='frailty_index').exists()
    assert not FieldConceptMapping.objects.filter(field_name='frailty_index').exists()


# ── Cross-instance identity ───────────────────────────────────────────────

def test_concept_is_reresolved_by_code_not_by_id():
    """The same concept can carry a different id on the target instance."""
    _seed_curation()
    payload = read_payload('default')
    _wipe()
    Concept = ConceptFactory._meta.model
    Concept.objects.filter(concept_id=3016723).delete()

    # Instance B knows the same LOINC code under a different id.
    ConceptFactory(
        concept_id=9999001, vocabulary_id='LOINC', concept_code='2160-0',
        concept_name='Creatinine [Mass/volume] in Serum or Plasma',
    )

    stats = apply_payload(payload)

    assert stats.warnings == []
    assert FieldConceptMapping.objects.get(field_name='creatinine').concept_id == 9999001


def test_missing_concept_warns_and_copies_the_rest_of_the_mapping():
    _seed_curation()
    payload = read_payload('default')
    _wipe()
    ConceptFactory._meta.model.objects.filter(concept_id=3016723).delete()

    stats = apply_payload(payload)

    assert any('not loaded on this instance' in w for w in stats.warnings)
    mapping = FieldConceptMapping.objects.get(field_name='creatinine')
    assert mapping.concept_id is None
    # The rest of the curation still arrived.
    assert mapping.unit == 'mg/dL'
    assert mapping.status == 'approved'


def test_missing_concept_code_does_not_fall_back_to_a_reused_local_id():
    """Locally minted concept IDs are not identities across instances."""
    _seed_curation()
    payload = read_payload('default')
    _wipe()
    Concept = ConceptFactory._meta.model
    Concept.objects.filter(concept_id=3016723).delete()
    ConceptFactory(
        concept_id=3016723,
        vocabulary_id='None',
        concept_code='different-local-concept',
        concept_name='A different concept on instance B',
    )

    stats = apply_payload(payload)

    assert stats.warnings
    assert FieldConceptMapping.objects.get(field_name='creatinine').concept_id is None


def test_reviewer_is_not_carried_across_instances():
    """Identity IDs mean different people on different instances."""
    _seed_curation()
    payload = read_payload('default')
    assert 'reviewer' not in payload['mappings'][0]

    _wipe()
    apply_payload(payload)
    assert FieldConceptMapping.objects.get(field_name='creatinine').reviewer_id is None


# ── Selection and safety ──────────────────────────────────────────────────

def test_tables_flag_restricts_what_is_copied():
    _seed_curation()
    payload = read_payload('default', tables=('formulas',))
    assert set(payload) == {'formulas'}

    _wipe()
    stats = apply_payload(payload, tables=('formulas',))

    assert stats.total(stats.created) == 1
    assert FieldFormula.objects.count() == 1
    assert FieldConceptMapping.objects.count() == 0


def test_custom_field_without_its_mapping_is_skipped_not_crashed():
    _seed_curation()
    payload = read_payload('default')
    _wipe()

    stats = apply_payload(payload, tables=('custom_fields',))

    assert stats.skipped['custom_fields'] == 1
    assert any('is not present' in w for w in stats.warnings)
    assert CustomPatientField.objects.count() == 0


def test_dry_run_reports_without_writing():
    _seed_curation()
    payload = read_payload('default')
    _wipe()

    stats = apply_payload(payload, dry_run=True)

    assert stats.total(stats.created) == 6
    assert FieldConceptMapping.objects.count() == 0
    assert FieldChoice.objects.count() == 0


def test_command_requires_a_source_url(monkeypatch):
    monkeypatch.delenv('SOURCE_DATABASE_URL', raising=False)
    with pytest.raises(CommandError, match='SOURCE_DATABASE_URL'):
        call_command('copy_field_mappings')


def test_source_connection_is_registered_read_only():
    from django.db import connections
    from omop_core.management.commands.copy_field_mappings import (
        register_source_connection,
    )

    alias = 'test_field_mapping_source'
    try:
        register_source_connection('postgresql://u:p@example.invalid:5432/instance_a', alias)
        config = connections.databases[alias]
        assert config['NAME'] == 'instance_a'
        assert config['HOST'] == 'example.invalid'
        assert config['OPTIONS']['options'] == '-c default_transaction_read_only=on'
    finally:
        connections.databases.pop(alias, None)


def test_source_connection_carries_the_defaults_django_applies_at_startup():
    """An alias added after startup misses ConnectionHandler.configure_settings.

    Without these keys the connection opens and then dies on the first query
    with a bare ``KeyError: 'TIME_ZONE'``, which no amount of URL-parsing
    coverage catches.
    """
    from django.db import connections
    from omop_core.management.commands.copy_field_mappings import (
        register_source_connection,
    )

    alias = 'test_field_mapping_source_defaults'
    try:
        register_source_connection('postgresql://u:p@example.invalid:5432/instance_a', alias)
        config = connections.databases[alias]
        default = connections.databases['default']
        assert set(default) <= set(config)
        assert set(default['TEST']) <= set(config['TEST'])
    finally:
        connections.databases.pop(alias, None)
