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
    """Remap old stem_cell_transplant_history strings to the new 3-value vocabulary.

    - Values in _OLD_TO_NEW_SCT are remapped to their new equivalents.
    - 'never received SCT' maps to None and is intentionally cleared (removed from the list).
    - Non-string items (e.g. dicts written by the BQ loader) are skipped and logged.
    - Strings NOT in _OLD_TO_NEW_SCT are preserved as-is with a warning rather than
      silently dropped. Run `manage.py audit_sct_history` before applying this migration
      to production to surface any such values.
    """
    import logging
    logger = logging.getLogger(__name__)

    PatientInfo = apps.get_model('omop_core', 'PatientInfo')
    qs = PatientInfo.objects.exclude(
        stem_cell_transplant_history=[]
    ).exclude(
        stem_cell_transplant_history__isnull=True
    )
    rows_to_update = []
    unrecognized: dict = {}

    for pi in qs.iterator():
        old = pi.stem_cell_transplant_history or []
        new = []
        for v in old:
            if not isinstance(v, str):
                # Non-string (e.g. dict from old BQ loader) — skip and report.
                key = f'<{type(v).__name__}>'
                unrecognized[key] = unrecognized.get(key, 0) + 1
                continue
            if v not in _OLD_TO_NEW_SCT:
                # Unrecognized string — preserve rather than silently drop.
                unrecognized[v] = unrecognized.get(v, 0) + 1
                if v not in new:
                    new.append(v)
                continue
            mapped = _OLD_TO_NEW_SCT[v]
            # mapped is None → 'never received SCT', intentionally cleared (not added to new).
            if mapped is not None and mapped not in new:
                new.append(mapped)

        if old != new:
            pi.stem_cell_transplant_history = new
            rows_to_update.append(pi)

    if unrecognized:
        logger.warning(
            "migrate_patientinfo_sct_history: %d unrecognized SCT value(s) preserved as-is "
            "(not in _OLD_TO_NEW_SCT): %s — run `manage.py audit_sct_history` to review.",
            sum(unrecognized.values()),
            unrecognized,
        )

    if rows_to_update:
        PatientInfo.objects.bulk_update(rows_to_update, ['stem_cell_transplant_history'])


def reverse_migrate_patientinfo_sct_history(apps, schema_editor):
    # Original SCT history strings cannot be recovered after remapping.
    # Rolling back to migration 0085 will leave stem_cell_transplant_history values
    # in the new 3-value format, inconsistent with the restored 13-value vocabulary.
    # Manual data correction is required after a rollback.
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
    new_codes = {'autologousSCT', 'allogeneicSCT', 'tandemSCT'}
    # Delete only old rows not in the new 3-value set, preserving any future additions.
    StemCellTransplant.objects.exclude(code__in=new_codes).delete()
    for code, title in [
        ('autologousSCT', 'autologous SCT'),
        ('allogeneicSCT', 'allogeneic SCT'),
        ('tandemSCT',     'tandem SCT'),
    ]:
        StemCellTransplant.objects.get_or_create(code=code, defaults={'title': title})


def reverse_replace_stem_cell_transplant_values(apps, schema_editor):
    """Restore the original 13 StemCellTransplant vocabulary values."""
    StemCellTransplant = apps.get_model('omop_core', 'StemCellTransplant')
    old_codes = {
        'priorSCT', 'priorAutologousSCT', 'priorAllogeneicSCT', 'recentSCT',
        'recentAutologousSCT', 'recentAllogeneicSCT', 'relapsedPostSCT',
        'relapsedPostAutologousSCT', 'relapsedPostAllogeneicSCT', 'completedTandemSCT',
        'neverReceivedSCT', 'preAutologousSCT', 'preAllogeneicSCT',
    }
    StemCellTransplant.objects.exclude(code__in=old_codes).delete()
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
