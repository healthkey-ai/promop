"""Retain the migration node; SCCM reconciliation supersedes export evidence.

The removed export snapshot must not run on downstream deployments. Keep this
node to preserve the migration graph; 0259 performs the SCCM-based correction.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0257_sourcecodeconceptmapping_source_label_norm')]
    operations = []
