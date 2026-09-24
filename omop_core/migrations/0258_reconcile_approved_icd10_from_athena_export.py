"""Apply original Athena evidence to approved non-Athena mappings downstream.

0256 remains historical: it queried STCM, absent from the Athena download.
This new operation works offline on each instance's SCCM rows, preserves
reviewer sign-off, and changes only mapping definitions for future imports.
"""
import gzip
import hashlib
import json
from pathlib import Path

from django.db import migrations

SNAPSHOT = Path(__file__).resolve().parent.parent / 'data' / 'athena_reconciliation_20260921' / 'evidence.json.gz'
SHA256 = '5f161d927a8feb022b09f3c06a474ff79b7d19a41c0b7f127dcecbdaf22f97dc'


def read_snapshot():
    raw = SNAPSHOT.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SHA256:
        raise ValueError('Athena approved-mapping snapshot checksum mismatch')
    return json.loads(gzip.decompress(raw))


def load_reconciliation(apps, schema_editor):
    from omop_core.data_migrations.athena_approved_reconcile_v1 import reconcile, summarize
    payload = read_snapshot()
    receipts = reconcile(apps, schema_editor.connection, payload,
                         reference=f'Athena export {payload["as_of"]}; snapshot sha256={SHA256}')
    print('Athena approved ICD-10 reconciliation (0258): ' + json.dumps(summarize(receipts), sort_keys=True))


class Migration(migrations.Migration):
    atomic = False  # Each bounded batch locks, rechecks and commits independently.
    dependencies = [('omop_core', '0257_sourcecodeconceptmapping_source_label_norm')]
    # Reversal retains corrections instead of overwriting subsequent curator edits.
    operations = [migrations.RunPython(load_reconciliation, migrations.RunPython.noop)]
