"""Tests for the import_etl_cross_maps management command."""
import json
import tempfile
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command

from omop_core.models import Concept, SourceCodeConceptMapping
from tests.factories import ConceptFactory, VocabularyFactory, DomainFactory, ConceptClassFactory

pytestmark = pytest.mark.django_db


def _ensure_vocabs():
    """Ensure required vocabularies, domains, and concept classes exist."""
    VocabularyFactory(vocabulary_id='CPT4', vocabulary_name='CPT4')
    VocabularyFactory(vocabulary_id='SNOMED', vocabulary_name='SNOMED')
    VocabularyFactory(vocabulary_id='RxNorm', vocabulary_name='RxNorm')
    DomainFactory(domain_id='Procedure', domain_name='Procedure')
    DomainFactory(domain_id='Drug', domain_name='Drug')
    ConceptClassFactory(concept_class_id='Procedure', concept_class_name='Procedure')
    ConceptClassFactory(concept_class_id='Clinical Drug', concept_class_name='Clinical Drug')


def _make_cpt_snomed_file(entries, tmp_dir):
    """Write a minimal CPT→SNOMED JSON file and return its path."""
    data = {}
    for e in entries:
        data[e['cptCode']] = e
    path = Path(tmp_dir) / 'cpt_to_snomed_map.json'
    path.write_text(json.dumps(data))
    return str(path)


def _make_snomed_rxnorm_file(entries, tmp_dir):
    """Write a minimal SNOMED→RxNorm JSON file and return its path."""
    path = Path(tmp_dir) / 'snomed_to_rxnorm_map.json'
    path.write_text(json.dumps(entries))
    return str(path)


class TestCptSnomedImport:

    def test_imports_valid_entries(self):
        _ensure_vocabs()
        snomed_vocab = VocabularyFactory(vocabulary_id='SNOMED', vocabulary_name='SNOMED')
        snomed_domain = DomainFactory(domain_id='Procedure', domain_name='Procedure')
        snomed_concept = ConceptFactory(
            concept_id=4001, concept_code='12345',
            concept_name='Some Procedure',
            vocabulary=snomed_vocab, domain=snomed_domain,
        )
        cpt_concept = ConceptFactory(
            concept_id=5001, concept_code='99213',
            concept_name='Office visit',
            vocabulary=VocabularyFactory(vocabulary_id='CPT4'),
            domain=snomed_domain,
        )

        entries = [{
            'cptConceptId': '5001',
            'cptCode': '99213',
            'cptDescriptor': 'Office or other outpatient visit',
            'snomedId': '4001',
            'snomedDescriptor': 'Some Procedure',
        }]

        with tempfile.TemporaryDirectory() as tmp:
            f = _make_cpt_snomed_file(entries, tmp)
            call_command(
                'import_etl_cross_maps',
                f'--cpt-snomed-file={f}',
                '--skip-rxnorm',
                stdout=StringIO(),
            )

        mapping = SourceCodeConceptMapping.objects.get(
            source_vocabulary_id='CPT4', source_code='99213',
        )
        assert mapping.target_concept_id == 4001
        assert mapping.source_concept_id == 5001  # FK set from pre-fetched concept
        assert mapping.status == 'approved'
        assert mapping.origin == 'import'
        assert mapping.origin_system == 'etl-cross-map'
        assert mapping.domain_id == 'Procedure'  # derived from target concept
        assert mapping.omop_table == 'procedure'
        assert mapping.destination_vocabulary_id == 'SNOMED'

    def test_skips_unmapped_entries(self):
        _ensure_vocabs()
        entries = [{
            'cptConceptId': '5002',
            'cptCode': '10004',
            'cptDescriptor': 'Fine needle aspiration',
            'snomedId': '0',
            'snomedDescriptor': '',
        }]

        with tempfile.TemporaryDirectory() as tmp:
            f = _make_cpt_snomed_file(entries, tmp)
            out = StringIO()
            call_command(
                'import_etl_cross_maps',
                f'--cpt-snomed-file={f}',
                '--skip-rxnorm',
                stdout=out,
            )

        assert SourceCodeConceptMapping.objects.filter(
            source_vocabulary_id='CPT4', source_code='10004',
        ).count() == 0
        assert '1 unmapped' in out.getvalue()

    def test_idempotent_rerun(self):
        """Re-running the command does not duplicate rows."""
        _ensure_vocabs()
        snomed_concept = ConceptFactory(
            concept_id=4002, concept_code='67890',
            vocabulary=VocabularyFactory(vocabulary_id='SNOMED'),
            domain=DomainFactory(domain_id='Procedure'),
        )
        entries = [{
            'cptConceptId': '5003',
            'cptCode': '99214',
            'cptDescriptor': 'Office visit established',
            'snomedId': '4002',
            'snomedDescriptor': '',
        }]

        with tempfile.TemporaryDirectory() as tmp:
            f = _make_cpt_snomed_file(entries, tmp)
            call_command('import_etl_cross_maps', f'--cpt-snomed-file={f}', '--skip-rxnorm', stdout=StringIO())
            call_command('import_etl_cross_maps', f'--cpt-snomed-file={f}', '--skip-rxnorm', stdout=StringIO())

        assert SourceCodeConceptMapping.objects.filter(
            source_vocabulary_id='CPT4', source_code='99214',
        ).count() == 1


