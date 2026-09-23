"""Ship verified Athena ICD-10 mappings and curator-selectable alternatives.

Offline and additive to patient facts. Uses historical models and a checksummed
snapshot; no local download directory, network, or deployment credential needed.
"""
import gzip
import hashlib
import json
from pathlib import Path

from django.db import migrations

SNAPSHOT = Path(__file__).resolve().parent.parent / 'data' / 'athena_recovery_20260921' / 'mappings.json.gz'
SHA256 = 'daf08d1818d12cdec6ecab5385f331b6b65a41b2ac33bea46d42030d248e3ba5'


def read_snapshot():
    raw = SNAPSHOT.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SHA256:
        raise ValueError('Athena recovery snapshot checksum mismatch')
    return json.loads(gzip.decompress(raw))


def load_mappings(apps, schema_editor):
    from omop_core.data_migrations.athena_recovery_v1 import reconcile, summarize
    receipts = reconcile(apps, schema_editor.connection, read_snapshot())
    print('Athena ICD-10 recovery:', summarize(receipts))


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('omop_core', '0252_genomic_feature_recipes')]
    operations = [migrations.RunPython(load_mappings, migrations.RunPython.noop)]
