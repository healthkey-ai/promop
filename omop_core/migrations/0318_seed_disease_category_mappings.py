"""Seed DiseaseCategoryMapping with known disease names from _DISEASE_ALIASES
and common HealthTree GCP disease strings."""

from django.db import migrations


# (disease_name, category)
_SEED = [
    # MM — Multiple Myeloma
    ('Multiple Myeloma', 'MM'),
    ('Multiple myeloma', 'MM'),
    ('Myeloma', 'MM'),
    ('Myeloma (disorder)', 'MM'),
    ('Multiple myeloma (disorder)', 'MM'),
    ('Smoldering myeloma', 'MM'),
    ('Multiple myeloma in remission', 'MM'),
    ('Relapse multiple myeloma', 'MM'),
    ('Myeloma-associated amyloidosis', 'MM'),
    ('Solitary osseous myeloma', 'MM'),
    ('Hypogammaglobulinemia due to multiple myeloma', 'MM'),
    ('IgG myeloma', 'MM'),
    ('Myeloma kidney', 'MM'),
    ('Kappa light chain myeloma', 'MM'),
    ('Lambda light chain myeloma', 'MM'),
    ('Asymptomatic multiple myeloma', 'MM'),
    ('Multiple myeloma not having achieved remission', 'MM'),
    ('Non-secretory myeloma', 'MM'),
    ('Immunoglobulin G deficiency due to multiple myeloma', 'MM'),
    ('Light chain myeloma', 'MM'),
    ('Light chain nephropathy due to multiple myeloma', 'MM'),
    ('IgA myeloma', 'MM'),
    ('Plasma cell leukemia', 'MM'),
    ('Plasma cell neoplasm', 'MM'),
    ('Plasma cell myeloma/plasmacytoma', 'MM'),
    ('Plasma cell myeloma', 'MM'),
    ('Plasma cell leukemia in remission', 'MM'),
    ('Osteoporosis co-occurrent and due to multiple myeloma', 'MM'),
    ('Neuropathy due to multiple myeloma', 'MM'),
    ('Multiple myeloma in relapse', 'MM'),
    ('History of multiple myeloma', 'MM'),
    ('Family history of multiple myeloma', 'MM'),
    ('Hx of multiple myeloma', 'MM'),
    ('MULTIPLE MYELOMA IN REMISSION', 'MM'),
    ('Immunoglobulin D myeloma', 'MM'),
    ('Indolent multiple myeloma', 'MM'),
    ('Osteosclerotic myeloma', 'MM'),
    ('Immunoproliferative neoplasm', 'MM'),
    # FL — Follicular Lymphoma
    ('Follicular Lymphoma', 'FL'),
    ('Follicular lymphoma', 'FL'),
    ('Follicular lymphoma (disorder)', 'FL'),
    ('Follicular lymphoma grade 3a', 'FL'),
    ('Follicular lymphoma grade I, extranodal and solid organ sites', 'FL'),
    ('Follicular lymphoma grade IIIa, lymph nodes of head, face, and neck', 'FL'),
    ('Follicular lymphoma grade I, lymph nodes of head, face, and neck', 'FL'),
    ('Follicular lymphoma, unspecified, lymph nodes of inguinal region and lower limb', 'FL'),
    ("Follicular non-Hodgkin's lymphoma, mixed small cleaved cell and large cell (clinical)", 'FL'),
    ("Follicular non-Hodgkin's lymphoma, small cleaved cell (clinical)", 'FL'),
    ("Follicular non-Hodgkin's lymphoma diffuse follicle center sub-type grade 1", 'FL'),
    ("Follicular non-Hodgkin's lymphoma of lymph nodes of multiple sites", 'FL'),
    ('Pediatric follicular lymphoma', 'FL'),
    ('Follicular neoplasm of thyroid', 'FL'),
    # BC — Breast Cancer
    ('Breast Cancer', 'BC'),
    ('Breast cancer (disorder)', 'BC'),
    ('Malignant neoplasm of breast', 'BC'),
    ('Malignant neoplasm of breast (disorder)', 'BC'),
    ('Carcinoma of breast', 'BC'),
    ('Carcinoma of breast (disorder)', 'BC'),
    ('ER|ERBB2 breast cancer', 'BC'),
    ('ER|ERBB2 breast cancer (disorder)', 'BC'),
    ('Primary malignant neoplasm of breast', 'BC'),
    ('Malignant tumor of breast', 'BC'),
    ('Primary malignant neoplasm of upper inner quadrant of breast', 'BC'),
    ('Primary malignant neoplasm of upper outer quadrant of breast', 'BC'),
    ('Primary malignant neoplasm of lower inner quadrant of breast', 'BC'),
    ('Primary malignant neoplasm of female left breast', 'BC'),
    ('Malignant neoplasm of female breast', 'BC'),
    ('Malignant neoplasm of overlapping sites of breast', 'BC'),
    ('Malignant neoplasm of lower outer quadrant of breast', 'BC'),
    ('Malignant neoplasm of upper outer quadrant of breast', 'BC'),
    ('Ductal carcinoma in situ of right breast', 'BC'),
    ('Ductal carcinoma in situ of left breast', 'BC'),
    ('Ductal carcinoma in situ of breast', 'BC'),
    ('Lobular carcinoma in situ of left breast', 'BC'),
    ('Lobular carcinoma in situ of right breast', 'BC'),
    ('Lobular carcinoma in situ of breast', 'BC'),
    ('Infiltrating duct carcinoma of breast', 'BC'),
    ('Infiltrating duct carcinoma of right breast', 'BC'),
    ('Infiltrating lobular carcinoma of breast', 'BC'),
    ('Inflammatory carcinoma of breast', 'BC'),
    ('History of malignant neoplasm of breast', 'BC'),
    ('Family history of breast cancer', 'BC'),
    ('Family history of malignant neoplasm of breast', 'BC'),
    ('Family history of malignant neoplasm of breast in first degree relative', 'BC'),
    ('At high risk for malignant neoplasm of breast', 'BC'),
    ('At increased risk of malignant neoplasm of breast', 'BC'),
    ('History of ductal carcinoma in situ of breast', 'BC'),
    ('Estrogen receptor positive tumor', 'BC'),
    # CLL — Chronic Lymphocytic Leukemia
    ('Chronic Lymphocytic Leukemia', 'CLL'),
    ('Chronic lymphocytic leukemia', 'CLL'),
    ('Chronic lymphocytic leukemia (disorder)', 'CLL'),
    ('Chronic lymphoid leukemia, disease', 'CLL'),
    ('Chronic lymphoid leukemia in remission', 'CLL'),
    ('Chronic lymphocytic leukemia of B-cell type not having achieved remission', 'CLL'),
    ('B-cell chronic lymphocytic leukemia variant', 'CLL'),
    ('Monoclonal B-cell lymphocytosis chronic lymphocytic leukemia-type', 'CLL'),
    # MCL — Mantle Cell Lymphoma
    ('Mantle Cell Lymphoma', 'MCL'),
    ('Mantle cell lymphoma', 'MCL'),
    ('Mantle cell lymphoma (disorder)', 'MCL'),
    ('Mantle cell lymphoma of lymph nodes of multiple sites', 'MCL'),
    # Other
    ('Diffuse large B-cell lymphoma', 'Other'),
    ('Hodgkin lymphoma', 'Other'),
    ("Hodgkin's disease (clinical)", 'Other'),
    ("Hodgkin's disease, mixed cellularity of lymph nodes of multiple sites", 'Other'),
    ("Hodgkin's disease, nodular sclerosis (clinical)", 'Other'),
    ('Hodgkin lymphoma, nodular lymphocyte predominance (clinical)', 'Other'),
    ('Classical Hodgkin lymphoma', 'Other'),
    ('Waldenstrom macroglobulinemia', 'Other'),
    ('Marginal zone lymphoma', 'Other'),
    ('Burkitt lymphoma', 'Other'),
    ('T-cell lymphoma (clinical)', 'Other'),
    ('Acute myeloid leukemia, disease', 'Other'),
    ('Acute myeloid leukemia in remission', 'Other'),
    ('Chronic myeloid leukemia', 'Other'),
    ('Chronic myeloid leukemia in remission', 'Other'),
    ('Chronic myelomonocytic leukemia', 'Other'),
    ('Myelodysplastic syndrome', 'Other'),
    ('Amyloidosis', 'Other'),
    ('Polycythemia vera', 'Other'),
    ('Essential thrombocythemia', 'Other'),
    ('Primary myelofibrosis', 'Other'),
    ("Non-Hodgkin's lymphoma (clinical)", 'Other'),
    ("Non-Hodgkin's lymphoma of lymph nodes of multiple sites", 'Other'),
    ('Malignant lymphoma - small lymphocytic', 'Other'),
    ('Malignant lymphoma - lymphoplasmacytic', 'Other'),
    ('Malignant lymphoma', 'Other'),
    ('Myeloproliferative neoplasm', 'Other'),
    ('Acute lymphoid leukemia', 'Other'),
    ('Renal cell carcinoma', 'Other'),
    ('Angioimmunoblastic T-cell lymphoma', 'Other'),
    ('Anaplastic large cell lymphoma, ALK negative', 'Other'),
    ('Cutaneous peripheral T-cell lymphoma', 'Other'),
    ('Primary central nervous system lymphoma', 'Other'),
    ('Large granular lymphocytic leukemia', 'Other'),
    ('T-cell large granular lymphocytic leukemia', 'Other'),
    ('T-cell prolymphocytic leukemia', 'Other'),
    ('B-cell lymphoma (clinical)', 'Other'),
    ('Extranodal marginal zone B-cell lymphoma of mucosa-associated lymphoid tissue [MALT-lymphoma] not having achieved remission', 'Other'),
    ('Extranodal marginal zone B-cell lymphoma of mucosa-associated lymphoid tissue (MALT-lymphoma)', 'Other'),
    ('Blastic plasmacytoid dendritic cell neoplasm', 'Other'),
    ('Myelodysplastic neoplasm', 'Other'),
]


def seed_forward(apps, schema_editor):
    M = apps.get_model('omop_core', 'DiseaseCategoryMapping')
    for name, cat in _SEED:
        M.objects.get_or_create(disease_name=name, defaults={'category': cat})


def seed_reverse(apps, schema_editor):
    M = apps.get_model('omop_core', 'DiseaseCategoryMapping')
    names = [name for name, _ in _SEED]
    M.objects.filter(disease_name__in=names).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0317_diseasecategorymapping'),
    ]

    operations = [
        migrations.RunPython(seed_forward, seed_reverse),
    ]
