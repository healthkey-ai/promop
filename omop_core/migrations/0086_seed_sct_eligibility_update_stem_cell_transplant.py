from django.db import migrations

# Mapping from the old 13-value vocabulary to the new 3-value set.
_OLD_TO_NEW_SCT = {
    'prior SCT':                    'autologous SCT',
    'prior autologous SCT':         'autologous SCT',
    'prior allogeneic SCT':         'allogeneic SCT',
    'recent SCT':                   'autologous SCT',
    'recent autologous SCT':        'autologous SCT',
    'recent allogeneic SCT':        'allogeneic SCT',
    'relapsed post-SCT':            'autologous SCT',
    'relapsed post-autologous SCT': 'autologous SCT',
    'relapsed post-allogeneic SCT': 'allogeneic SCT',
    'completed tandem SCT':         'tandem SCT',
    'never received SCT':           None,          # no equivalent — cleared
    'pre-autologous SCT':           'autologous SCT',
    'pre-allogeneic SCT':           'allogeneic SCT',
}


def migrate_patientinfo_sct_history(apps, schema_editor):
    """Remap old stem_cell_transplant_history strings to the new 3-value vocabulary."""
    PatientInfo = apps.get_model('omop_core', 'PatientInfo')
    qs = PatientInfo.objects.exclude(
        stem_cell_transplant_history=[]
    ).exclude(
        stem_cell_transplant_history__isnull=True
    )
    rows_to_update = []
    for pi in qs.iterator():
        old = pi.stem_cell_transplant_history or []
        new = []
        for v in old:
            mapped = _OLD_TO_NEW_SCT.get(v)
            if mapped and mapped not in new:
                new.append(mapped)
        if old != new:
            pi.stem_cell_transplant_history = new
            rows_to_update.append(pi)
    if rows_to_update:
        PatientInfo.objects.bulk_update(rows_to_update, ['stem_cell_transplant_history'])


def reverse_migrate_patientinfo_sct_history(apps, schema_editor):
    # Best-effort reverse: clear any values that matched new vocab (can't recover originals).
    pass


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
        # Remap PatientInfo rows BEFORE truncating the vocabulary table.
        migrations.RunPython(migrate_patientinfo_sct_history, reverse_migrate_patientinfo_sct_history),
        migrations.RunPython(replace_stem_cell_transplant_values, reverse_replace_stem_cell_transplant_values),
    ]
