"""Seed supportive therapies and disease outcome choices from cancerbot.

Source: trials/services/therapies_mapper.py and value_options.py (2026-09-10).
BC has CR/PR/SD/PD; the other groups retain cancerbot's seven outcomes.
CLL/MCL/DLBCL use the common supportive list because the reference has no
additional disease-specific supportive list for those groups.
"""
from django.db import migrations

DISEASES = {'C3242': 'Multiple Myeloma', 'C3209': 'Follicular Lymphoma',
            'C9335': 'Breast Cancer', 'C2987': 'Chronic Lymphocytic Leukemia',
            'MCL': 'Mantle Cell Lymphoma', 'DLBCL': 'Diffuse Large B-Cell Lymphoma'}
OUTCOMES = [
    ('CR', 'Complete Response', 'Complete Response (CR)'),
    ('sCR', 'Stringent Complete Response', 'Stringent Complete Response (sCR)'),
    ('VGPR', 'Very Good Partial Response', 'Very Good Partial Response (VGPR)'),
    ('PR', 'Partial Response', 'Partial Response (PR)'),
    ('MRD', 'Minimal Residual Disease Negativity', 'Minimal Residual Disease (MRD) Negativity'),
    ('SD', 'Stable Disease', 'Stable Disease (SD)'),
    ('PD', 'Progressive Disease', 'Progressive Disease (PD)'),
]
SUPPORTIVE = {'abemaciclib': {'diseases': ['C9335'], 'title': 'Abemaciclib (Verzenio)'},
 'anastrozole_maintenance': {'diseases': ['C9335'], 'title': 'Anastrozole Maintenance'},
 'antiviral': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'], 'title': 'antiviral'},
 'aspirin_gt_81mg_daily': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                           'title': 'Aspirin > 81mg daily'},
 'aspirin_lt_81mg_daily': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                           'title': 'Aspirin =< 81mg daily'},
 'bortezomib_maintenance': {'diseases': ['C3242'], 'title': 'Bortezomib (maintenance)'},
 'bupropion': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
               'title': 'Bupropion (Wellbutrin) - antidepressant'},
 'carbamazepine': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                   'title': 'Carbamazepine (Tegretol) - anticonvulsant'},
 'chronic_opioid_therapy': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                            'title': 'Chronic opioid therapy'},
 'ciprofloxacin': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                   'title': 'Ciprofloxacin (Cipro) - antibiotic'},
 'clarithromycin': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                    'title': 'Clarithromycin (Biaxin) - antibiotic'},
 'contraceptives': {'diseases': ['C9335'], 'title': 'contraceptives (Estrogen-containing medication)'},
 'denosumab': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
               'title': 'Denosumab (Xgeva)'},
 'erythromycin': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                  'title': 'Erythromycin - antibiotic'},
 'esa': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
         'title': 'Erythropoiesis-Stimulating Agent (ESA)'},
 'exemestane_maintenance': {'diseases': ['C9335'], 'title': 'Exemestane Maintenance'},
 'fluconazole': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                 'title': 'Fluconazole (Diflucan) - antifungal'},
 'fluoxetine': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                'title': 'Fluoxetine (Prozac) - antidepressant'},
 'fluvoxamine': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                 'title': 'Fluvoxamine (Luvox) - antidepressant'},
 'g_csf': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
           'title': 'Granulocyte-Colony Stimulating factor (G-CSFs)'},
 'gm_csf': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
            'title': 'Granulocyte–Macrophage Colony-Stimulating Factor (GM-CSF)'},
 'grapefruit_juice': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                      'title': 'Grapefruit juice'},
 'heparin_anticoagulant': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                           'title': 'heparin - anticoagulant'},
 'her2_targeted_therapies': {'diseases': ['C9335'], 'title': 'HER2-targeted therapies'},
 'herbal_supplements': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                        'title': 'Herbal supplements (e.g., echinacea, ginseng, ginkgo biloba, high-dose '
                                 'turmeric)'},
 'hiv_antiretroviral_therapy': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                                'title': 'HIV antiretroviral therapy'},
 'hormonal_therapy': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                      'title': 'Hormonal therapy'},
 'hrt': {'diseases': ['C9335'], 'title': 'HRT (Estrogen-containing medication)'},
 'ibritumomab_tiuxetan_radioimmunotherapy': {'diseases': ['C3209'],
                                             'title': 'Ibritumomab tiuxetan radioimmunotherapy'},
 'immunoglobulin_replacement_therapy': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                                        'title': 'Immunoglobulin replacement therapy'},
 'immunosuppressant': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                       'title': 'Immunosuppressant'},
 'inhaled_corticosteroids': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                             'title': 'Inhaled corticosteroids'},
 'intranasal_corticosteroids': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                                'title': 'Intranasal corticosteroid'},
 'itraconazole': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                  'title': 'Itraconazole (Sporanox) - antifungal'},
 'ivig': {'diseases': ['C3242'], 'title': 'IVIG (intravenous immunoglobulin)'},
 'ixazomib_maintenance': {'diseases': ['C3242'], 'title': 'Ixazomib (maintenance)'},
 'ketoconazole': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                  'title': 'Ketoconazole - antifungal'},
 'lenalidomide_maintenance': {'diseases': ['C3242', 'C3209'], 'title': 'Lenalidomide maintenance'},
 'letrozole_maintenance': {'diseases': ['C9335'], 'title': 'Letrozole Maintenance'},
 'lhrh_gnrh_agonists': {'diseases': ['C9335'],
                        'title': 'LHRH/GnRH agonists (e.g., goserelin, leuprolide, triptorelin, buserelin)'},
 'local_palliative_radiotherapy': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                                   'title': 'Local palliative radiotherapy'},
 'mineralocorticoids': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                        'title': 'mineralocorticoids (e.g., fludrocortisone)'},
 'palbociclib': {'diseases': ['C9335'], 'title': 'Palbociclib (Ibrance)'},
 'pamidronate': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'], 'title': 'Pamidronate'},
 'paroxetine': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                'title': 'Paroxetine (Paxil) - antidepressant'},
 'phenobarbital': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                   'title': 'Phenobarbital - anticonvulsant'},
 'phenytoin': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
               'title': 'Phenytoin (Dilantin) - anticonvulsant'},
 'plasmapheresis': {'diseases': ['C3242'], 'title': 'Plasmapheresis'},
 'posaconazole': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                  'title': 'Posaconazole (Noxafil) - antifungal'},
 'radiotherapy': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'], 'title': 'Radiotherapy'},
 'ribociclib': {'diseases': ['C9335'], 'title': 'Ribociclib (Kisqali)'},
 'rifabutin': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
               'title': 'Rifabutin (Mycobutin) - antibiotic'},
 'rifampin': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
              'title': 'Rifampin (Rifadin, Rimactane) - antibiotic'},
 'rituximab_maintenance_therapy': {'diseases': ['C3209'], 'title': 'Rituximab maintenance therapy'},
 'sertraline': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                'title': 'Sertraline (Zoloft) - antidepressant'},
 'st_john_s_wort': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                    'title': "St. John's Wort"},
 'systemic_corticosteroids_gt_10_mg_day': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                                           'title': 'systemic corticosteroids (e.g., prednisone) > 10 '
                                                    'mg/day'},
 'systemic_corticosteroids_gt_20_mg_day': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                                           'title': 'systemic corticosteroids (e.g., prednisone) > 20 '
                                                    'mg/day'},
 'systemic_corticosteroids_gt_5_mg_day': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                                          'title': 'systemic corticosteroids (e.g., prednisone) > 5 mg/day'},
 'systemic_corticosteroids_lt_5_mg_day': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                                          'title': 'systemic corticosteroids (e.g., prednisone) =< 5 mg/day'},
 'tamoxifen_maintenance': {'diseases': ['C9335'], 'title': 'Tamoxifen Maintenance'},
 'topical_corticosteroids': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                             'title': 'Topical corticosteroids'},
 'topiramate': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                'title': 'Topiramate (Topamax) - anticonvulsant'},
 'voriconazole': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                  'title': 'Voriconazole (Vfend) - antifungal'},
 'warfarin_anticoagulant': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                            'title': 'warfarin - anticoagulant'},
 'zoledronic_acid': {'diseases': ['C3242', 'C3209', 'C9335', 'C2987', 'MCL', 'DLBCL'],
                     'title': 'Zoledronic Acid'}}


