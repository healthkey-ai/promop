export const GENDER_OPTIONS = ['Male', 'Female', 'Other', 'Unknown'];
export const COUNTRY_OPTIONS = ['United States', 'Canada', 'United Kingdom', 'Germany', 'France', 'Spain', 'Italy', 'Other'];
export const US_STATES = [
  'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID', 'IL', 'IN', 'IA', 'KS',
  'KY', 'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY',
  'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV',
  'WI', 'WY', 'DC', 'PR', 'GU', 'VI',
];
export const RACE_OPTIONS = [
  'White',
  'Black or African American',
  'Asian',
  'American Indian or Alaska Native',
  'Native Hawaiian or Pacific Islander',
  'Other Race',
  'Unknown',
];
export const ETHNICITY_OPTIONS = ['Hispanic or Latino', 'Not Hispanic or Latino', 'Unknown'];
export const DISEASE_OPTIONS = ['Breast Cancer', 'Follicular Lymphoma', 'Multiple Myeloma', 'Chronic Lymphocytic Leukemia (CLL)', 'Lung Cancer', 'Colon Cancer', 'Other'];
export const STAGE_OPTIONS = ['0', 'I', 'IA', 'IB', 'II', 'IIA', 'IIB', 'III', 'IIIA', 'IIIB', 'IIIC', 'IV', 'Unknown'];
export const HISTOLOGIC_TYPE_OPTIONS = [
  'Unknown',
  'Infiltrating ductal carcinoma (IDC)',
  'Ductal carcinoma in situ (DCIS)',
  'Infiltrating lobular carcinoma (ILC)',
  'Lobular carcinoma in situ (LCIS)',
  'Mixed ductal and lobular carcinoma',
  'Mucinous (colloid) carcinoma',
  'Tubular carcinoma',
  'Medullary carcinoma',
  'Papillary carcinoma',
  'Metaplastic carcinoma',
  'Paget disease of the nipple',
  'Inflammatory carcinoma',
];
export const ECOG_OPTIONS = ['0', '1', '2', '3', '4', '5'];
export const KARNOFSKY_OPTIONS = ['100', '90', '80', '70', '60', '50', '40', '30', '20', '10', '0'];
export const YES_NO_OPTIONS = ['Yes', 'No', 'Unknown'];
export const POSITIVE_NEGATIVE_OPTIONS = ['Positive', 'Negative', 'Unknown'];

export const NODES_STAGE_OPTIONS = [
  'NX: Nodes cannot be assessed (e.g., previously removed)',
  'N0: No lymph node involvement',
  'N1: 1–3 axillary lymph nodes or small internal mammary nodes',
  'N1mi: Micrometastasis (0.2–2 mm)',
  'N1a: 1–3 axillary nodes (>2 mm)',
  'N1b: Cancer cells in internal mammary sentinel nodes',
  'N1c: 1–3 axillary nodes + internal mammary sentinel nodes',
  'N2: 4–9 axillary nodes or internal mammary nodes without axillary nodes',
  'N2a: 4–9 axillary nodes (>2 mm)',
  'N2b: Internal mammary nodes only (no axillary)',
  'N3: 10+ axillary, infraclavicular, or supraclavicular nodes; or both axillary + internal mammary',
  'N3a: ≥10 axillary nodes (≥2 mm) or infraclavicular',
  'N3b: 4–9 Axillary + mammary nodes',
  'N3c: Supraclavicular nodes',
];

export const TUMOR_STAGE_OPTIONS = [
  'Tx: Primary Tumor, cannot be assessed',
  'T0: No tumor evidence',
  'Tis: Non-invasive Carcinoma in situ (DCIS, LCIS, Paget\'s without tumor)',
  'T1: Invasive Tumor ≤ 2 cm',
  'T1mi: Invasive Tumor ≤ 0.1 cm',
  'T1a: 0.1 – 0.5 cm',
  'T1b: 0.5 – 1 cm',
  'T1c: 1 – 2 cm',
  'T2: Invasive Tumor > 2 – 5 cm',
  'T3: Invasive Tumor > 5 cm',
  'T4: Invades chest wall or skin, or inflammatory',
  'T4a: Invades chest wall',
  'T4b: Invades skin (may be swelling/ulcer)',
  'T4c: Invades both skin + chest wall',
  'T4d: Inflammatory carcinoma',
];