class TestSnomedRxnormImport:

    def test_imports_valid_entries(self):
        _ensure_vocabs()
        rxnorm_concept = ConceptFactory(
            concept_id=6001, concept_code='483117',
            concept_name='Aspirin 325 MG',
            vocabulary=VocabularyFactory(vocabulary_id='RxNorm'),
            domain=DomainFactory(domain_id='Drug'),
        )
        snomed_concept = ConceptFactory(
            concept_id=7001, concept_code='102002',
            concept_name='Aspirin substance',
            vocabulary=VocabularyFactory(vocabulary_id='SNOMED'),
            domain=DomainFactory(domain_id='Drug'),
        )

        data = {'102002': '483117'}

        with tempfile.TemporaryDirectory() as tmp:
            f = _make_snomed_rxnorm_file(data, tmp)
            call_command(
                'import_etl_cross_maps',
                f'--snomed-rxnorm-file={f}',
                '--skip-cpt',
                stdout=StringIO(),
            )

        mapping = SourceCodeConceptMapping.objects.get(
            source_vocabulary_id='SNOMED', source_code='102002',
        )
        assert mapping.target_concept_id == rxnorm_concept.concept_id
        assert mapping.status == 'approved'
        assert mapping.destination_vocabulary_id == 'RxNorm'
        assert mapping.domain_id == 'Drug'
        assert mapping.omop_table == 'drug_exposure'

    def test_skips_missing_rxnorm_concept(self):
        """When the RxNorm code is not in the DB, the entry is skipped."""
        _ensure_vocabs()
        data = {'999999': '999999'}  # Neither code exists in DB

        with tempfile.TemporaryDirectory() as tmp:
            f = _make_snomed_rxnorm_file(data, tmp)
            out = StringIO()
            call_command(
                'import_etl_cross_maps',
                f'--snomed-rxnorm-file={f}',
                '--skip-cpt',
                stdout=out,
            )

        assert SourceCodeConceptMapping.objects.filter(
            source_vocabulary_id='SNOMED', source_code='999999',
        ).count() == 0
        assert '1 missing target' in out.getvalue()

    def test_idempotent_rerun(self):
        _ensure_vocabs()
        rxnorm_concept = ConceptFactory(
            concept_id=6002, concept_code='98297',
            vocabulary=VocabularyFactory(vocabulary_id='RxNorm'),
            domain=DomainFactory(domain_id='Drug'),
        )
        data = {'120006': '98297'}

        with tempfile.TemporaryDirectory() as tmp:
            f = _make_snomed_rxnorm_file(data, tmp)
            call_command('import_etl_cross_maps', f'--snomed-rxnorm-file={f}', '--skip-cpt', stdout=StringIO())
            call_command('import_etl_cross_maps', f'--snomed-rxnorm-file={f}', '--skip-cpt', stdout=StringIO())

        assert SourceCodeConceptMapping.objects.filter(
            source_vocabulary_id='SNOMED', source_code='120006',
        ).count() == 1


