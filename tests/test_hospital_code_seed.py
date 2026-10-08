import hashlib
import importlib
import json
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from omop_core.models import HospitalCodeImport, SourceCodeConceptMapping
from omop_core.services import hospital_code_seed


pytestmark = pytest.mark.django_db
EPIC = 'urn:oid:1.2.840.114350.1.13.90.2.7.2.707679'
IDENTITY = 'test-hospital-seed-v1'


def seed_archive(tmp_path, rows):
    member = tmp_path / hospital_code_seed.MEMBER_NAME
    member.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    member_sha = hashlib.sha256(member.read_bytes()).hexdigest()
    manifest = {
        'schema_version': 1,
        'artifact_identity': IDENTITY,
        'member': hospital_code_seed.MEMBER_NAME,
        'member_sha256': member_sha,
        'counts': {'total': len(rows), 'epic': len(rows), 'cerner': 0},
    }
    archive = tmp_path / 'seed.zip'
    with ZipFile(archive, 'w', compression=ZIP_DEFLATED) as output:
        output.write(member, hospital_code_seed.MEMBER_NAME)
        output.writestr('manifest.json', json.dumps(manifest))
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def row(code='ALB'):
    return {
        'source_vocabulary_id': EPIC,
        'source_code': code,
        'source_code_description': 'Albumin',
        'occurrence_count': 12,
        'source_group_occurrence_count': 20,
        'source_metadata': {
            'records': 12,
            'facilities': [{
                'name': 'University Hospital', 'level': 'brand',
                'confidence': 'high', 'records': 12,
            }],
        },
        'domain_id': 'Measurement',
        'omop_table': 'measurement',
        'source_unit_evidence': [{
            'display': 'g/dL', 'code': 'g/dL', 'normalized': 'g/dL',
            'verdict': 'valid', 'count': 12,
            'distribution': {'p25': 3.5, 'p50': 4.1, 'p75': 4.6},
        }],
    }


def job_for(archive, digest, expected_rows=1):
    return HospitalCodeImport.objects.create(
        source_url='https://example.test/folder', artifact_filename=archive.name,
        artifact_identity=IDENTITY, artifact_sha256=digest,
        expected_rows=expected_rows, outcome='queued',
    )


def test_import_archive_preserves_curator_decision_and_adds_evidence(tmp_path):
    archive, digest = seed_archive(tmp_path, [row()])
    job = job_for(archive, digest)
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=EPIC, source_code='ALB',
        source_code_description='Curator label', occurrence_count=2,
        status='rejected', origin='curator', notes='Keep this decision',
    )

    stats = hospital_code_seed.import_archive(archive, job, batch_size=1)

    mapping.refresh_from_db()
    assert stats['total'] == 1
    assert stats['existing'] == 1
    assert mapping.status == 'rejected'
    assert mapping.notes == 'Keep this decision'
    assert mapping.source_code_description == 'Curator label'
    assert mapping.source_metadata['facilities'][0]['name'] == 'University Hospital'
    assert mapping.source_unit_evidence[0]['normalized'] == 'g/dL'
    assert mapping.source_unit_evidence[0]['distribution']['p50'] == 4.1


def test_validation_rejects_checksum_before_writing(tmp_path):
    archive, _digest = seed_archive(tmp_path, [row()])
    job = job_for(archive, '0' * 64)

    with pytest.raises(Exception, match='SHA-256 mismatch'):
        hospital_code_seed.import_archive(archive, job)

    assert not SourceCodeConceptMapping.objects.exists()


def test_migration_only_records_durable_intent():
    migration = importlib.import_module('omop_core.migrations.0276_hospital_code_import')

    with patch.object(hospital_code_seed, 'download_seed') as download:
        from django.apps import apps
        migration.queue_hospital_code_import(apps, None)

    queued = HospitalCodeImport.objects.get(
        artifact_identity=migration.ARTIFACT_IDENTITY,
    )
    assert queued.outcome == 'queued'
    assert queued.expected_rows == 691_800
    assert queued.artifact_filename == 'healthtree_hospital_codes_20261002_v1.zip'
    download.assert_not_called()


def test_dispatch_publishes_queued_intent(tmp_path):
    archive, digest = seed_archive(tmp_path, [row()])
    job = job_for(archive, digest)
    with patch('omop_core.tasks.import_hospital_code_seed_task.apply_async') as publish:
        assert hospital_code_seed.dispatch_queued_imports() == 1

        publish.assert_called_once_with(
            args=[job.pk], task_id=f'hospital-code-import-{job.pk}',
        )
    job.refresh_from_db()
    assert job.task_id == f'hospital-code-import-{job.pk}'


def test_import_task_is_redelivered_when_worker_is_lost():
    from omop_core.tasks import import_hospital_code_seed_task

    assert import_hospital_code_seed_task.acks_late is True
    assert import_hospital_code_seed_task.reject_on_worker_lost is True


@pytest.mark.parametrize('outcome, should_requeue', [
    ('running', True),
    ('applied', False),
    ('failed', False),
])
def test_recovery_migration_only_requeues_interrupted_v1(outcome, should_requeue):
    from django.apps import apps
    from django.utils import timezone

    migration = importlib.import_module(
        'omop_core.migrations.0277_requeue_interrupted_hospital_code_import',
    )
    job = HospitalCodeImport.objects.create(
        source_url='https://example.test/folder',
        artifact_filename='healthtree_hospital_codes_20261002_v1.zip',
        artifact_identity=migration.ARTIFACT_IDENTITY,
        artifact_sha256='1' * 64,
        expected_rows=691_800,
        outcome=outcome,
        task_id='hospital-code-import-1',
        stats={'partial': 860},
        failure_reason='existing state',
        started_at=timezone.now(),
        completed_at=timezone.now() if outcome != 'running' else None,
    )

    migration.requeue_interrupted_import(apps, None)

    job.refresh_from_db()
    if should_requeue:
        assert job.outcome == 'queued'
        assert job.task_id == ''
        assert job.stats == {}
        assert job.started_at is None
        assert job.completed_at is None
        assert 'worker replacement' in job.failure_reason
    else:
        assert job.outcome == outcome
        assert job.task_id == 'hospital-code-import-1'
        assert job.stats == {'partial': 860}


def test_corrected_unit_migration_records_new_durable_intent():
    from django.apps import apps

    migration = importlib.import_module(
        'omop_core.migrations.0280_corrected_hospital_code_units',
    )

    with patch.object(hospital_code_seed, 'download_seed') as download:
        migration.queue_corrected_hospital_code_import(apps, None)

    queued = HospitalCodeImport.objects.get(
        artifact_identity=migration.ARTIFACT_IDENTITY,
    )
    assert queued.outcome == 'queued'
    assert queued.task_id == ''
    assert queued.expected_rows == 691_800
    assert queued.artifact_filename == 'healthtree_hospital_codes_20261005_v2.zip'
    assert queued.artifact_sha256 == (
        'ad94793ee6f232154db33b9d65debab7d5565299ec382ec39d0923e863635c05'
    )
    download.assert_not_called()