export const DISTANT_METASTASIS_STAGE_OPTIONS = [
  'M0: No distant metastasis',
  'M0(i+): No metastasis on scans, but cancer cells found in blood/bone marrow/distant nodes',
  'M1: Distant metastasis present',
];

export const STAGING_MODALITIES_OPTIONS = [
  'c → Clinical',
  'p → Pathological',
  'yp → Pathological after neoadjuvant therapy',
];

export const ER_OPTIONS = ['ER+', 'ER-', 'ER+ with low expression', 'ER+ with high expression', 'Unknown'];
export const PR_OPTIONS = ['PR+', 'PR-', 'PR+ with low expression', 'PR+ with high expression', 'Unknown'];
export const HER2_OPTIONS = ['HER2+', 'HER2-', 'HER2 low', 'Unknown'];
export const HR_OPTIONS = ['HR+', 'HR-', 'HR+ with low expression', 'HR+ with high expression', 'Unknown'];
export const HRD_OPTIONS = ['HRD+', 'HRD-', 'Unknown'];
export const MENOPAUSAL_OPTIONS = ['Pre-menopausal', 'Peri-menopausal', 'Post-menopausal', 'Unknown'];
export const FLIPI_RISK_OPTIONS = ['Low', 'Intermediate', 'High'];
export const GELF_OPTIONS = ['Met', 'Not Met', 'Unknown'];
export const FL_TUMOR_GRADE_OPTIONS = [
  'Grade 1 (0–5 centroblasts/HPF)',
  'Grade 2 (6–15 centroblasts/HPF)',
  'Grade 3a (>15 centroblasts/HPF, centrocytes present)',
  'Grade 3b (solid sheets of centroblasts)',
];
export const ISS_STAGE_OPTIONS = ['Stage I', 'Stage II', 'Stage III'];
export const MRD_STATUS_OPTIONS = [
  'MRD Negative (10⁻⁵)',
  'MRD Negative (10⁻⁶)',
  'Sustained MRD Negative',
  'MRD Positive',
  'Not Assessed',
  'Unknown',
];
export const MM_PROGRESSION_OPTIONS = ['Stable', 'Active', 'Smoldering', 'Progressive', 'Relapsed', 'Refractory'];
export const STEM_CELL_TRANSPLANT_OPTIONS = [
  'prior SCT', 'prior autologous SCT', 'prior allogeneic SCT',
  'recent SCT', 'recent autologous SCT', 'recent allogeneic SCT',
  'relapsed post-SCT', 'relapsed post-autologous SCT', 'relapsed post-allogeneic SCT',
  'completed tandem SCT', 'never received SCT', 'pre-autologous SCT', 'pre-allogeneic SCT',
];
export const CYTOGENETIC_RISK_OPTIONS = ['Standard Risk', 'High Risk', 'Very High Risk'];
export const THERAPY_OUTCOME_OPTIONS = [
  'Complete Response (CR)', 'Partial Response (PR)', 'Stable Disease (SD)', 'Progressive Disease (PD)',
];
export const SMOKING_STATUS_OPTIONS = ['Never Smoker', 'Former Smoker', 'Current Smoker', 'Unknown'];
export const ALCOHOL_USE_OPTIONS = ['None', 'Occasional', 'Moderate', 'Heavy', 'Unknown'];
export const EXERCISE_FREQUENCY_OPTIONS = ['None', 'Rarely', '1-2 times/week', '3-4 times/week', '5+ times/week', 'Daily', 'Unknown'];
export const REFRACTORY_STATUS_OPTIONS = ['Unknown', 'Not Refractory', 'Primary Refractory', 'Secondary Refractory', 'Multi-Refractory'];
export const THERAPY_INTENT_OPTIONS = ['Adjuvant', 'Neoadjuvant', 'Metastatic'];
export const DISCONTINUATION_REASON_OPTIONS = ['Progression', 'Toxicity', 'Completion'];
export const DIET_TYPE_OPTIONS = ['Regular', 'Vegetarian', 'Vegan', 'Mediterranean', 'Low-carb', 'Ketogenic', 'Other'];
export const SLEEP_QUALITY_OPTIONS = ['Excellent', 'Good', 'Fair', 'Poor', 'Very Poor'];
export const STRESS_LEVEL_OPTIONS = ['None', 'Low', 'Moderate', 'High', 'Very High'];
export const SOCIAL_SUPPORT_OPTIONS = ['Excellent', 'Good', 'Fair', 'Poor', 'None'];
export const EMPLOYMENT_STATUS_OPTIONS = ['Employed Full-time', 'Employed Part-time', 'Self-employed', 'Unemployed', 'Retired', 'Disabled', 'Student', 'Homemaker'];
export const EDUCATION_LEVEL_OPTIONS = ['Less than High School', 'High School Graduate', 'Some College', 'Associate Degree', 'Bachelor Degree', 'Master Degree', 'Doctoral Degree', 'Professional Degree'];
export const MARITAL_STATUS_OPTIONS = ['Single', 'Married', 'Divorced', 'Widowed', 'Separated', 'Domestic Partnership'];
export const INSURANCE_TYPE_OPTIONS = ['Private Insurance', 'Medicare', 'Medicaid', 'Veterans Affairs', 'Other Government', 'Self-pay', 'None'];

