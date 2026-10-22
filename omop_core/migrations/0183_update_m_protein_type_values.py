"""Update the Multiple Myeloma M-Protein Type vocabulary values."""

from django.db import migrations


REQUESTED_M_PROTEIN_TYPES = [
    ('igg-kappa', 'IgG kappa'),
    ('igg-lambda', 'IgG lambda'),
    ('iga-kappa', 'IgA kappa'),
    ('iga-lambda', 'IgA lambda'),
    ('igd-kappa', 'IgD kappa'),
    ('igd-lambda', 'IgD lambda'),
    ('ige-kappa', 'IgE kappa'),
    ('ige-lambda', 'IgE lambda'),
    ('igm-kappa', 'IgM kappa'),
    ('igm-lambda', 'IgM lambda'),
    ('light-chain-kappa', 'Light-chain kappa'),
    ('light-chain-lambda', 'Light-chain lambda'),
]

REMOVED_M_PROTEIN_TYPE_CODES = {
    'non-secretory',
    'biclonal',
}

TITLE_RENAMES = {
    'IgG Kappa': 'IgG kappa',
    'IgG Lambda': 'IgG lambda',
    'IgA Kappa': 'IgA kappa',
    'IgA Lambda': 'IgA lambda',
    'IgD Kappa': 'IgD kappa',
    'IgD Lambda': 'IgD lambda',
    'IgE Kappa': 'IgE kappa',
    'IgE Lambda': 'IgE lambda',
    'IgM Kappa': 'IgM kappa',
    'IgM Lambda': 'IgM lambda',
    'Light Chain Only (Kappa)': 'Light-chain kappa',
    'Light Chain Only (Lambda)': 'Light-chain lambda',
}

PREVIOUS_MYELOMA_TYPES = [
    ('igg-kappa', 'IgG Kappa'),
    ('igg-lambda', 'IgG Lambda'),
    ('iga-kappa', 'IgA Kappa'),
    ('iga-lambda', 'IgA Lambda'),
    ('igd-kappa', 'IgD Kappa'),
    ('igd-lambda', 'IgD Lambda'),
    ('ige-kappa', 'IgE Kappa'),
    ('ige-lambda', 'IgE Lambda'),
    ('igm-kappa', 'IgM Kappa'),
    ('igm-lambda', 'IgM Lambda'),
    ('light-chain-kappa', 'Light Chain Only (Kappa)'),
    ('light-chain-lambda', 'Light Chain Only (Lambda)'),
    ('non-secretory', 'Non-secretory'),
    ('biclonal', 'Biclonal'),
]


def update_m_protein_types(apps, schema_editor):
    MyelomaType = apps.get_model('omop_core', 'MyelomaType')
    PatientRecord = apps.get_model('omop_core', 'PatientRecord')
    for sort_order, (code, title) in enumerate(REQUESTED_M_PROTEIN_TYPES):
        MyelomaType.objects.update_or_create(
            code=code,
            defaults={'title': title, 'sort_key': sort_order}
            if any(f.name == 'sort_key' for f in MyelomaType._meta.get_fields())
            else {'title': title},
        )
    MyelomaType.objects.filter(code__in=REMOVED_M_PROTEIN_TYPE_CODES).delete()
    for old_title, new_title in TITLE_RENAMES.items():
        PatientRecord.objects.filter(myeloma_type=old_title).update(
            myeloma_type=new_title,
        )


def restore_previous_myeloma_types(apps, schema_editor):
    MyelomaType = apps.get_model('omop_core', 'MyelomaType')
    PatientRecord = apps.get_model('omop_core', 'PatientRecord')
    for sort_order, (code, title) in enumerate(PREVIOUS_MYELOMA_TYPES):
        MyelomaType.objects.update_or_create(
            code=code,
            defaults={'title': title, 'sort_key': sort_order}
            if any(f.name == 'sort_key' for f in MyelomaType._meta.get_fields())
            else {'title': title},
        )
    for old_title, new_title in TITLE_RENAMES.items():
        PatientRecord.objects.filter(myeloma_type=new_title).update(
            myeloma_type=old_title,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0181_seed_refractory_field_mappings'),
    ]

    operations = [
        migrations.RunPython(update_m_protein_types, restore_previous_myeloma_types),
    ]
