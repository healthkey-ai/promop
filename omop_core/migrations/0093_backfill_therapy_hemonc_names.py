"""
0093_backfill_therapy_hemonc_names.py

Data migration: normalise first_line_therapy, second_line_therapy, later_therapy
text values to canonical HemOnc concept_names and populate the matching
*_therapy_id / later_therapy_ids concept-id fields.

Mapping built from:
  SELECT concept_id, concept_name
  FROM concept
  WHERE vocabulary_id = 'HemOnc' AND concept_class_id = 'Regimen'
    AND invalid_reason IS NULL
on the dev DB (2026-06-16), cross-referenced against the distinct therapy
values observed in patient_info.
"""

from django.db import migrations

# ---------------------------------------------------------------------------
# Mapping: existing free-text value  →  (canonical HemOnc name, concept_id)
# concept_id = None  means the regimen exists in HemOnc but its concept is
# absent from this vocabulary load (or truly has no HemOnc entry).
# ---------------------------------------------------------------------------

_THERAPY_MAP: dict[str, tuple[str, int | None]] = {
    # ── Multiple Myeloma ───────────────────────────────────────────────────
    'VRd':                                                          ('RVD',                                        35806260),
    'VRd (Bortezomib, Lenalidomide, and Dexamethasone)':           ('RVD',                                        35806260),
    'VRd Lite (Bortezomib, Lenalidomide, and Dexamethasone)':      ('RVD',                                        35806260),
    'KRd':                                                          ('KRd',                                        35806284),
    'KRd (Carfilzomib, Lenalidomide, and Dexamethasone)':          ('KRd',                                        35806284),
    'DRd':                                                          ('Dara-Rd',                                    35806311),
    'Dara-Rd (Daratumumab, Lenalidomide, and Dexamethasone)':      ('Dara-Rd',                                    35806311),
    'Dara-VRd (Daratumumab, Bortezomib, Lenalidomide, and Dexamethasone)': ('Dara-RVd',                           911993),
    'DVd':                                                          ('Dara-Vd',                                    35806312),
    'DKRd':                                                         ('Dara-KRd',                                   905602),
    'Rd':                                                           ('Lenalidomide and Dexamethasone (Rd)',         35806053),
    'Td':                                                           ('Thalidomide and Dexamethasone (TD)',          35806268),
    'VCd':                                                          ('VCd',                                        None),    # no HemOnc regimen
    'CyBorD (Cyclophosphamide, Bortezomib, and Dexamethasone)':    ('VCd',                                        None),    # same as VCd
    'IsaKRd':                                                       ('IsaKRd',                                     None),    # not in HemOnc load
    'Isa-VRd (Isatuximab, Bortezomib, Lenalidomide, and Dexamethasone)': ('Isa-RVd',                              37557069),
    'SVd':                                                          ('SVd',                                        905768),
    'SVd (Selinexor, Bortezomib, and Dexamethasone)':              ('SVd',                                        905768),
    'Sd':                                                           ('Selinexor and Dexamethasone (Sd)',            35100304),
    'Pd':                                                           ('Pomalidomide and Dexamethasone (Pd)',         42542407),
    'PomDex':                                                       ('Pomalidomide and Dexamethasone (Pd)',         42542407),
    'KPd':                                                          ('KPD',                                        35806324),
    'KPD':                                                          ('KPD',                                        35806324),
    'IsaPd':                                                        ('Isa-Pd',                                     911941),
    'DPd':                                                          ('Dara-Pd',                                    35806326),
    'EPd (Elotuzumab, Pomalidomide, and Dexamethasone)':           ('Elo-Pd',                                     35806313),
    'EloPd':                                                        ('Elo-Pd',                                     35806313),
    'VPd':                                                          ('VPd',                                        None),    # not in HemOnc load
    'IxaRd':                                                        ('IxaRd',                                      None),    # not in HemOnc load
    'Daratumumab (Darzalex/Darzalex Faspro) Monotherapy':          ('Daratumumab monotherapy',                    35806063),
    'Elotuzumab (Empliciti) Monotherapy':                           ('Elotuzumab (Empliciti) Monotherapy',         None),    # not in HemOnc load
    'Venetoclax Monotherapy':                                       ('Venetoclax monotherapy',                     35804617),
    'Selinexor (Xpovio)':                                           ('Selinexor monotherapy',                      905766),
    'Isatuximab (Sarclisa) Monotherapy':                            ('Isatuximab (Sarclisa) Monotherapy',          None),    # not in HemOnc load
    'Carfilzomib (Kyprolis) Monotherapy':                           ('Carfilzomib monotherapy',                    35806280),
    'Pomalidomide (Pomalyst) Monotherapy':                          ('Pomalidomide monotherapy',                   35806317),
    'Ixazomib (Ninlaro)':                                           ('Ixazomib monotherapy',                       35806064),
    'Belantamab mafodotin monotherapy':                             ('Belantamab mafodotin monotherapy',           911956),
    'Belantamab Mafodotin (Blenrep) Monotherapy':                  ('Belantamab mafodotin monotherapy',           911956),
    'Belantamab':                                                   ('Belantamab mafodotin monotherapy',           911956),
    'Ide-cel (Abecma) Monotherapy':                                 ('Idecabtagene vicleucel monotherapy',         905696),
    'Idecabtagene':                                                  ('Idecabtagene vicleucel monotherapy',         905696),
    'Cilta-cel (Carvykti) Monotherapy':                             ('Ciltacabtagene autoleucel monotherapy',      1525038),
    'Ciltacabtagene':                                                ('Ciltacabtagene autoleucel monotherapy',      1525038),
    'Teclistamab (Tecvayli) Monotherapy':                           ('Teclistamab monotherapy',                    37557075),
    'Teclistamab':                                                   ('Teclistamab monotherapy',                    37557075),
    'Melphalan+pred':                                               ('Melphalan and Prednisone (MP)',               35806056),
    'Cyclophosphamide or Melphalan Monotherapy':                    ('Cyclophosphamide or Melphalan Monotherapy',  None),

    # ── Breast Cancer ──────────────────────────────────────────────────────
    'AC-T':                                                         ('AC-T',                                       35101507),
    'TC':                                                           ('Cyclophosphamide and Docetaxel (TC)',         35804232),
    'Tamoxifen':                                                    ('Tamoxifen monotherapy',                      35804221),
    'Paclitaxel/Trastuzumab/Pertuzumab':                           ('THP (Paclitaxel)',                            1525210),
    'CDK4/6 Inhibitor + Letrozole':                                 ('CDK4/6 Inhibitor + Letrozole',               None),
    'Capecitabine':                                                 ('Capecitabine monotherapy',                   35804227),
    'T-DM1':                                                        ('Trastuzumab emtansine monotherapy',          35805230),
    'T-DXd':                                                        ('Trastuzumab deruxtecan monotherapy',         42542261),
    'Eribulin':                                                     ('Eribulin monotherapy',                       35804265),
    'Olaparib':                                                     ('Olaparib monotherapy',                       35804269),
    'Sacituzumab govitecan':                                        ('Sacituzumab govitecan monotherapy',          912024),
    'Vinorelbine':                                                  ('Vinorelbine monotherapy',                    35804241),
    'Pembrolizumab':                                                ('Pembrolizumab monotherapy',                  35803678),
    'Gemcitabine/Carboplatin':                                      ('Gemcitabine/Carboplatin',                    None),
}