export const GENE_OPTIONS = ['BRCA1', 'BRCA2', 'TP53', 'PIK3CA', 'ESR1'];
export const MUTATION_OPTIONS: { [key: string]: string[] } = {
  'BRCA1': ['c.68_69delAG', 'c.5266dupC', 'c.181T>G', 'c.3756_3759del', '185delAG'],
  'BRCA2': ['c.5946delT', 'c.9097dupA', 'c.7617+1G>A', '6174delT', 'c.8537_8538del'],
  'TP53': ['R175H', 'R248Q', 'R273H', 'R248W', 'R282W'],
  'PIK3CA': ['E542K', 'E545K', 'H1047R', 'H1047L', 'E726K'],
  'ESR1': ['D538G', 'Y537S', 'Y537C', 'Y537N', 'E380Q'],
};
export const ORIGIN_OPTIONS = ['Germline', 'Somatic', 'Unknown'];
export const INTERPRETATION_OPTIONS = ['Pathogenic', 'Likely pathogenic', 'VUS', 'Likely benign', 'Benign'];

export const BINET_STAGE_OPTIONS = [
  'Binet Stage A (<3 lymphoid areas involved)',
  'Binet Stage B (≥3 lymphoid areas involved)',
  'Binet Stage C (Anemia or Thrombocytopenia)',
];

export const PROTEIN_EXPRESSION_OPTIONS = [
  'CD38 +ve', 'CD38 -ve', 'ZAP-70 +ve', 'ZAP-70 -ve',
  'CD49d +ve', 'CD49d -ve', 'CD19 +ve', 'CD19 -ve',
  'CD5 +ve', 'CD5 -ve', 'CD20 +ve', 'CD20 -ve',
  'CD23 +ve', 'CD23 -ve',
  'Kappa (κ) light chain +ve', 'Kappa (κ) light chain -ve',
  'Lambda (λ) light chain +ve', 'Lambda (λ) light chain -ve',
];

export const RICHTER_TRANSFORMATION_OPTIONS = [
  'Richter Transformation to DLBCL',
  'Richter Transformation to Hodgkin Lymphoma',
  'Richter Transformation to Non-Hodgkin Lymphoma',
  'Clonally Related RT',
  'Clonally Unrelated RT',
];

export const TUMOR_BURDEN_OPTIONS = ['Low', 'Intermediate', 'High'];
export const DISEASE_ACTIVITY_OPTIONS = ['Active', 'Inactive', 'Remission', 'Relapsed', 'Refractory'];

export const CLL_FIRST_LINE = [
  'Watch and Wait', 'FCR (Fludarabine/Cyclophosphamide/Rituximab)',
  'BR (Bendamustine/Rituximab)', 'Ibrutinib', 'Acalabrutinib',
  'Venetoclax + Obinutuzumab', 'Chlorambucil + Obinutuzumab',
  'Chlorambucil + Rituximab', 'Other',
];
export const CLL_SECOND_LINE = [
  'Ibrutinib', 'Acalabrutinib', 'Zanubrutinib',
  'Venetoclax + Rituximab', 'Idelalisib + Rituximab', 'Duvelisib',
  'BR (Bendamustine/Rituximab)', 'Other',
];
export const CLL_LATER_LINE = [
  'Pirtobrutinib', 'Venetoclax',
  'Lisocabtagene maraleucel (CAR-T)', 'Allogeneic SCT', 'Clinical Trial', 'Other',
];

