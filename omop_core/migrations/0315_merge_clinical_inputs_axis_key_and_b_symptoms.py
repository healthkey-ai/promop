"""Join the dev and cb-v2 leaves after the 2026-10-28 back-merge.

``0308_merge_clinical_inputs_and_unit_evidence`` closes the dev source-input and
unit-evidence branches, ``0313_widen_axis_key`` closes the cb-v2 chain that runs
through ``0309``-``0312``, and ``0314_b_symptom_mappings`` closes the dev
B-symptom branch. This migration changes no data; it only leaves the app with a
single leaf so ``migrate`` stops refusing to run.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0308_merge_clinical_inputs_and_unit_evidence'),
        ('omop_core', '0313_widen_axis_key'),
        ('omop_core', '0314_b_symptom_mappings'),
    ]

    operations = []
