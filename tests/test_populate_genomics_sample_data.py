"""Synthetic sample command → OMOP → patient projection, using Athena CDM links."""
import json
from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from omop_core.management.commands import populate_genomics_sample_data as sample
from omop_core.models import Measurement, Note, Observation
from omop_core.services.genomics import FIELDS, list_variants, normalize_variant, save_variant
from omop_core.services.genomics_catalog import marker_for_variant
from omop_core.services.patient_record_service import refresh_patient_record
from tests.factories import ConceptFactory, ConditionOccurrenceFactory, PatientRecordFactory

pytestmark = pytest.mark.django_db


def sample_record(*, disease_slug, email='', **kwargs):
    record = PatientRecordFactory(person__email=email, **kwargs)
    ConditionOccurrenceFactory(person=record.person, condition_concept_id=0,
                               condition_source_value=disease_slug.replace('-', ' '))
    refresh_patient_record(record.person)
    record.refresh_from_db()
    assert record.disease_slug == disease_slug
    return record


@pytest.fixture
def configured():
    ConceptFactory(vocabulary__vocabulary_id='CDM', concept_code='CDM126',
                   concept_name='measurement.measurement_id')
    for code, domain in FIELDS.values():
        if not code.startswith('genomics:'):
            ConceptFactory(concept_code=code, domain__domain_id=domain)
    call_command('seed_genomics_catalog', stdout=StringIO())


@pytest.mark.parametrize('disease', sample._DISEASE_POOLS)
@pytest.mark.parametrize('status', ['present', 'absent'])
def test_all_disease_markers_round_trip(configured, monkeypatch, disease, status):
    record = sample_record(disease_slug=sample._SLUG_MAP[disease])
    monkeypatch.setattr(sample, '_select_markers', lambda pool: pool)
    monkeypatch.setattr(sample.random, 'choice', lambda choices: status)
    payloads = []

    def capture(person, payload, **kwargs):
        payloads.append(payload)
        return save_variant(person, payload, **kwargs)

    monkeypatch.setattr(sample, 'save_variant', capture)
    call_command('populate_genomics_sample_data', patient=str(record.person_id), stdout=StringIO())
    record.refresh_from_db()
    variants = {v['marker_key']: v for v in record.genetic_mutations}
    assert set(variants) == {m['marker_key'] for m in sample._DISEASE_POOLS[disease]}
    for payload in payloads:
        actual = variants[payload['marker_key']]
        for key, expected in payload.items():
            if key == 'allelic_frequency':
                expected = float(expected)
            elif key == 'gene':
                expected = expected.upper()
            assert actual[key] == expected, key
        assert actual['collection_date'] < actual['test_date'] == actual['interpretation_date']
        assert actual['origin'] == actual['genomic_source_class']
        assert str(record.person_id) in actual['report_id']
        projected = getattr(record, marker_for_variant(actual)['field_name'])
        assert len(projected) == 1
        assert projected[0]['report_id'] == actual['report_id']
    # Long descriptions and provenance survive CDM text overflow via linked notes.
    assert Note.objects.filter(person=record.person).exists()
    assert list_variants(record.person) == record.genetic_mutations


def test_sequence_annotations_are_paired_and_inapplicable_fields_empty(monkeypatch):
    for disease, pool in sample._DISEASE_POOLS.items():
        for entry in pool:
            key = entry['marker_key']
            if entry['kind'] != 'gene':
                continue
            for index, variant in enumerate(sample._HGVS_VARIANTS[key]):
                monkeypatch.setattr(sample.random, 'randrange', lambda n, i=index: i)
                payload = sample._build_gene_payload(entry, disease=disease)
                normalize_variant(payload)
                assert payload['variant'] == payload['transcript_dna_change'] == variant
                assert payload['collection_date'] < payload['test_date'] < date.today().isoformat()
                assert 'clone_fraction' not in payload
                if key in sample._AMINO_ACID_CHANGES:
                    assert payload['amino_acid_change'] == sample._AMINO_ACID_CHANGES[key][index]
                else:
                    assert 'amino_acid_change' not in payload
                if key == 'palb1':
                    assert 'transcript_reference_sequence_id' not in payload
                    assert 'genomic_dna_change' not in payload
    esr1 = next(m for m in sample._DISEASE_POOLS['BC'] if m['marker_key'] == 'esr1')
    monkeypatch.setattr(sample.random, 'randrange', lambda n: 0)
    assert sample._build_gene_payload(esr1)['amino_acid_change'] == 'p.Tyr537Cys'