export const BREAST_CANCER_FIRST_LINE = [
  'Watchful Waiting (Active Surveillance)', 'Lumpectomy (Lumpectomy)', 'Mastectomy (Mastectomy)',
  'Aromatase Inhibitor (Aromatase Inhibitor)', 'Trastuzumab (Herceptin) (Trastuzumab)',
  'Pertuzumab (Perjeta) (Pertuzumab)', 'Genomic Testing (Genomic Testing)',
  'Tamoxifen (Tamoxifen)', 'Letrozole (Letrozole)', 'Anastrozole (Arimidex) (Anastrozole)',
  'Exemestane (Exemestane)',
  'Lumpectomy + Radiation (Lumpectomy, Ipsilateral Breast Radiation, Adjuvant Radiotherapy)',
  'Mastectomy + Radiation (Mastectomy, Ipsilateral Breast Radiation, Adjuvant Radiotherapy)',
  'Axillary LND + Lumpectomy + Radiation (Lumpectomy, Axillary Lymph Node Dissection (ALND), Ipsilateral Breast Radiation, Adjuvant Radiotherapy)',
  'Axillary LND + Mastectomy (Mastectomy, Axillary Lymph Node Dissection (ALND))',
  'Axillary LND + Mastectomy + Radiation (Mastectomy, Axillary Lymph Node Dissection (ALND), Ipsilateral Breast Radiation, Adjuvant Radiotherapy)',
];

export const BREAST_CANCER_SECOND_LINE = [
  'Fulvestrant (Faslodex) (Fulvestrant)', 'Exemestane + Everolimus (Exemestane, Everolimus)',
  'Atezolizumab (Atezolizumab)', 'Sacituzumab Govitecan (Sacituzumab Govitecan)',
  'Platinum-Based Chemotherapy (Platinum-Based Chemotherapy)', 'PARP Inhibitor (PARP Inhibitor)',
  'Other Chemotherapy (Other Chemotherapy)', 'Capivasertib (Capivasertib)',
  'Axillary LND + Lumpectomy + Radiation (Lumpectomy, Axillary Lymph Node Dissection (ALND), Ipsilateral Breast Radiation, Adjuvant Radiotherapy)',
  'Axillary LND + Mastectomy (Mastectomy, Axillary Lymph Node Dissection (ALND))',
  'Axillary LND + Mastectomy + Radiation (Mastectomy, Axillary Lymph Node Dissection (ALND), Ipsilateral Breast Radiation, Adjuvant Radiotherapy)',
];

export const BREAST_CANCER_LATER_LINE = [
  'Fulvestrant (Faslodex) (Fulvestrant)', 'Exemestane + Everolimus (Exemestane, Everolimus)',
  'Sacituzumab Govitecan (Sacituzumab Govitecan)',
  'Alpelisib + Fulvestrant (Alpelisib, Fulvestrant)',
  'Capivasertib + Fulvestrant (Fulvestrant, Capivasertib)', 'Elacestrant (Elacestrant)',
  'Tamoxifen (Tamoxifen)', 'Megestrol acetate (Megestrol acetate)',
  'Capecitabine (Capecitabine)', 'Eribulin (Eribulin)', 'Vinorelbine (Vinorelbine)',
  'Gemcitabine (Gemcitabine)', 'Paclitaxel (Paclitaxel)', 'Docetaxel (Docetaxel)',
  'Trastuzumab deruxtecan (T-DXd / Enhertu) (Trastuzumab Deruxtecan)',
  'Tucatinib + Trastuzumab + Capecitabine (Trastuzumab, Capecitabine, Tucatinib)',
  'Lapatinib (Tykerb) (Lapatinib)', 'Neratinib (Nerlynx) (Neratinib)',
  'Trastuzumab emtansine (T-DM1 / Kadcyla) (Trastuzumab Emtansine)',
  'Atezolizumab + Nab-Paclitaxel (Atezolizumab, Nab-Paclitaxel)',
  'Pembrolizumab + Chemotherapy (Pembrolizumab)',
  'Olaparib (Olaparib)', 'Talazoparib (Talazoparib)',
  'Carboplatin (Carboplatin)', 'Cisplatin (Cisplatin)',
  'Alpelisib (Piqray) Monotherapy (Alpelisib)', 'Capivasertib (Capivasertib)',
  'Larotrectinib (Larotrectinib)', 'Entrectinib (Entrectinib)',
  'Liposomal Doxorubicin (Doxorubicin)',
  'Axillary LND + Radiation (Axillary Lymph Node Dissection (ALND), Ipsilateral Breast Radiation)',
  'Axillary LND + Mastectomy (Mastectomy, Axillary Lymph Node Dissection (ALND))',
  'Axillary LND + Mastectomy + Radiation (Mastectomy, Axillary Lymph Node Dissection (ALND), Ipsilateral Breast Radiation, Adjuvant Radiotherapy)',
];