def _migrate_therapy_fields(apps, schema_editor):
    PatientInfo = apps.get_model('omop_core', 'PatientInfo')
    updated = 0

    for pi in PatientInfo.objects.filter(
        first_line_therapy__isnull=False
    ) | PatientInfo.objects.filter(
        second_line_therapy__isnull=False
    ) | PatientInfo.objects.filter(
        later_therapy__isnull=False
    ):
        changes = {}
        changed = False

        # --- first line ---
        if pi.first_line_therapy and pi.first_line_therapy in _THERAPY_MAP:
            canonical, cid = _THERAPY_MAP[pi.first_line_therapy]
            if pi.first_line_therapy != canonical:
                changes['first_line_therapy'] = canonical
                changed = True
            if cid is not None and pi.first_line_therapy_id != cid:
                changes['first_line_therapy_id'] = cid
                changed = True

        # --- second line ---
        if pi.second_line_therapy and pi.second_line_therapy in _THERAPY_MAP:
            canonical, cid = _THERAPY_MAP[pi.second_line_therapy]
            if pi.second_line_therapy != canonical:
                changes['second_line_therapy'] = canonical
                changed = True
            if cid is not None and pi.second_line_therapy_id != cid:
                changes['second_line_therapy_id'] = cid
                changed = True

        # --- later line ---
        if pi.later_therapy and pi.later_therapy in _THERAPY_MAP:
            canonical, cid = _THERAPY_MAP[pi.later_therapy]
            if pi.later_therapy != canonical:
                changes['later_therapy'] = canonical
                changed = True
            if cid is not None:
                existing_ids = pi.later_therapy_ids or []
                if cid not in existing_ids:
                    changes['later_therapy_ids'] = existing_ids + [cid]
                    changed = True

        if changed:
            PatientInfo.objects.filter(pk=pi.pk).update(**changes)
            updated += 1

    print(f'  [0093] therapy backfill: updated {updated} PatientInfo rows')


def _noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0092_therapy_hemonc_concept_ids'),
    ]

    operations = [
        migrations.RunPython(_migrate_therapy_fields, _noop),
    ]
