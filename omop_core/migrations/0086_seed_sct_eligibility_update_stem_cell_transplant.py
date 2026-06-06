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


def reverse_seed_sct_eligibility(apps, schema_editor):
    SctEligibility = apps.get_model('omop_core', 'SctEligibility')
    SctEligibility.objects.filter(code__in=[
        'eligibleAuto', 'eligibleAllo', 'ineligibleAuto', 'ineligibleAllo',
    ]).delete()


def replace_stem_cell_transplant_values(apps, schema_editor):
    StemCellTransplant = apps.get_model('omop_core', 'StemCellTransplant')
    StemCellTransplant.objects.all().delete()
    for code, title in [
        ('autologousSCT', 'autologous SCT'),
        ('allogeneicSCT', 'allogeneic SCT'),
        ('tandemSCT',     'tandem SCT'),
    ]:
        StemCellTransplant.objects.get_or_create(code=code, defaults={'title': title})


def reverse_replace_stem_cell_transplant_values(apps, schema_editor):
    """Restore the original 13 StemCellTransplant vocabulary values."""
    StemCellTransplant = apps.get_model('omop_core', 'StemCellTransplant')
    StemCellTransplant.objects.all().delete()
    for code, title in [
        ('priorSCT',                  'prior SCT'),
        ('priorAutologousSCT',        'prior autologous SCT'),
        ('priorAllogeneicSCT',        'prior allogeneic SCT'),
        ('recentSCT',                 'recent SCT'),
        ('recentAutologousSCT',       'recent autologous SCT'),
        ('recentAllogeneicSCT',       'recent allogeneic SCT'),
        ('relapsedPostSCT',           'relapsed post-SCT'),
        ('relapsedPostAutologousSCT', 'relapsed post-autologous SCT'),
        ('relapsedPostAllogeneicSCT', 'relapsed post-allogeneic SCT'),
        ('completedTandemSCT',        'completed tandem SCT'),
        ('neverReceivedSCT',          'never received SCT'),
        ('preAutologousSCT',          'pre-autologous SCT'),
        ('preAllogeneicSCT',          'pre-allogeneic SCT'),
    ]:
        StemCellTransplant.objects.get_or_create(code=code, defaults={'title': title})


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0085_scteligibility_patientinfo_sct_date_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_sct_eligibility, reverse_seed_sct_eligibility),
        migrations.RunPython(replace_stem_cell_transplant_values, reverse_replace_stem_cell_transplant_values),
    ]