export const LYMPHOMA_FIRST_LINE = [
  'R-CHOP (Rituximab/Cyclophosphamide/Doxorubicin/Vincristine/Prednisone)',
  'BR (Bendamustine/Rituximab)',
  'R-CVP (Rituximab/Cyclophosphamide/Vincristine/Prednisone)',
  'Rituximab Monotherapy', 'Watch and Wait', 'Other',
];
export const LYMPHOMA_SECOND_LINE = [
  'R-ICE (Rituximab/Ifosfamide/Carboplatin/Etoposide)',
  'R-DHAP (Rituximab/Dexamethasone/Cytarabine/Cisplatin)',
  'BR (Bendamustine/Rituximab)', 'Lenalidomide/Rituximab',
  'Obinutuzumab-based therapy', 'Other',
];
export const LYMPHOMA_LATER_LINE = [
  'Tazemetostat', 'Lenalidomide/Rituximab',
  'PI3K Inhibitor (Copanlisib/Duvelisib/Idelalisib)',
  'Obinutuzumab Monotherapy', 'Clinical Trial', 'Other',
];

export const MYELOMA_FIRST_LINE = [
  'VRd (Bortezomib/Lenalidomide/Dexamethasone)',
  'CyBorD (Cyclophosphamide/Bortezomib/Dexamethasone)',
  'DRd (Daratumumab/Lenalidomide/Dexamethasone)',
  'RVd (Lenalidomide/Bortezomib/Dexamethasone)',
  'KRd (Carfilzomib/Lenalidomide/Dexamethasone)', 'Other',
];
export const MYELOMA_SECOND_LINE = [
  'DVd (Daratumumab/Bortezomib/Dexamethasone)',
  'KRd (Carfilzomib/Lenalidomide/Dexamethasone)',
  'DRd (Daratumumab/Lenalidomide/Dexamethasone)',
  'Elotuzumab/Lenalidomide/Dexamethasone',
  'Ixazomib/Lenalidomide/Dexamethasone',
  'Carfilzomib/Dexamethasone', 'Other',
];
export const MYELOMA_LATER_LINE = [
  'Isatuximab/Pomalidomide/Dexamethasone',
  'Daratumumab/Pomalidomide/Dexamethasone',
  'Selinexor/Bortezomib/Dexamethasone', 'Belantamab mafodotin',
  'CAR-T (Idecabtagene vicleucel/Ciltacabtagene autoleucel)',
  'Clinical Trial', 'Other',
];

