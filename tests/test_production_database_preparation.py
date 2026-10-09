"""Bound production bootstrap work so Render can start the web process in time."""

import csv
from io import StringIO
from unittest.mock import Mock
import zipfile

import pytest
from django.core.management import call_command, CommandError
from django.db.migrations.recorder import MigrationRecorder

from omop_core.management.commands import load_athena_vocabularies as loader
from omop_core.management.commands import prepare_production_database as preparation
from tests.factories import ConceptFactory


def _bootstrap_archive(path):
    concept_rows = StringIO()
    writer = csv.writer(concept_rows, delimiter='\t')
    writer.writerow([
        'concept_id', 'concept_name', 'domain_id', 'vocabulary_id',
        'concept_class_id', 'standard_concept', 'concept_code',
        'valid_start_date', 'valid_end_date', 'invalid_reason',
    ])
    writer.writerow([
        991001, 'Required test LOINC', 'Measurement', 'LOINC', 'Lab Test',
        'S', 'REQUIRED-TEST', '20260101', '20991231', '',
    ])
    writer.writerow([
        991002, 'Unrelated test LOINC', 'Measurement', 'LOINC', 'Lab Test',
        'S', 'UNRELATED-TEST', '20260101', '20991231', '',
    ])
    files = {
        'RELATIONSHIP.csv': (
            'relationship_id\trelationship_name\tis_hierarchical\tdefines_ancestry\t'
            'reverse_relationship_id\trelationship_concept_id\n'
        ),
        'VOCABULARY.csv': (
            'vocabulary_id\tvocabulary_name\tvocabulary_reference\t'
            'vocabulary_version\tvocabulary_concept_id\n'
            'LOINC\tLOINC\thttps://loinc.org\ttest\t0\n'
        ),
        'DOMAIN.csv': 'domain_id\tdomain_name\tdomain_concept_id\nMeasurement\tMeasurement\t0\n',
        'CONCEPT_CLASS.csv': (
            'concept_class_id\tconcept_class_name\tconcept_class_concept_id\n'
            'Lab Test\tLab Test\t0\n'
        ),
        'CONCEPT.csv': concept_rows.getvalue(),
    }
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(f'export/{name}', content)


def test_required_code_set_matches_migration_0201_data():
    migration = loader.importlib.import_module('omop_core.migrations.0201_seed_hklabs_sccm')
    expected = {code for _, code, _ in migration._LOINC_COMMON}
    expected.update(migration._CATALOG_LOINC.values())
    expected.update(code for _, code in migration._CURATED_ALIASES)
    assert loader.required_hklabs_loinc_codes() == expected
    assert len(expected) == 118


@pytest.mark.django_db
def test_migration_bootstrap_scans_archive_but_inserts_only_required_concepts(
    tmp_path, monkeypatch,
):
    archive = tmp_path / 'athena.zip'
    _bootstrap_archive(archive)
    monkeypatch.setattr(
        loader, 'required_hklabs_loinc_codes', lambda: frozenset({'REQUIRED-TEST'}),
    )
    output = StringIO()

    call_command(
        'load_athena_vocabularies', archive=str(archive),
        migration_bootstrap=True, stdout=output,
    )

    assert loader.Concept.objects.filter(concept_code='REQUIRED-TEST').exists()
    assert not loader.Concept.objects.filter(concept_code='UNRELATED-TEST').exists()
    assert 'verified all 1 migration-required LOINC concepts' in output.getvalue()
    assert 'release publication' in output.getvalue()


@pytest.mark.django_db
def test_migration_bootstrap_allows_archive_to_omit_historical_code(tmp_path, monkeypatch):
    archive = tmp_path / 'athena.zip'
    _bootstrap_archive(archive)
    monkeypatch.setattr(
        loader, 'required_hklabs_loinc_codes',
        lambda: frozenset({'REQUIRED-TEST', 'MISSING-TEST'}),
    )
    output = StringIO()
    call_command(
        'load_athena_vocabularies', archive=str(archive),
        migration_bootstrap=True, stdout=output,
    )
    assert 'MISSING-TEST' in output.getvalue()


@pytest.mark.django_db
@pytest.mark.parametrize('incompatible', ['replace', 'dry_run'])
def test_migration_bootstrap_rejects_destructive_or_nonwriting_modes(
    tmp_path, incompatible,
):
    archive = tmp_path / 'athena.zip'
    _bootstrap_archive(archive)
    with pytest.raises(CommandError, match='cannot be combined'):
        call_command(
            'load_athena_vocabularies', archive=str(archive),
            migration_bootstrap=True, **{incompatible: True}, stdout=StringIO(),
        )


@pytest.mark.django_db
def test_preparation_skips_athena_after_dependent_migration(monkeypatch):
    monkeypatch.setattr(
        MigrationRecorder, 'applied_migrations',
        lambda self: {(preparation.APP, preparation.DEPENDENT_MIGRATION)},
    )
    commands = Mock()
    monkeypatch.setattr(preparation, 'call_command', commands)
    call_command('prepare_production_database', gdrive='https://example.invalid/archive')
    commands.assert_called_once_with('migrate', interactive=False, verbosity=1)


@pytest.mark.django_db
def test_preparation_uses_existing_required_concepts_without_downloading(monkeypatch):
    target = ConceptFactory(concept_code='REQUIRED-TEST', vocabulary__vocabulary_id='LOINC')
    monkeypatch.setattr(MigrationRecorder, 'applied_migrations', lambda self: {})
    monkeypatch.setattr(
        preparation, 'required_hklabs_loinc_codes',
        lambda: frozenset({target.concept_code}),
    )
    commands = Mock()
    monkeypatch.setattr(preparation, 'call_command', commands)
    call_command('prepare_production_database', gdrive='https://example.invalid/archive')
    assert [call.args[0] for call in commands.call_args_list] == ['migrate', 'migrate']


@pytest.mark.django_db
def test_preparation_loads_missing_targets_before_remaining_migrations(monkeypatch):
    monkeypatch.setattr(MigrationRecorder, 'applied_migrations', lambda self: {})
    monkeypatch.setattr(
        preparation, 'required_hklabs_loinc_codes',
        lambda: frozenset({'REQUIRED-TEST'}),
    )
    calls = []

    def command(name, *args, **kwargs):
        calls.append((name, args, kwargs))
        if name == 'load_athena_vocabularies':
            ConceptFactory(concept_code='REQUIRED-TEST', vocabulary__vocabulary_id='LOINC')

    monkeypatch.setattr(preparation, 'call_command', command)
    call_command('prepare_production_database', gdrive='https://example.invalid/archive')
    assert [name for name, _, _ in calls] == [
        'migrate', 'load_athena_vocabularies', 'migrate',
    ]
    assert calls[1][2]['migration_bootstrap'] is True
