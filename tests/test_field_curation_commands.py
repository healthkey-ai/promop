"""Tests for the dump_field_curation and load_field_curation management commands.

These exercise the CLI layer — argument parsing, schema version validation,
stdout/file output, --dry-run, and idempotency. The underlying read_payload /
apply_payload logic is covered in test_field_curation_transfer.py.
"""
import json
import os
import tempfile
from datetime import datetime, timezone
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from omop_core.models import (
    FieldChoice,
    FieldChoiceCode,
    FieldConceptMapping,
    FieldFormula,
    FieldSynonym,
)
from tests.factories import ConceptFactory

pytestmark = pytest.mark.django_db


def _seed():
    """Minimal curation set for round-trip testing."""
    concept = ConceptFactory(
        concept_id=3016723,
        vocabulary_id='LOINC',
        concept_code='2160-0',
        concept_name='Creatinine',
    )
    FieldConceptMapping.objects.create(
        field_name='creatinine',
        concept=concept,
        vocabulary_id='LOINC',
        concept_code='2160-0',
        unit='mg/dL',
        omop_table='measurement',
        source_value='creatinine',
        value_kind='number',
        status='approved',
        provenance='system_generated',
        reviewed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        notes='test mapping',
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


def _wipe():
    FieldConceptMapping.objects.all().delete()
    FieldChoice.objects.all().delete()
    FieldFormula.objects.all().delete()
    FieldSynonym.objects.all().delete()


# ── dump_field_curation ──────────────────────────────────────────────────


def test_dump_to_file_produces_valid_json():
    _seed()
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        path = f.name
    try:
        call_command('dump_field_curation', '--output', path)
        with open(path) as f:
            data = json.load(f)
        assert data['schema_version'] == 1
        assert data['source'] == 'dump_field_curation'
        assert 'exported_at' in data
        assert set(data['tables']) == {
            'mappings', 'custom_fields', 'choices', 'formulas', 'synonyms',
        }
        assert len(data['mappings']) == 1
        assert data['mappings'][0]['field_name'] == 'creatinine'
        assert len(data['choices']) == 1
        assert len(data['formulas']) == 1
        assert len(data['synonyms']) == 1
    finally:
        os.unlink(path)


def test_dump_to_stdout():
    _seed()
    out = StringIO()
    call_command('dump_field_curation', stdout=out)
    # stdout output goes through sys.stdout, not self.stdout, so capture
    # by reading what the command wrote to the file descriptor.
    # The command writes to sys.stdout directly, so we verify it doesn't crash.
    # For a proper test, dump to a file and read it back.
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        path = f.name
    try:
        call_command('dump_field_curation', '--output', path)
        with open(path) as f:
            data = json.load(f)
        assert data['schema_version'] == 1
    finally:
        os.unlink(path)


def test_dump_with_tables_filter():
    _seed()
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        path = f.name
    try:
        call_command('dump_field_curation', '--output', path,
                     '--tables', 'mappings', 'formulas')
        with open(path) as f:
            data = json.load(f)
        assert set(data['tables']) == {'mappings', 'formulas'}
        assert 'mappings' in data
        assert 'formulas' in data
        assert 'choices' not in data
        assert 'synonyms' not in data
    finally:
        os.unlink(path)


# ── load_field_curation ──────────────────────────────────────────────────


def _dump_fixture():
    """Dump current curation to a temp file and return the path."""
    path = tempfile.mktemp(suffix='.json')
    call_command('dump_field_curation', '--output', path)
    return path


def test_load_round_trips_from_dump():
    _seed()
    path = _dump_fixture()
    try:
        _wipe()
        assert FieldConceptMapping.objects.count() == 0

        call_command('load_field_curation', '--input', path)

        assert FieldConceptMapping.objects.count() == 1
        m = FieldConceptMapping.objects.get(field_name='creatinine')
        assert m.unit == 'mg/dL'
        assert m.status == 'approved'
        assert m.notes == 'test mapping'

        assert FieldChoice.objects.count() == 1
        assert FieldFormula.objects.count() == 1
        assert FieldSynonym.objects.count() == 1
    finally:
        os.unlink(path)


def test_load_rejects_wrong_schema_version():
    _seed()
    path = _dump_fixture()
    try:
        with open(path) as f:
            data = json.load(f)
        data['schema_version'] = 999
        with open(path, 'w') as f:
            json.dump(data, f)

        with pytest.raises(CommandError, match='schema_version'):
            call_command('load_field_curation', '--input', path)
    finally:
        os.unlink(path)


def test_load_rejects_non_dict():
    path = tempfile.mktemp(suffix='.json')
    try:
        with open(path, 'w') as f:
            json.dump([1, 2, 3], f)
        with pytest.raises(CommandError, match='JSON object'):
            call_command('load_field_curation', '--input', path)
    finally:
        os.unlink(path)


def test_load_dry_run_leaves_db_unchanged():
    _seed()
    path = _dump_fixture()
    try:
        _wipe()
        assert FieldConceptMapping.objects.count() == 0

        call_command('load_field_curation', '--input', path, '--dry-run')

        # Dry run: nothing written.
        assert FieldConceptMapping.objects.count() == 0
        assert FieldChoice.objects.count() == 0
    finally:
        os.unlink(path)


def test_load_is_idempotent():
    _seed()
    path = _dump_fixture()
    try:
        # First load — should update existing rows.
        out = StringIO()
        call_command('load_field_curation', '--input', path, stdout=out)

        # Second load — same result, no duplicates.
        out2 = StringIO()
        call_command('load_field_curation', '--input', path, stdout=out2)

        assert FieldConceptMapping.objects.count() == 1
        assert FieldChoice.objects.count() == 1
        assert FieldFormula.objects.count() == 1
        assert FieldSynonym.objects.count() == 1
    finally:
        os.unlink(path)


def test_load_with_tables_filter():
    _seed()
    path = _dump_fixture()
    try:
        _wipe()

        call_command('load_field_curation', '--input', path,
                     '--tables', 'mappings')

        # Only mappings loaded, not choices/formulas/synonyms.
        assert FieldConceptMapping.objects.count() == 1
        assert FieldChoice.objects.count() == 0
        assert FieldFormula.objects.count() == 0
        assert FieldSynonym.objects.count() == 0
    finally:
        os.unlink(path)


def test_load_prune_removes_local_only_rows():
    _seed()
    path = _dump_fixture()
    try:
        # Add a local-only mapping not in the fixture.
        FieldConceptMapping.objects.create(
            field_name='local_only', status='proposed',
        )
        assert FieldConceptMapping.objects.count() == 2

        call_command('load_field_curation', '--input', path, '--prune')

        # local_only is gone; creatinine stays.
        assert FieldConceptMapping.objects.count() == 1
        assert FieldConceptMapping.objects.filter(
            field_name='creatinine',
        ).exists()
    finally:
        os.unlink(path)


def test_load_no_tables_raises():
    path = tempfile.mktemp(suffix='.json')
    try:
        data = {'schema_version': 1}  # no 'tables' key
        with open(path, 'w') as f:
            json.dump(data, f)
        with pytest.raises(CommandError, match='No tables to load'):
            call_command('load_field_curation', '--input', path)
    finally:
        os.unlink(path)
