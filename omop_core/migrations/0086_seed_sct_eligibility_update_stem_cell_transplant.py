from django.db import migrations


def seed_sct_eligibility(apps, schema_editor):
    SctEligibility = apps.get_model('omop_core', 'SctEligibility')
    for code, title in [
        ('eligibleAuto',   'eligible for autologous SCT'),
        ('eligibleAllo',   'eligible for allogeneic SCT'),
        ('ineligibleAuto', 'ineligible for autologous SCT'),
        ('ineligibleAllo', 'ineligible for allogeneic SCT'),
    ]:
        SctEligibility.objects.get_or_create(code=code, defaults={'title': title})


def replace_stem_cell_transplant_values(apps, schema_editor):
    StemCellTransplant = apps.get_model('omop_core', 'StemCellTransplant')
    StemCellTransplant.objects.all().delete()
    for code, title in [
        ('autologousSCT', 'autologous SCT'),
        ('allogeneicSCT', 'allogeneic SCT'),
        ('tandemSCT',     'tandem SCT'),
    ]:
        StemCellTransplant.objects.get_or_create(code=code, defaults={'title': title})


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0085_scteligibility_patientinfo_sct_date_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_sct_eligibility, reverse_noop),
        migrations.RunPython(replace_stem_cell_transplant_values, reverse_noop),
    ]