def seed(apps, schema_editor):
    Disease = apps.get_model('omop_core', 'Disease')
    Outcome = apps.get_model('omop_core', 'TherapyOutcome')
    Regimen = apps.get_model('omop_core', 'TherapyRegimen')
    Round = apps.get_model('omop_core', 'TherapyRound')
    Link = apps.get_model('omop_core', 'DiseaseTherapyRegimen')
    diseases = {}
    for code, title in DISEASES.items():
        existing = Disease.objects.filter(code=code).first() or Disease.objects.filter(title__iexact=title).first()
        diseases[code] = existing or Disease.objects.create(code=code, title=title)
    for order, (code, value, title) in enumerate(OUTCOMES):
        outcome, _ = Outcome.objects.get_or_create(code=code, defaults={
            'value': value, 'title': title, 'sort_order': order,
        })
        outcome.diseases.add(*[d for key, d in diseases.items()
                              if key != 'C9335' or code in {'CR', 'PR', 'SD', 'PD'}])
    therapy_round, _ = Round.objects.get_or_create(code='supportive_therapy', defaults={'title': 'Supportive Therapy'})
    for code, item in SUPPORTIVE.items():
        regimen = Regimen.objects.filter(code=code).first() or Regimen.objects.filter(title=item['title']).first()
        if regimen is None:
            regimen = Regimen.objects.create(code=code, title=item['title'], source_name='cancerbot supportive therapy catalog')
        for disease in item['diseases']:
            Link.objects.get_or_create(disease=diseases[disease], round=therapy_round, regimen=regimen)


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0220_treatment_editors')]
    # Catalog entries may be referenced by patient courses; retain on rollback.
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
