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
    Concept,
    FieldFormula,
    FieldSynonym,
    SourceCodeConceptMapping,
)
from django.db import connection
from django.test.utils import CaptureQueriesContext

from omop_core.services import field_curation_transfer
from omop_core.services.field_curation_transfer import (
    DEFAULT_TABLES,
    TransferStats,
    apply_payload,
    read_payload,
)
from tests.factories import ConceptFactory, VocabularyFactory

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
        call_command('copy_curation')


def test_source_connection_is_registered_read_only():
    from django.db import connections
    from omop_core.management.commands.copy_curation import (
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
    from django.db.utils import ConnectionHandler
    from omop_core.management.commands.copy_curation import (
        register_source_connection,
    )

    alias = 'test_field_mapping_source_defaults'
    try:
        register_source_connection('postgresql://u:p@example.invalid:5432/instance_a', alias)
        config = connections.databases[alias]
        # Compare framework defaults, not this deployment's optional settings
        # (such as a test TEMPLATE that the remote source must not inherit).
        default = ConnectionHandler({'default': {
            'ENGINE': config['ENGINE'], 'NAME': 'instance_a',
        }}).settings['default']
        assert set(default) <= set(config)
        assert set(default['TEST']) <= set(config['TEST'])
    finally:
        connections.databases.pop(alias, None)


# ── Code mappings (SourceCodeConceptMapping) ──────────────────────────────
#
# A different curation screen from the five above, so it gets its own seed and
# its own wipe rather than joining _seed_curation: the default table set does
# not include it, and a test that conflated the two would stop catching that.

def _wipe_code_mappings():
    SourceCodeConceptMapping.objects.all().delete()


def _vocab(vocabulary_id):
    """A Vocabulary row for ``vocabulary_id``.

    ConceptFactory only ever creates LOINC and Concept.vocabulary is a deferred
    FK. A bare vocabulary_id='SNOMED' builds a concept pointing at a missing
    row, and the violation surfaces at teardown, not in the test that caused it.
    """
    return VocabularyFactory(
        vocabulary_id=vocabulary_id, vocabulary_name=vocabulary_id,
    )


def _seed_code_mapping(**overrides):
    target = ConceptFactory(
        concept_id=3016723, vocabulary_id='LOINC', concept_code='2160-0',
        concept_name='Creatinine [Mass/volume] in Serum or Plasma',
    )
    kwargs = dict(
        domain_id='Measurement',
        # Uncoded: a paper lab test name, which the model calls out as
        # An HK-* value would be invalid here. clean() rejects those as source
        # systems and create() does not run clean().
        source_vocabulary_id='',
        source_code='CREAT',
        source_code_description='Creatinine, serum',
        target_concept=target,
        destination_vocabulary_id='LOINC',
        omop_table='measurement',
        status='approved',
        origin='curator',
        notes='approved on instance A',
        reviewed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    kwargs.update(overrides)
    return SourceCodeConceptMapping.objects.create(**kwargs)


def test_code_mappings_are_not_copied_unless_asked_for():
    """The default set is the field-mapping screen, not this one."""
    _seed_code_mapping()
    payload = read_payload('default', tables=DEFAULT_TABLES)
    assert 'code_mappings' not in payload

    _wipe_code_mappings()
    apply_payload(payload, tables=DEFAULT_TABLES)
    assert SourceCodeConceptMapping.objects.count() == 0


def test_code_mapping_round_trip():
    _seed_code_mapping()
    payload = read_payload('default', tables=('code_mappings',))
    _wipe_code_mappings()

    stats = apply_payload(payload, tables=('code_mappings',))

    assert stats.created['code_mappings'] == 1
    row = SourceCodeConceptMapping.objects.get()
    assert row.source_vocabulary_id == ''
    assert row.source_code == 'CREAT'
    assert row.source_code_description == 'Creatinine, serum'
    assert row.target_concept.concept_code == '2160-0'
    assert row.omop_table == 'measurement'
    assert row.status == 'approved'
    assert row.notes == 'approved on instance A'
    assert row.reviewed_at == datetime(2026, 1, 2, tzinfo=timezone.utc)


def test_code_mapping_natural_key_is_vocabulary_and_code():
    """An existing local row with the same (vocabulary, code) is overwritten."""
    _seed_code_mapping()
    payload = read_payload('default', tables=('code_mappings',))
    _wipe_code_mappings()
    _seed_code_mapping(status='proposed', notes='local guess', target_concept=None)

    stats = apply_payload(payload, tables=('code_mappings',))

    assert stats.updated['code_mappings'] == 1
    assert SourceCodeConceptMapping.objects.count() == 1
    row = SourceCodeConceptMapping.objects.get()
    assert row.status == 'approved'
    assert row.notes == 'approved on instance A'


def test_uncoded_source_rows_key_on_blank_vocabulary_without_colliding():
    """Blank source_vocabulary_id is a value, not a null. Two uncoded names
    with different text stay two rows."""
    _seed_code_mapping(source_vocabulary_id='', source_code='M-PROTEIN')
    _seed_code_mapping(source_vocabulary_id='', source_code='M PROTEIN')
    payload = read_payload('default', tables=('code_mappings',))
    _wipe_code_mappings()

    apply_payload(payload, tables=('code_mappings',))

    assert SourceCodeConceptMapping.objects.count() == 2
    assert set(
        SourceCodeConceptMapping.objects.values_list('source_code', flat=True)
    ) == {'M-PROTEIN', 'M PROTEIN'}


def test_all_three_concept_fks_are_reresolved_by_code():
    """Not just target: source_concept and suggested_target_concept too.

    Uses a coded row (ICD-10-CM -> SNOMED), because an uncoded source has no
    concept of its own and would leave source_concept null.
    """
    source_c = ConceptFactory(
        concept_id=45_000_001, vocabulary=_vocab('ICD10CM'), concept_code='N18.3',
    )
    target = ConceptFactory(
        concept_id=4_030_518, vocabulary=_vocab('SNOMED'), concept_code='433144002',
    )
    suggested = ConceptFactory(
        concept_id=4_030_519, vocabulary=_vocab('SNOMED'), concept_code='709044004',
    )
    _seed_code_mapping(
        source_vocabulary_id='ICD10CM', source_code='N18.3',
        domain_id='Condition', omop_table='condition',
        destination_vocabulary_id='SNOMED',
        source_concept=source_c, target_concept=target,
        suggested_target_concept=suggested,
        suggestion_outcome='overridden', suggestion_model_version='v0.2',
    )
    payload = read_payload('default', tables=('code_mappings',))
    _wipe_code_mappings()

    # Instance B numbers the same three concepts differently.
    Concept.objects.all().delete()
    ConceptFactory(concept_id=9_101, vocabulary=_vocab('ICD10CM'), concept_code='N18.3')
    ConceptFactory(concept_id=9_102, vocabulary=_vocab('SNOMED'), concept_code='433144002')
    ConceptFactory(concept_id=9_103, vocabulary=_vocab('SNOMED'), concept_code='709044004')

    stats = apply_payload(payload, tables=('code_mappings',))

    row = SourceCodeConceptMapping.objects.get()
    assert row.source_concept_id == 9_101
    assert row.target_concept_id == 9_102
    assert row.suggested_target_concept_id == 9_103
    assert stats.warnings == []
    assert row.suggestion_outcome == 'overridden'
    assert row.suggestion_model_version == 'v0.2'


def test_unresolvable_concept_is_nulled_not_carried_as_a_stale_id():
    """db_constraint=False would accept the source id, so nulling is deliberate:
    a stale id names a different concept here and ingest would act on it."""
    _seed_code_mapping()
    payload = read_payload('default', tables=('code_mappings',))
    _wipe_code_mappings()
    Concept.objects.all().delete()   # instance B never loaded LOINC 2160-0

    stats = apply_payload(payload, tables=('code_mappings',))

    row = SourceCodeConceptMapping.objects.get()
    assert row.target_concept_id is None
    assert any('2160-0' in w for w in stats.warnings)
    # The rest of the curation still lands.
    assert row.status == 'approved'
    assert row.source_code_description == 'Creatinine, serum'


def test_attribution_and_occurrence_counters_do_not_cross_instances():
    """Counters record this deployment's own ingest traffic, so the source's
    numbers must not overwrite them."""
    _seed_code_mapping(occurrence_count=812)
    payload = read_payload('default', tables=('code_mappings',))
    _wipe_code_mappings()
    local = _seed_code_mapping(status='proposed', occurrence_count=3)

    apply_payload(payload, tables=('code_mappings',))

    local.refresh_from_db()
    assert local.occurrence_count == 3      # local traffic, not instance A's 812
    assert local.status == 'approved'       # curation did cross
    assert local.reviewer_id is None
    assert local.created_by_id is None
    assert local.updated_by_id is None


def test_prune_removes_local_only_code_mappings():
    _seed_code_mapping()
    payload = read_payload('default', tables=('code_mappings',))
    _seed_code_mapping(source_vocabulary_id='ICD10CM', source_code='N18.3')

    stats = apply_payload(payload, tables=('code_mappings',), prune=True)

    assert stats.deleted['code_mappings'] == 1
    assert SourceCodeConceptMapping.objects.count() == 1
    assert SourceCodeConceptMapping.objects.get().source_code == 'CREAT'


def test_code_mapping_dry_run_writes_nothing():
    _seed_code_mapping()
    payload = read_payload('default', tables=('code_mappings',))
    _wipe_code_mappings()

    stats = apply_payload(payload, tables=('code_mappings',), dry_run=True)

    assert stats.created['code_mappings'] == 1
    assert SourceCodeConceptMapping.objects.count() == 0


@pytest.mark.django_db
def test_code_mappings_stream_in_chunks_without_loading_the_table(monkeypatch):
    """The read side never holds the whole table, and the write side never
    loads every local row to match against.

    This is what OOM-killed a 512Mi Cloud Run job: 117k rows as one list, plus
    a dict of every local row. Chunking is the fix, so it is asserted rather
    than left to be rediscovered.
    """
    monkeypatch.setattr(field_curation_transfer, 'CODE_MAPPING_CHUNK', 2)
    for i in range(5):
        _seed_code_mapping(source_code=f'STREAM-{i}')

    payload = read_payload('default', tables=('code_mappings',), stream=True)
    assert not isinstance(payload['code_mappings'], list)

    rows = list(read_payload('default', tables=('code_mappings',))['code_mappings'])
    assert len(rows) == 5


@pytest.mark.django_db
def test_code_mapping_queries_do_not_grow_with_row_count():
    """Concepts are resolved per chunk, not per reference.

    The naive version ran one query per concept FK per row, 350k for a real
    copy. Chunk size is constant here, so per-chunk lookups must not multiply
    with rows.
    """
    _seed_code_mapping(source_code='TEMPLATE')
    template = list(
        read_payload('default', tables=('code_mappings',))['code_mappings']
    )[0]

    def queries_for(count):
        SourceCodeConceptMapping.objects.all().delete()
        rows = []
        for i in range(count):
            row = dict(template)
            row['source_code'] = f'Q-{i}'
            rows.append(row)
        stats = TransferStats()
        with CaptureQueriesContext(connection) as ctx:
            field_curation_transfer._apply_code_mappings(rows, stats)
        return len(ctx.captured_queries)

    assert field_curation_transfer.CODE_MAPPING_CHUNK >= 20
    small, large = queries_for(5), queries_for(20)
    # One chunk either way, so the two lookup queries are paid once each; only
    # the per-row INSERTs scale.
    assert large - small <= (20 - 5) + 2


@pytest.mark.django_db
def test_dangling_source_concept_does_not_bind_a_local_concept_by_id():
    """A source FK pointing at a deleted concept must land as null.

    The FKs are db_constraint=False so the source can keep an id whose concept
    is gone. That id may name a different locally minted concept here, and
    ingest would act on it.
    """
    _wipe_code_mappings()
    local = ConceptFactory(concept_id=9_500_001, concept_code='LOCAL-ONLY')
    mapping = _seed_code_mapping(source_code='DANGLE')
    SourceCodeConceptMapping.objects.filter(pk=mapping.pk).update(
        target_concept_id=local.concept_id,
    )
    row = list(read_payload('default', tables=('code_mappings',))['code_mappings'])[0]
    # Source instance lost the concept, keeping only the id.
    row['target_concept'] = {
        'vocabulary_id': '', 'concept_code': '', 'concept_id': local.concept_id,
    }

    _wipe_code_mappings()
    stats = TransferStats()
    field_curation_transfer._apply_code_mappings([row], stats)

    copied = SourceCodeConceptMapping.objects.get(source_code='DANGLE')
    assert copied.target_concept_id is None
    assert any('not loaded on this instance' in w for w in stats.warnings)


@pytest.mark.django_db
def test_streamed_source_failure_is_reported_as_a_command_error(monkeypatch):
    """A streamed read fails inside apply_payload, not read_payload."""
    def boom(*args, **kwargs):
        raise RuntimeError('connection lost mid-stream')

    monkeypatch.setattr(field_curation_transfer, '_apply_code_mappings', boom)
    with pytest.raises(CommandError, match='Could not copy from the source database'):
        call_command(
            'copy_curation', '--tables', 'code_mappings',
            '--source-url', 'postgresql://u:p@localhost/db',
        )