export const SUPPORTIVE_THERAPIES_OPTIONS = [
  'Adjuvant Radiotherapy (Adjuvant Radiotherapy)',
  'systemic corticosteroids (e.g., prednisone) =< 5 mg/day (Systemic Corticosteroid =< 5 mg/day)',
  'systemic corticosteroids (e.g., prednisone) > 5 mg/day (Systemic Corticosteroid > 5 mg/day)',
  'systemic corticosteroids (e.g., prednisone) > 10 mg/day (Systemic Corticosteroid > 10 mg/day)',
  'systemic corticosteroids (e.g., prednisone) > 20 mg/day (Systemic Corticosteroid > 20 mg/day)',
  'mineralocorticoids (e.g., fludrocortisone) (Mineralocorticoid)',
  'Inhaled corticosteroids (Inhaled corticosteroid)',
  'Topical corticosteroids (Topical corticosteroid)',
  'Intranasal corticosteroids (Intranasal corticosteroid)',
  'Immunosuppressant (Immunosuppressant)', 'antiviral (Antiviral)',
  'Warfarin - anticoagulant (Warfarin)', 'heparin - anticoagulant (Heparin)',
  'Aspirin =< 81mg daily (Aspirin =< 81mg daily)', 'Aspirin > 81mg daily (Aspirin > 81mg daily)',
  'Chronic Opioid Therapy (Analgesic Opioid Agent)',
  'HIV antiretroviral therapy (HIV Antiretroviral)',
  'Herbal supplements (e.g., echinacea, ginseng, ginkgo biloba, high-dose turmeric) (Herbal Supplement)',
  'Local palliative radiotherapy (Palliative Radiotherapy)',
  'Clarithromycin (Biaxin) - antibiotic (Clarithromycin)',
  'Erythromycin - antibiotic (Erythromycin)',
  'Ciprofloxacin (Cipro) - antibiotic (Ciprofloxacin)',
  'Rifampin (Rifadin, Rimactane) - antibiotic (Rifampin)',
  'Rifabutin (Mycobutin) - antibiotic (Rifabutin)',
  'Itraconazole (Sporanox) - antifungal (Itraconazole)',
  'Ketoconazole - antifungal (Ketoconazole)',
  'Fluconazole (Diflucan) - antifungal (Fluconazole)',
  'Voriconazole (Vfend) - antifungal (Voriconazole)',
  'Posaconazole (Noxafil) - antifungal (Posaconazole)',
  'Fluoxetine (Prozac) - antidepressant (Fluoxetine)',
  'Paroxetine (Paxil) - antidepressant (Paroxetine)',
  'Fluvoxamine (Luvox) - antidepressant (Fluvoxamine)',
  'Sertraline (Zoloft) - antidepressant (Sertraline)',
  'Bupropion (Wellbutrin) - antidepressant (Bupropion)',
  'Carbamazepine (Tegretol) - anticonvulsant (Carbamazepine)',
  'Phenytoin (Dilantin) - anticonvulsant (Phenytoin)',
  'Phenobarbital - anticonvulsant (Phenobarbital)',
  'Topiramate (Topamax) - anticonvulsant (Topiramate)',
  "St. John's Wort (St. John's Wort)", 'Grapefruit juice (Grapefruit juice)',
  'Pamidronate (Pamidronate)', 'Zoledronic Acid (Zoledronic Acid)',
  'Denosumab (Xgeva) (Denosumab)',
  'Granulocyte-Colony Stimulating factor (G-CSF) (Growth Factor)',
  'Granulocyte-Macrophage Colony-Stimulating Factor (GM-CSF) (Growth Factor)',
  'Erythropoiesis-Stimulating Agent (ESA) (Growth Factor)',
  'LHRH/GnRH agonists (e.g., goserelin, leuprolide, triptorelin, buserelin) (LHRH/GnRH agonist)',
  'Palbociclib (Ibrance) (Palbociclib)', 'Ribociclib (Kisqali) (Ribociclib)',
  'Abemaciclib (Verzenio) (Abemaciclib)',
  'HRT (Estrogen-containing medication) (Estrogen)',
  'Contraceptives (Estrogen-containing medication) (Estrogen)',
  'Tamoxifen Maintenance (Tamoxifen)', 'Anastrozole (Arimidex) Maintenance (Anastrozole)',
  'Letrozole Maintenance (Letrozole)', 'Exemestane Maintenance (Exemestane)',
  'IVIG (intravenous immunoglobulin) (IVIG (intravenous immunoglobulin))',
  'Adjuvant Chemotherapy (Adjuvant Chemotherapy)',
  'Neoadjuvant Chemotherapy (Neoadjuvant Chemotherapy)',
  'Adjuvant Hormonal Therapy (Adjuvant Hormonal Therapy)',
  'Neoadjuvant Hormonal Therapy (Neoadjuvant Hormonal Therapy)',
  'Sentinel Lymph Node Biopsy (SLNB) (Sentinel Lymph Node Biopsy (SLNB))',
  'Trastuzumab (Herceptin) Maintenance (Trastuzumab)',
  'Pertuzumab (Perjeta) Maintenance (Pertuzumab)',
  'Trastuzumab emtansine (T-DM1 / Kadcyla) Maintenance (Trastuzumab Emtansine)',
  'Trastuzumab deruxtecan (T-DXd / Enhertu) Maintenance (Trastuzumab Deruxtecan)',
  'Lapatinib (Tykerb) Maintenance (Lapatinib)', 'Neratinib (Nerlynx) Maintenance (Neratinib)',
  'Advil / Motrin IB / Ibuprofen (Ibuprofen)', 'Aleve (Naproxen Sodium)',
  'Voltaren (Diclofenac Sodium)', 'Cataflam (Diclofenac Potassium)',
];