@pytest.mark.parametrize('status', ['present', 'absent'])
def test_cytogenetic_metadata_uses_clone_fraction_not_vaf(monkeypatch, status):
    monkeypatch.setattr(sample.random, 'choice', lambda choices: status)
    for pool in sample._DISEASE_POOLS.values():
        for entry in pool:
            if entry['kind'] != 'abnormality':
                continue
            payload = sample._build_abnormality_payload(entry)
            normalize_variant(payload)
            assert payload['status'] == payload['assessment'] == status
            assert not {'allelic_frequency', 'amino_acid_change', 'coverage_depth',
                        'transcript_reference_sequence_id', 'genomic_dna_change'} & payload.keys()
            if payload['variant_analysis_method_type'] == 'FISH':
                assert payload['clone_fraction_unit'] == '%'
                assert (payload['clone_fraction'] > 0) == (status == 'present')
            else:
                assert 'clone_fraction' not in payload


def test_verbose_dry_run_does_not_write_or_overwrite(configured):
    record = sample_record(disease_slug='breast-cancer')
    save_variant(record.person, {'gene': 'TP53', 'variant': 'existing'})
    before = list_variants(record.person)
    counts = [model.objects.count() for model in (Measurement, Observation, Note)]
    output = StringIO()
    call_command('populate_genomics_sample_data', patient=str(record.person_id),
                 overwrite=True, dry_run=True, verbosity=2, stdout=output)
    payloads = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{')]
    assert payloads and all(p['report_id'] and p['specimen_type'] for p in payloads)
    assert [model.objects.count() for model in (Measurement, Observation, Note)] == counts
    assert list_variants(record.person) == before


def test_overwrite_retires_athena_components_and_default_skips_existing(configured, monkeypatch):
    record = sample_record(disease_slug='breast-cancer')
    old = save_variant(record.person, {'gene': 'TP53', 'variant': 'old', 'laboratory': 'Old lab'})
    monkeypatch.setattr(sample, '_select_markers', lambda pool: pool[:1])
    call_command('populate_genomics_sample_data', all=True, stdout=StringIO())
    assert list_variants(record.person) == [old]
    call_command('populate_genomics_sample_data', patient=str(record.person_id),
                 overwrite=True, stdout=StringIO())
    record.refresh_from_db()
    assert len(record.genetic_mutations) == 1
    assert record.genetic_mutations[0]['id'] != old['id']
    assert Measurement.objects.get(pk=old['id']).is_erroneous
    assert not Measurement.objects.filter(measurement_event_id=old['id'], is_erroneous=False).exists()
    assert not Observation.objects.filter(observation_event_id=old['id'], is_erroneous=False).exists()


def test_failed_patient_rolls_back_overwrite_and_reports_failure(configured, monkeypatch):
    record = sample_record(disease_slug='breast-cancer')
    before = save_variant(record.person, {'gene': 'TP53', 'variant': 'preserve me'})
    monkeypatch.setattr(sample, '_select_markers', lambda pool: pool[:2])
    calls = 0

    def fail_second(person, payload, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('Injected write failure')
        return save_variant(person, payload, **kwargs)

    monkeypatch.setattr(sample, 'save_variant', fail_second)
    error = StringIO()
    with pytest.raises(CommandError, match='1 patient.*failed; seeded 0 variants across 0 patients'):
        call_command('populate_genomics_sample_data', patient=str(record.person_id),
                     overwrite=True, stdout=StringIO(), stderr=error)
    assert 'Injected write failure' in error.getvalue()
    assert list_variants(record.person) == [before]
    record.refresh_from_db()
    assert record.genetic_mutations == [before]


def test_selection_respects_org_disease_count_and_email(configured, monkeypatch):
    target = sample_record(disease_slug='breast-cancer', email='sample@example.test')
    same_org = sample_record(organization=target.organization, disease_slug='multiple-myeloma')
    other_org = sample_record(disease_slug='breast-cancer')
    monkeypatch.setattr(sample, '_select_markers', lambda pool: pool[:1])
    call_command('populate_genomics_sample_data', org=target.organization.slug,
                 disease='BC', count=1, stdout=StringIO())
    assert len(list_variants(target.person)) == 1
    assert not list_variants(same_org.person)
    assert not list_variants(other_org.person)
    call_command('populate_genomics_sample_data', patient=target.email,
                 overwrite=True, stdout=StringIO())
    assert len(list_variants(target.person)) == 1


@pytest.mark.parametrize('count', [0, -1])
def test_nonpositive_count_rejected(count):
    with pytest.raises(CommandError, match='positive integer'):
        call_command('populate_genomics_sample_data', count=count, dry_run=True)
