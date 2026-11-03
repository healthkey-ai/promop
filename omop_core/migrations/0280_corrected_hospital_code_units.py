from django.db import migrations


SOURCE_URL = 'https://drive.google.com/drive/folders/11B-gJzFeEtl0xGxYNRnk6S57_ulkoTYP?usp=drive_link'
ARTIFACT_FILENAME = 'healthtree_hospital_codes_20261005_v2.zip'
ARTIFACT_IDENTITY = 'healthtree-hospital-seed-20261005-v2'
ARTIFACT_SHA256 = 'ad94793ee6f232154db33b9d65debab7d5565299ec382ec39d0923e863635c05'
EXPECTED_ROWS = 691_800


def queue_corrected_hospital_code_import(apps, schema_editor):
    HospitalCodeImport = apps.get_model('omop_core', 'HospitalCodeImport')
    HospitalCodeImport.objects.update_or_create(
        artifact_identity=ARTIFACT_IDENTITY,
        defaults={
            'source_url': SOURCE_URL,
            'artifact_filename': ARTIFACT_FILENAME,
            'artifact_sha256': ARTIFACT_SHA256,
            'expected_rows': EXPECTED_ROWS,
            'outcome': 'queued',
            'task_id': '',
            'stats': {},
            'failure_reason': '',
            'started_at': None,
            'completed_at': None,
        },
    )


def unqueue_corrected_hospital_code_import(apps, schema_editor):
    HospitalCodeImport = apps.get_model('omop_core', 'HospitalCodeImport')
    HospitalCodeImport.objects.filter(
        artifact_identity=ARTIFACT_IDENTITY,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0279_field_mapping_provenance_text')]

    operations = [
        # Record durable intent only. The worker downloads and applies the
        # checksum-pinned artifact after deployment.
        migrations.RunPython(
            queue_corrected_hospital_code_import,
            unqueue_corrected_hospital_code_import,
        ),
    ]