export const PLANNED_THERAPIES = [
  'No planned therapy', 'Surgery', 'breast-conserving surgery (lumpectomy)', 'mastectomy',
  'axillary lymph node dissection', 'Neoadjuvant Chemotherapy',
  'Neoadjuvant Anthracycline-based Chemotherapy', 'Neoadjuvant Taxane-based Chemotherapy',
  'Neoadjuvant Platinum-based Chemotherapy', 'Neoadjuvant Endocrine/Hormonal Therapy',
  'Neoadjuvant Aromatase inhibitors (e.g., letrozole, anastrozole)', 'Neoadjuvant Tamoxifen',
  'Neoadjuvant Ovarian suppression (e.g., goserelin)', 'Neoadjuvant HER2-Targeted Therapy',
  'Neoadjuvant Trastuzumab (Herceptin)', 'Neoadjuvant Pertuzumab (Perjeta)',
  'Neoadjuvant Trastuzumab emtansine (T-DM1)', 'Neoadjuvant Immunotherapy',
  'Neoadjuvant Checkpoint inhibitors (e.g., pembrolizumab, atezolizumab)',
  'Neoadjuvant Radiotherapy', 'Neoadjuvant External beam radiation therapy',
  'Neoadjuvant Targeted intraoperative radiotherapy', 'Adjuvant Chemotherapy',
  'Adjuvant Endocrine/Hormonal Therapy', 'Adjuvant HER2-Targeted Therapy',
  'Adjuvant trastuzumab', 'Adjuvant Radiotherapy', 'Chemotherapy',
  'Anthracycline-based Chemotherapy', 'Taxane-based Chemotherapy',
  'Platinum-based Chemotherapy', 'Endocrine/Hormonal Therapy',
  'Aromatase inhibitors (e.g., letrozole, anastrozole)', 'Tamoxifen',
  'Ovarian suppression (e.g., goserelin)', 'Trastuzumab (Herceptin)',
  'Pertuzumab (Perjeta)', 'Trastuzumab emtansine (T-DM1)', 'Immunotherapy',
  'Checkpoint inhibitors (experimental)', 'Radiotherapy',
  'External beam radiation therapy', 'Targeted intraoperative radiotherapy',
  'Bone-Modifying Agents', 'Bisphosphonates (e.g., zoledronic acid)', 'Denosumab',
  'Targeted therapy', 'Sentinel Lymph Node Biopsy (SLNB)',
  'Anti-HER2 ADCs', 'Anti-HER2 Monoclonal Antibodies', 'HER2 Tyrosine Kinase Inhibitors',
  'R-CHOP (Rituximab/Cyclophosphamide/Doxorubicin/Vincristine/Prednisone)',
  'BR (Bendamustine/Rituximab)',
  'R-CVP (Rituximab/Cyclophosphamide/Vincristine/Prednisone)',
  'R-ICE (Rituximab/Ifosfamide/Carboplatin/Etoposide)',
  'R-DHAP (Rituximab/Dexamethasone/Cytarabine/Cisplatin)',
  'Rituximab Monotherapy', 'Obinutuzumab-based therapy', 'Obinutuzumab Monotherapy',
  'Lenalidomide/Rituximab', 'Tazemetostat', 'PI3K Inhibitor (Copanlisib/Duvelisib/Idelalisib)',
  'VRd (Bortezomib/Lenalidomide/Dexamethasone)',
  'CyBorD (Cyclophosphamide/Bortezomib/Dexamethasone)',
  'DRd (Daratumumab/Lenalidomide/Dexamethasone)',
  'KRd (Carfilzomib/Lenalidomide/Dexamethasone)',
  'DVd (Daratumumab/Bortezomib/Dexamethasone)',
  'Elotuzumab/Lenalidomide/Dexamethasone', 'Ixazomib/Lenalidomide/Dexamethasone',
  'Carfilzomib/Dexamethasone', 'Isatuximab/Pomalidomide/Dexamethasone',
  'Daratumumab/Pomalidomide/Dexamethasone', 'Selinexor/Bortezomib/Dexamethasone',
  'Belantamab mafodotin', 'CAR-T Therapy',
  'Autologous Stem Cell Transplant', 'Allogeneic Stem Cell Transplant',
  'Radiation Therapy', 'Surgery', 'Clinical Trial', 'Watch and Wait', 'Other',
];
