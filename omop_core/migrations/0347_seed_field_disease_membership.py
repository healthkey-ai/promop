"""Seed each disease's extension from the fields its tab renders today (#2331).

MM, BC, FL, MCL and CLL start with the fields ``DiseaseTab.tsx`` renders for
each. Every other field has no row and is part of the base record. Additive:
a row already present is left alone, and a category this instance's curators
deleted gets no members. Read-only as far as patients go: no tab and no save
consults these rows yet.

Historical models and this file's own copy of the rows only (#2440): what a
fresh install seeds must not depend on the code it is installed from.
``FIELD_DISEASE_SEED`` is read by tests/test_disease_profiles.py, which fails
when ``disease_profiles.SEED_DISEASE_FIELDS`` gains a field that no migration
writes. A later migration that adds rows declares its own ``FIELD_DISEASE_SEED``.
"""
from django.db import migrations

SEED_PROVENANCE = 'disease-tab-seed v1'

FIELD_DISEASE_SEED = {
    'BC': frozenset({
        'histologic_type', 'tumor_grade', 'menopausal_status', 'tumor_stage', 'nodes_stage',
        'staging_modalities', 'distant_metastasis_stage', 'bone_only_metastasis_status',
        'measurable_disease_by_recist_status', 'estrogen_receptor_status',
        'progesterone_receptor_status', 'her2_status', 'hr_status', 'hrd_status',
        'androgen_receptor_status', 'ki67_proliferation_index', 'pd_l1_tumor_cells',
        'oncotype_dx_score', 'test_methodology', 'test_date', 'test_specimen_type',
        'report_interpretation', 'lymph_node_status', 'metastasis_status',
        'pd_l1_combined_positive_score', 'pd_l1_ic_percentage', 'pd_l1_assay',
    }),
    'FL': frozenset({
        'stage', 'tumor_grade', 'bulky_disease',
        'b_symptom_fever', 'b_symptom_night_sweats', 'b_symptom_weight_loss',
        'transformed_to_dlbcl',
        'dlbcl_transformation_date', 'post_transformation_outcome', 'bone_marrow_involvement',
        'clonal_bone_marrow_b_lymphocytes', 'ldh_upper_limit_normal', 'number_of_nodal_sites',
    }),
    'MM': frozenset({
        'myeloma_type', 'stage', 'progression', 'measurable_disease_imwg', 'mrd_status',
        'meets_crab', 'meets_slim', 'stem_cell_transplant_history', 'sct_date', 'sct_eligibility',
        'monoclonal_protein_serum', 'monoclonal_protein_urine', 'kappa_flc', 'lambda_flc',
        'kappa_lambda_ratio', 'involved_uninvolved_ratio', 'bone_lesions', 'clonal_plasma_cells',
        'cytogenetic_markers',
    }),
    'CLL': frozenset({
        'binet_stage', 'tumor_burden', 'disease_activity', 'richter_transformation',
        'protein_expressions', 'absolute_lymphocyte_count', 'lymphocyte_doubling_time',
        'serum_beta2_microglobulin_level', 'clonal_b_lymphocyte_count',
        'clonal_bone_marrow_b_lymphocytes', 'qtcf_value', 'largest_lymph_node_size', 'spleen_size',
        'bone_marrow_involvement', 'measurable_disease_iwcll', 'splenomegaly', 'hepatomegaly',
        'lymphadenopathy', 'autoimmune_cytopenias_refractory_to_steroids',
        'btk_inhibitor_refractory', 'bcl2_inhibitor_refractory',
    }),
    'MCL': frozenset({
        'stage', 'protein_expressions', 'ki67_proliferation_index', 'lymphadenopathy',
        'splenomegaly', 'spleen_size', 'hepatomegaly', 'largest_lymph_node_size',
    }),
}


def seed(apps, schema_editor):
    Membership = apps.get_model('omop_core', 'FieldDiseaseMembership')
    Category = apps.get_model('omop_core', 'DiseaseCategoryChoice')
    known = set(Category.objects.values_list('code', flat=True))
    held = set(Membership.objects.values_list('field_name', 'disease'))
    Membership.objects.bulk_create([
        Membership(field_name=field_name, disease=code, provenance=SEED_PROVENANCE)
        for code, fields in sorted(FIELD_DISEASE_SEED.items())
        if code in known
        for field_name in sorted(fields)
        if (field_name, code) not in held
    ])


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0346_field_disease_membership'),
    ]

    operations = [
        # Rolling back drops the table with 0346, so there is nothing to undo
        # here. Re-applying after a partial rollback would re-add a row a
        # curator removed; the seed is meant to run once.
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