class TestDryRun:

    def test_dry_run_creates_no_rows(self):
        _ensure_vocabs()
        snomed_concept = ConceptFactory(
            concept_id=4010, concept_code='11111',
            vocabulary=VocabularyFactory(vocabulary_id='SNOMED'),
            domain=DomainFactory(domain_id='Procedure'),
        )
        entries = [{
            'cptConceptId': '5010',
            'cptCode': '99215',
            'cptDescriptor': 'Office visit',
            'snomedId': '4010',
            'snomedDescriptor': '',
        }]

        before = SourceCodeConceptMapping.objects.count()

        with tempfile.TemporaryDirectory() as tmp:
            f = _make_cpt_snomed_file(entries, tmp)
            out = StringIO()
            call_command(
                'import_etl_cross_maps',
                f'--cpt-snomed-file={f}',
                '--skip-rxnorm',
                '--dry-run',
                stdout=out,
            )

        assert SourceCodeConceptMapping.objects.count() == before
        assert 'Would create' in out.getvalue()


class TestConflictLogging:

    def test_logs_warning_when_existing_target_differs(self):
        """When athena already mapped a CPT code to a different SNOMED concept,
        the command should log a warning."""
        _ensure_vocabs()
        snomed_domain = DomainFactory(domain_id='Procedure')
        snomed_vocab = VocabularyFactory(vocabulary_id='SNOMED')
        old_target = ConceptFactory(
            concept_id=4020, concept_code='OLD',
            vocabulary=snomed_vocab, domain=snomed_domain,
        )
        new_target = ConceptFactory(
            concept_id=4021, concept_code='NEW',
            vocabulary=snomed_vocab, domain=snomed_domain,
        )

        # Pre-existing row from athena sync
        SourceCodeConceptMapping.objects.create(
            source_vocabulary_id='CPT4',
            source_code='99216',
            target_concept=old_target,
            destination_vocabulary_id='SNOMED',
            domain_id='Procedure',
            omop_table='procedure',
            status='approved',
            origin='import',
            origin_system='athena',
            source='Athena',
        )

        entries = [{
            'cptConceptId': '5020',
            'cptCode': '99216',
            'cptDescriptor': 'Office visit',
            'snomedId': '4021',
            'snomedDescriptor': '',
        }]

        with tempfile.TemporaryDirectory() as tmp:
            f = _make_cpt_snomed_file(entries, tmp)
            out = StringIO()
            call_command(
                'import_etl_cross_maps',
                f'--cpt-snomed-file={f}',
                '--skip-rxnorm',
                stdout=out,
            )

        # The existing row should NOT be overwritten
        mapping = SourceCodeConceptMapping.objects.get(
            source_vocabulary_id='CPT4', source_code='99216',
        )
        assert mapping.target_concept_id == 4020  # unchanged
        assert '1 conflicts' in out.getvalue()


class TestLimitFlag:

    def test_limit_caps_rows_processed(self):
        _ensure_vocabs()
        snomed_vocab = VocabularyFactory(vocabulary_id='SNOMED')
        snomed_domain = DomainFactory(domain_id='Procedure')
        for i in range(5):
            ConceptFactory(
                concept_id=4100 + i, concept_code=str(40000 + i),
                vocabulary=snomed_vocab, domain=snomed_domain,
            )

        entries = [
            {
                'cptConceptId': str(5100 + i),
                'cptCode': str(99300 + i),
                'cptDescriptor': f'Procedure {i}',
                'snomedId': str(4100 + i),
                'snomedDescriptor': '',
            }
            for i in range(5)
        ]

        with tempfile.TemporaryDirectory() as tmp:
            f = _make_cpt_snomed_file(entries, tmp)
            call_command(
                'import_etl_cross_maps',
                f'--cpt-snomed-file={f}',
                '--skip-rxnorm',
                '--limit=2',
                stdout=StringIO(),
            )

        assert SourceCodeConceptMapping.objects.filter(
            source_vocabulary_id='CPT4',
            source_code__in=[str(99300 + i) for i in range(5)],
        ).count() == 2
