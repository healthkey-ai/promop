import React, { useState, useEffect } from 'react';
import {
  Box,
  Paper,
  Typography,
  CircularProgress,
  Alert,
  Tabs,
  Tab,
  Grid,
  Button,
  Divider,
  TextField,
  Select,
  MenuItem,
  FormControl,
  InputLabel,
  Checkbox,
  ListItemText,
  Tooltip,
} from '@mui/material';
import { useParams, useNavigate } from 'react-router-dom';
import { ArrowLeft, Save } from 'lucide-react';
import api from '../../api/axios';
import { useVocabulary, VocabSource } from '../../hooks/useVocabulary';

interface TabPanelProps {
  children?: React.ReactNode;
  index: number;
  value: number;
}

function TabPanel(props: TabPanelProps) {
  const { children, value, index, ...other } = props;
  return (
    <div
      role="tabpanel"
      hidden={value !== index}
      id={`patient-tabpanel-${index}`}
      aria-labelledby={`patient-tab-${index}`}
      {...other}
    >
      {value === index && <Box sx={{ p: 3 }}>{children}</Box>}
    </div>
  );
}

// Dropdown options
const GENDER_OPTIONS = ['Male', 'Female', 'Other', 'Unknown'];
const COUNTRY_OPTIONS = ['United States', 'Canada', 'United Kingdom', 'Germany', 'France', 'Spain', 'Italy', 'Other'];
const US_STATES = [
  'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID', 'IL', 'IN', 'IA', 'KS',
  'KY', 'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY',
  'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV',
  'WI', 'WY', 'DC', 'PR', 'GU', 'VI'
];
const ETHNICITY_OPTIONS = ['African/Black', 'Asian', 'Native American', "Other/Won't Say"];
const DISEASE_OPTIONS = ['Breast Cancer', 'Follicular Lymphoma', 'Multiple Myeloma', 'Chronic Lymphocytic Leukemia (CLL)', 'Lung Cancer', 'Colon Cancer', 'Other'];
const STAGE_OPTIONS = ['0', 'I', 'IA', 'IB', 'II', 'IIA', 'IIB', 'III', 'IIIA', 'IIIB', 'IIIC', 'IV', 'Unknown'];
const HISTOLOGIC_TYPE_OPTIONS = [
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
  'Inflammatory carcinoma'
];
const ECOG_OPTIONS = ['0', '1', '2', '3', '4', '5'];
const KARNOFSKY_OPTIONS = ['100', '90', '80', '70', '60', '50', '40', '30', '20', '10', '0'];
const YES_NO_OPTIONS = ['Yes', 'No', 'Unknown'];
const POSITIVE_NEGATIVE_OPTIONS = ['Positive', 'Negative', 'Unknown'];

const NODES_STAGE_OPTIONS = [
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
  'N3c: Supraclavicular nodes'
];

const TUMOR_STAGE_OPTIONS = [
  'Tx: Primary Tumor, cannot be assessed',
  'T0: No tumor evidence',
  'Tis: Non-invasive Carcinoma in situ (DCIS, LCIS, Paget’s without tumor)',
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
  'T4d: Inflammatory carcinoma'
];

const DISTANT_METASTASIS_STAGE_OPTIONS = [
  'M0: No distant metastasis',
  'M0(i+): No metastasis on scans, but cancer cells found in blood/bone marrow/distant nodes',
  'M1: Distant metastasis present'
];

const STAGING_MODALITIES_OPTIONS = [
  'c → Clinical',
  'p → Pathological',
  'yp → Pathological after neoadjuvant therapy'
];

const ER_OPTIONS = ['ER+', 'ER-', 'ER+ with low expression', 'ER+ with high expression', 'Unknown'];
const PR_OPTIONS = ['PR+', 'PR-', 'PR+ with low expression', 'PR+ with high expression', 'Unknown'];
const HER2_OPTIONS = ['HER2+', 'HER2-', 'HER2 low', 'Unknown'];
const HR_OPTIONS = ['HR+', 'HR-', 'HR+ with low expression', 'HR+ with high expression', 'Unknown'];
const HRD_OPTIONS = ['HRD+', 'HRD-', 'Unknown'];
const MENOPAUSAL_OPTIONS = ['Pre-menopausal', 'Peri-menopausal', 'Post-menopausal', 'Unknown'];
const FLIPI_RISK_OPTIONS = ['Low', 'Intermediate', 'High'];
const GELF_OPTIONS = ['Met', 'Not Met', 'Unknown'];
const FL_TUMOR_GRADE_OPTIONS = [
  'Grade 1 (0–5 centroblasts/HPF)',
  'Grade 2 (6–15 centroblasts/HPF)',
  'Grade 3a (>15 centroblasts/HPF, centrocytes present)',
  'Grade 3b (solid sheets of centroblasts)',
];
const ISS_STAGE_OPTIONS = ['Stage I', 'Stage II', 'Stage III'];
const MM_PROGRESSION_OPTIONS = ['Stable', 'Active', 'Smoldering', 'Progressive', 'Relapsed', 'Refractory'];
const STEM_CELL_TRANSPLANT_OPTIONS = [
  'prior SCT',
  'prior autologous SCT',
  'prior allogeneic SCT',
  'recent SCT',
  'recent autologous SCT',
  'recent allogeneic SCT',
  'relapsed post-SCT',
  'relapsed post-autologous SCT',
  'relapsed post-allogeneic SCT',
  'completed tandem SCT',
  'never received SCT',
  'pre-autologous SCT',
  'pre-allogeneic SCT',
];
const CYTOGENETIC_RISK_OPTIONS = ['Standard Risk', 'High Risk', 'Very High Risk'];
const THERAPY_OUTCOME_OPTIONS = [
  'Complete Response (CR)',
  'Partial Response (PR)',
  'Stable Disease (SD)',
  'Progressive Disease (PD)',
];
const SMOKING_STATUS_OPTIONS = ['Never Smoker', 'Former Smoker', 'Current Smoker', 'Unknown'];
const ALCOHOL_USE_OPTIONS = ['None', 'Occasional', 'Moderate', 'Heavy', 'Unknown'];
const EXERCISE_FREQUENCY_OPTIONS = ['None', 'Rarely', '1-2 times/week', '3-4 times/week', '5+ times/week', 'Daily', 'Unknown'];
const REFRACTORY_STATUS_OPTIONS = ['Unknown', 'Not Refractory', 'Primary Refractory', 'Secondary Refractory', 'Multi-Refractory'];
const THERAPY_INTENT_OPTIONS = ['Adjuvant', 'Neoadjuvant', 'Metastatic'];
const DISCONTINUATION_REASON_OPTIONS = ['Progression', 'Toxicity', 'Completion'];
const DIET_TYPE_OPTIONS = ['Regular', 'Vegetarian', 'Vegan', 'Mediterranean', 'Low-carb', 'Ketogenic', 'Other'];
const SLEEP_QUALITY_OPTIONS = ['Excellent', 'Good', 'Fair', 'Poor', 'Very Poor'];
const STRESS_LEVEL_OPTIONS = ['None', 'Low', 'Moderate', 'High', 'Very High'];
const SOCIAL_SUPPORT_OPTIONS = ['Excellent', 'Good', 'Fair', 'Poor', 'None'];
const EMPLOYMENT_STATUS_OPTIONS = ['Employed Full-time', 'Employed Part-time', 'Self-employed', 'Unemployed', 'Retired', 'Disabled', 'Student', 'Homemaker'];
const EDUCATION_LEVEL_OPTIONS = ['Less than High School', 'High School Graduate', 'Some College', 'Associate Degree', 'Bachelor Degree', 'Master Degree', 'Doctoral Degree', 'Professional Degree'];
const MARITAL_STATUS_OPTIONS = ['Single', 'Married', 'Divorced', 'Widowed', 'Separated', 'Domestic Partnership'];
const INSURANCE_TYPE_OPTIONS = ['Private Insurance', 'Medicare', 'Medicaid', 'Veterans Affairs', 'Other Government', 'Self-pay', 'None'];

// Genetic mutation options
const GENE_OPTIONS = ['BRCA1', 'BRCA2', 'TP53', 'PIK3CA', 'ESR1'];
const MUTATION_OPTIONS: { [key: string]: string[] } = {
  'BRCA1': ['c.68_69delAG', 'c.5266dupC', 'c.181T>G', 'c.3756_3759del', '185delAG'],
  'BRCA2': ['c.5946delT', 'c.9097dupA', 'c.7617+1G>A', '6174delT', 'c.8537_8538del'],
  'TP53': ['R175H', 'R248Q', 'R273H', 'R248W', 'R282W'],
  'PIK3CA': ['E542K', 'E545K', 'H1047R', 'H1047L', 'E726K'],
  'ESR1': ['D538G', 'Y537S', 'Y537C', 'Y537N', 'E380Q']
};
const ORIGIN_OPTIONS = ['Germline', 'Somatic', 'Unknown'];
const INTERPRETATION_OPTIONS = ['Pathogenic', 'Likely pathogenic', 'VUS', 'Likely benign', 'Benign'];

// CLL-specific vocabulary options (sourced from vocabulary_binet_stage, etc.)
const BINET_STAGE_OPTIONS = [
  'Binet Stage A (<3 lymphoid areas involved)',
  'Binet Stage B (≥3 lymphoid areas involved)',
  'Binet Stage C (Anemia or Thrombocytopenia)',
];

const PROTEIN_EXPRESSION_OPTIONS = [
  'CD38 +ve', 'CD38 -ve',
  'ZAP-70 +ve', 'ZAP-70 -ve',
  'CD49d +ve', 'CD49d -ve',
  'CD19 +ve', 'CD19 -ve',
  'CD5 +ve', 'CD5 -ve',
  'CD20 +ve', 'CD20 -ve',
  'CD23 +ve', 'CD23 -ve',
  'Kappa (κ) light chain +ve', 'Kappa (κ) light chain -ve',
  'Lambda (λ) light chain +ve', 'Lambda (λ) light chain -ve',
];

const RICHTER_TRANSFORMATION_OPTIONS = [
  'Richter Transformation to DLBCL',
  'Richter Transformation to Hodgkin Lymphoma',
  'Richter Transformation to Non-Hodgkin Lymphoma',
  'Clonally Related RT',
  'Clonally Unrelated RT',
];

const TUMOR_BURDEN_OPTIONS = ['Low', 'Intermediate', 'High'];

const DISEASE_ACTIVITY_OPTIONS = ['Active', 'Inactive', 'Remission', 'Relapsed', 'Refractory'];

// CLL therapy options
const CLL_FIRST_LINE = [
  'Watch and Wait',
  'FCR (Fludarabine/Cyclophosphamide/Rituximab)',
  'BR (Bendamustine/Rituximab)',
  'Ibrutinib',
  'Acalabrutinib',
  'Venetoclax + Obinutuzumab',
  'Chlorambucil + Obinutuzumab',
  'Chlorambucil + Rituximab',
  'Other',
];

const CLL_SECOND_LINE = [
  'Ibrutinib',
  'Acalabrutinib',
  'Zanubrutinib',
  'Venetoclax + Rituximab',
  'Idelalisib + Rituximab',
  'Duvelisib',
  'BR (Bendamustine/Rituximab)',
  'Other',
];

const CLL_LATER_LINE = [
  'Pirtobrutinib',
  'Venetoclax',
  'Lisocabtagene maraleucel (CAR-T)',
  'Allogeneic SCT',
  'Clinical Trial',
  'Other',
];

// Disease-specific therapy options
const BREAST_CANCER_FIRST_LINE = [
  'Watchful Waiting (Active Surveillance)',
  'Lumpectomy (Lumpectomy)',
  'Mastectomy (Mastectomy)',
  'Aromatase Inhibitor (Aromatase Inhibitor)',
  'Trastuzumab (Herceptin) (Trastuzumab)',
  'Pertuzumab (Perjeta) (Pertuzumab)',
  'Genomic Testing (Genomic Testing)',
  'Tamoxifen (Tamoxifen)',
  'Letrozole (Letrozole)',
  'Anastrozole (Arimidex) (Anastrozole)',
  'Exemestane (Exemestane)',
  'Lumpectomy + Radiation (Lumpectomy, Ipsilateral Breast Radiation, Adjuvant Radiotherapy)',
  'Mastectomy + Radiation (Mastectomy, Ipsilateral Breast Radiation, Adjuvant Radiotherapy)',
  'Axillary LND + Lumpectomy + Radiation (Lumpectomy, Axillary Lymph Node Dissection (ALND), Ipsilateral Breast Radiation, Adjuvant Radiotherapy)',
  'Axillary LND + Mastectomy (Mastectomy, Axillary Lymph Node Dissection (ALND))',
  'Axillary LND + Mastectomy + Radiation (Mastectomy, Axillary Lymph Node Dissection (ALND), Ipsilateral Breast Radiation, Adjuvant Radiotherapy)'
];

const BREAST_CANCER_SECOND_LINE = [
  'Fulvestrant (Faslodex) (Fulvestrant)',
  'Exemestane + Everolimus (Exemestane, Everolimus)',
  'Atezolizumab (Atezolizumab)',
  'Sacituzumab Govitecan (Sacituzumab Govitecan)',
  'Platinum-Based Chemotherapy (Platinum-Based Chemotherapy)',
  'PARP Inhibitor (PARP Inhibitor)',
  'Other Chemotherapy (Other Chemotherapy)',
  'Capivasertib (Capivasertib)',
  'Axillary LND + Lumpectomy + Radiation (Lumpectomy, Axillary Lymph Node Dissection (ALND), Ipsilateral Breast Radiation, Adjuvant Radiotherapy)',
  'Axillary LND + Mastectomy (Mastectomy, Axillary Lymph Node Dissection (ALND))',
  'Axillary LND + Mastectomy + Radiation (Mastectomy, Axillary Lymph Node Dissection (ALND), Ipsilateral Breast Radiation, Adjuvant Radiotherapy)'
];

const BREAST_CANCER_LATER_LINE = [
  'Fulvestrant (Faslodex) (Fulvestrant)',
  'Exemestane + Everolimus (Exemestane, Everolimus)',
  'Sacituzumab Govitecan (Sacituzumab Govitecan)',
  'Alpelisib + Fulvestrant (Alpelisib, Fulvestrant)',
  'Capivasertib + Fulvestrant (Fulvestrant, Capivasertib)',
  'Elacestrant (Elacestrant)',
  'Tamoxifen (Tamoxifen)',
  'Megestrol acetate (Megestrol acetate)',
  'Capecitabine (Capecitabine)',
  'Eribulin (Eribulin)',
  'Vinorelbine (Vinorelbine)',
  'Gemcitabine (Gemcitabine)',
  'Paclitaxel (Paclitaxel)',
  'Docetaxel (Docetaxel)',
  'Trastuzumab deruxtecan (T-DXd / Enhertu) (Trastuzumab Deruxtecan)',
  'Tucatinib + Trastuzumab + Capecitabine (Trastuzumab, Capecitabine, Tucatinib)',
  'Lapatinib (Tykerb) (Lapatinib)',
  'Neratinib (Nerlynx) (Neratinib)',
  'Trastuzumab emtansine (T-DM1 / Kadcyla) (Trastuzumab Emtansine)',
  'Atezolizumab + Nab-Paclitaxel (Atezolizumab, Nab-Paclitaxel)',
  'Pembrolizumab + Chemotherapy (Pembrolizumab)',
  'Olaparib (Olaparib)',
  'Talazoparib (Talazoparib)',
  'Carboplatin (Carboplatin)',
  'Cisplatin (Cisplatin)',
  'Alpelisib (Piqray) Monotherapy (Alpelisib)',
  'Capivasertib (Capivasertib)',
  'Larotrectinib (Larotrectinib)',
  'Entrectinib (Entrectinib)',
  'Liposomal Doxorubicin (Doxorubicin)',
  'Axillary LND + Radiation (Axillary Lymph Node Dissection (ALND), Ipsilateral Breast Radiation)',
  'Axillary LND + Mastectomy (Mastectomy, Axillary Lymph Node Dissection (ALND))',
  'Axillary LND + Mastectomy + Radiation (Mastectomy, Axillary Lymph Node Dissection (ALND), Ipsilateral Breast Radiation, Adjuvant Radiotherapy)'
];

const LYMPHOMA_FIRST_LINE = [
  'R-CHOP (Rituximab/Cyclophosphamide/Doxorubicin/Vincristine/Prednisone)',
  'BR (Bendamustine/Rituximab)',
  'R-CVP (Rituximab/Cyclophosphamide/Vincristine/Prednisone)',
  'Rituximab Monotherapy',
  'Watch and Wait',
  'Other'
];

const LYMPHOMA_SECOND_LINE = [
  'R-ICE (Rituximab/Ifosfamide/Carboplatin/Etoposide)',
  'R-DHAP (Rituximab/Dexamethasone/Cytarabine/Cisplatin)',
  'BR (Bendamustine/Rituximab)',
  'Lenalidomide/Rituximab',
  'Obinutuzumab-based therapy',
  'Other'
];

const LYMPHOMA_LATER_LINE = [
  'Tazemetostat',
  'Lenalidomide/Rituximab',
  'PI3K Inhibitor (Copanlisib/Duvelisib/Idelalisib)',
  'Obinutuzumab Monotherapy',
  'Clinical Trial',
  'Other'
];

const MYELOMA_FIRST_LINE = [
  'VRd (Bortezomib/Lenalidomide/Dexamethasone)',
  'CyBorD (Cyclophosphamide/Bortezomib/Dexamethasone)',
  'DRd (Daratumumab/Lenalidomide/Dexamethasone)',
  'RVd (Lenalidomide/Bortezomib/Dexamethasone)',
  'KRd (Carfilzomib/Lenalidomide/Dexamethasone)',
  'Other'
];

const MYELOMA_SECOND_LINE = [
  'DVd (Daratumumab/Bortezomib/Dexamethasone)',
  'KRd (Carfilzomib/Lenalidomide/Dexamethasone)',
  'DRd (Daratumumab/Lenalidomide/Dexamethasone)',
  'Elotuzumab/Lenalidomide/Dexamethasone',
  'Ixazomib/Lenalidomide/Dexamethasone',
  'Carfilzomib/Dexamethasone',
  'Other'
];

const MYELOMA_LATER_LINE = [
  'Isatuximab/Pomalidomide/Dexamethasone',
  'Daratumumab/Pomalidomide/Dexamethasone',
  'Selinexor/Bortezomib/Dexamethasone',
  'Belantamab mafodotin',
  'CAR-T (Idecabtagene vicleucel/Ciltacabtagene autoleucel)',
  'Clinical Trial',
  'Other'
];

const SUPPORTIVE_THERAPIES_OPTIONS = [
  'Adjuvant Radiotherapy (Adjuvant Radiotherapy)',
  'systemic corticosteroids (e.g., prednisone) =< 5 mg/day (Systemic Corticosteroid =< 5 mg/day)',
  'systemic corticosteroids (e.g., prednisone) > 5 mg/day (Systemic Corticosteroid > 5 mg/day)',
  'systemic corticosteroids (e.g., prednisone) > 10 mg/day (Systemic Corticosteroid > 10 mg/day)',
  'systemic corticosteroids (e.g., prednisone) > 20 mg/day (Systemic Corticosteroid > 20 mg/day)',
  'mineralocorticoids (e.g., fludrocortisone) (Mineralocorticoid)',
  'Inhaled corticosteroids (Inhaled corticosteroid)',
  'Topical corticosteroids (Topical corticosteroid)',
  'Intranasal corticosteroids (Intranasal corticosteroid)',
  'Immunosuppressant (Immunosuppressant)',
  'antiviral (Antiviral)',
  'Warfarin - anticoagulant (Warfarin)',
  'heparin - anticoagulant (Heparin)',
  'Aspirin =< 81mg daily (Aspirin =< 81mg daily)',
  'Aspirin > 81mg daily (Aspirin > 81mg daily)',
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
  "St. John's Wort (St. John's Wort)",
  'Grapefruit juice (Grapefruit juice)',
  'Pamidronate (Pamidronate)',
  'Zoledronic Acid (Zoledronic Acid)',
  'Denosumab (Xgeva) (Denosumab)',
  'Granulocyte-Colony Stimulating factor (G-CSF) (Growth Factor)',
  'Granulocyte-Macrophage Colony-Stimulating Factor (GM-CSF) (Growth Factor)',
  'Erythropoiesis-Stimulating Agent (ESA) (Growth Factor)',
  'LHRH/GnRH agonists (e.g., goserelin, leuprolide, triptorelin, buserelin) (LHRH/GnRH agonist)',
  'Palbociclib (Ibrance) (Palbociclib)',
  'Ribociclib (Kisqali) (Ribociclib)',
  'Abemaciclib (Verzenio) (Abemaciclib)',
  'HRT (Estrogen-containing medication) (Estrogen)',
  'Contraceptives (Estrogen-containing medication) (Estrogen)',
  'Tamoxifen Maintenance (Tamoxifen)',
  'Anastrozole (Arimidex) Maintenance (Anastrozole)',
  'Letrozole Maintenance (Letrozole)',
  'Exemestane Maintenance (Exemestane)',
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
  'Lapatinib (Tykerb) Maintenance (Lapatinib)',
  'Neratinib (Nerlynx) Maintenance (Neratinib)',
  'Advil / Motrin IB / Ibuprofen (Ibuprofen)',
  'Aleve (Naproxen Sodium)',
  'Voltaren (Diclofenac Sodium)',
  'Cataflam (Diclofenac Potassium)'
];

const PLANNED_THERAPIES = [
  // Breast Cancer Therapies
  'No planned therapy',
  'Surgery',
  'breast-conserving surgery (lumpectomy)',
  'mastectomy',
  'axillary lymph node dissection',
  'Neoadjuvant Chemotherapy',
  'Neoadjuvant Anthracycline-based Chemotherapy',
  'Neoadjuvant Taxane-based Chemotherapy',
  'Neoadjuvant Platinum-based Chemotherapy',
  'Neoadjuvant Endocrine/Hormonal Therapy',
  'Neoadjuvant Aromatase inhibitors (e.g., letrozole, anastrozole)',
  'Neoadjuvant Tamoxifen',
  'Neoadjuvant Ovarian suppression (e.g., goserelin)',
  'Neoadjuvant HER2-Targeted Therapy',
  'Neoadjuvant Trastuzumab (Herceptin)',
  'Neoadjuvant Pertuzumab (Perjeta)',
  'Neoadjuvant Trastuzumab emtansine (T-DM1)',
  'Neoadjuvant Immunotherapy',
  'Neoadjuvant Checkpoint inhibitors (e.g., pembrolizumab, atezolizumab)',
  'Neoadjuvant Radiotherapy',
  'Neoadjuvant External beam radiation therapy',
  'Neoadjuvant Targeted intraoperative radiotherapy',
  'Adjuvant Chemotherapy',
  'Adjuvant Endocrine/Hormonal Therapy',
  'Adjuvant HER2-Targeted Therapy',
  'Adjuvant trastuzumab',
  'Adjuvant Radiotherapy',
  'Chemotherapy',
  'Anthracycline-based Chemotherapy',
  'Taxane-based Chemotherapy',
  'Platinum-based Chemotherapy',
  'Endocrine/Hormonal Therapy',
  'Aromatase inhibitors (e.g., letrozole, anastrozole)',
  'Tamoxifen',
  'Ovarian suppression (e.g., goserelin)',
  'Trastuzumab (Herceptin)',
  'Pertuzumab (Perjeta)',
  'Trastuzumab emtansine (T-DM1)',
  'Immunotherapy',
  'Checkpoint inhibitors (experimental)',
  'Radiotherapy',
  'External beam radiation therapy',
  'Targeted intraoperative radiotherapy',
  'Bone-Modifying Agents',
  'Bisphosphonates (e.g., zoledronic acid)',
  'Denosumab',
  'Targeted therapy',
  'Sentinel Lymph Node Biopsy (SLNB)',
  'Anti-HER2 ADCs',
  'Anti-HER2 Monoclonal Antibodies',
  'HER2 Tyrosine Kinase Inhibitors',
  // Lymphoma Therapies
  'R-CHOP (Rituximab/Cyclophosphamide/Doxorubicin/Vincristine/Prednisone)',
  'BR (Bendamustine/Rituximab)',
  'R-CVP (Rituximab/Cyclophosphamide/Vincristine/Prednisone)',
  'R-ICE (Rituximab/Ifosfamide/Carboplatin/Etoposide)',
  'R-DHAP (Rituximab/Dexamethasone/Cytarabine/Cisplatin)',
  'Rituximab Monotherapy',
  'Obinutuzumab-based therapy',
  'Obinutuzumab Monotherapy',
  'Lenalidomide/Rituximab',
  'Tazemetostat',
  'PI3K Inhibitor (Copanlisib/Duvelisib/Idelalisib)',
  // Myeloma Therapies
  'VRd (Bortezomib/Lenalidomide/Dexamethasone)',
  'CyBorD (Cyclophosphamide/Bortezomib/Dexamethasone)',
  'DRd (Daratumumab/Lenalidomide/Dexamethasone)',
  'KRd (Carfilzomib/Lenalidomide/Dexamethasone)',
  'DVd (Daratumumab/Bortezomib/Dexamethasone)',
  'Elotuzumab/Lenalidomide/Dexamethasone',
  'Ixazomib/Lenalidomide/Dexamethasone',
  'Carfilzomib/Dexamethasone',
  'Isatuximab/Pomalidomide/Dexamethasone',
  'Daratumumab/Pomalidomide/Dexamethasone',
  'Selinexor/Bortezomib/Dexamethasone',
  'Belantamab mafodotin',
  'CAR-T Therapy',
  // General Options
  'Autologous Stem Cell Transplant',
  'Allogeneic Stem Cell Transplant',
  'Radiation Therapy',
  'Surgery',
  'Clinical Trial',
  'Watch and Wait',
  'Other'
];

const PatientDetail: React.FC = () => {
  const { personId } = useParams<{ personId: string }>();
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [patientInfo, setPatientInfo] = useState<any>(null);
  const [editedInfo, setEditedInfo] = useState<any>({});
  const [patientName, setPatientName] = useState<string>('');
  const [editedName, setEditedName] = useState<string>('');
  const [activeTab, setActiveTab] = useState(0);

  // Vocabulary sources for tooltip display
  const { source: ecogSource }              = useVocabulary('ecog-status', 'code');
  const { source: karnofskySource }         = useVocabulary('karnofsky-score', 'code');
  const { source: diseaseSource }           = useVocabulary('disease', 'title');
  const { source: cancerStageSource }       = useVocabulary('cancer-stage', 'title');
  const { source: ethnicitySource }         = useVocabulary('ethnicity', 'title');
  const { source: gelfSource }              = useVocabulary('gelf-criteria', 'title');
  const { source: flipiSource }             = useVocabulary('flipi-score', 'code');
  const { source: flGradeSource }           = useVocabulary('follicular-lymphoma-grade', 'title');
  const { source: progressionSource }       = useVocabulary('disease-progression', 'title');
  const { source: erSource }                = useVocabulary('estrogen-receptor-status', 'title');
  const { source: prSource }                = useVocabulary('progesterone-receptor-status', 'title');
  const { source: her2Source }              = useVocabulary('her2-status', 'title');
  const { source: hrSource }                = useVocabulary('hr-status', 'title');
  const { source: hrdSource }               = useVocabulary('hrd-status', 'title');
  const { source: tumorStageSource }        = useVocabulary('tumor-stage', 'title');
  const { source: nodesStageSource }        = useVocabulary('nodes-stage', 'title');
  const { source: distantMetSource }        = useVocabulary('distant-metastasis-stage', 'title');
  const { source: stagingModalitySource }   = useVocabulary('staging-modality', 'title');
  const { options: histologicOptions, source: histologicSource } = useVocabulary('histologic-type', 'title');
  const { options: bcFirstLineOptions, source: bcFirstLineSource }   = useVocabulary('breast-cancer-first-line-therapy', 'title');
  const { options: bcSecondLineOptions, source: bcSecondLineSource } = useVocabulary('breast-cancer-second-line-therapy', 'title');
  const { options: bcLaterLineOptions, source: bcLaterLineSource }   = useVocabulary('breast-cancer-later-line-therapy', 'title');

  useEffect(() => {
    const fetchPatientInfo = async () => {
      if (!personId) return;

      try {
        setLoading(true);
        const response = await api.get(`/patient-info/${personId}/`);
        const patientData = response.data.patient_info;
        
        // Convert ECOG from integer to string for dropdown
        if (patientData.ecog_performance_status !== null && patientData.ecog_performance_status !== undefined) {
          patientData.ecog_performance_status = String(patientData.ecog_performance_status);
        }
        
        // Auto-compute Triple Negative status if not already set
        if (patientData.estrogen_receptor_status && patientData.progesterone_receptor_status && patientData.her2_status) {
          const erNeg = patientData.estrogen_receptor_status === 'Negative' || patientData.estrogen_receptor_status === 'ER-';
          const prNeg = patientData.progesterone_receptor_status === 'Negative' || patientData.progesterone_receptor_status === 'PR-';
          const her2Neg = patientData.her2_status === 'Negative' || patientData.her2_status === 'HER2-';
          patientData.tnbc_status = erNeg && prNeg && her2Neg;
        }
        
        setPatientInfo(patientData);
        setEditedInfo(patientData);
        
        if (response.data.user) {
          const user = response.data.user;
          const fullName = `${user.first_name} ${user.last_name}`.trim();
          setPatientName(fullName || user.username || `Patient ${personId}`);
          setEditedName(fullName || user.username || `Patient ${personId}`);
        } else {
          setPatientName(`Patient ${personId}`);
          setEditedName(`Patient ${personId}`);
        }
        
        setError(null);
      } catch (err: any) {
        setError(err.response?.data?.error || 'Failed to fetch patient information');
      } finally {
        setLoading(false);
      }
    };

    fetchPatientInfo();
  }, [personId]);

  const handleTabChange = (event: React.SyntheticEvent, newValue: number) => {
    setActiveTab(newValue);
  };

  const handleFieldChange = (field: string, value: any) => {
    const updatedInfo = { ...editedInfo, [field]: value };
    
    // Auto-compute Triple Negative status based on ER, PR, and HER2 statuses
    if (field === 'estrogen_receptor_status' || field === 'progesterone_receptor_status' || field === 'her2_status') {
      const er = field === 'estrogen_receptor_status' ? value : updatedInfo.estrogen_receptor_status;
      const pr = field === 'progesterone_receptor_status' ? value : updatedInfo.progesterone_receptor_status;
      const her2 = field === 'her2_status' ? value : updatedInfo.her2_status;
      
      // Triple negative if all three are negative (handle both old 'Negative' and new 'ER-'/'PR-'/'HER2-' vocab)
      const erNeg = er === 'Negative' || er === 'ER-';
      const prNeg = pr === 'Negative' || pr === 'PR-';
      const her2Neg = her2 === 'Negative' || her2 === 'HER2-';
      if (erNeg && prNeg && her2Neg) {
        updatedInfo.tnbc_status = true;
      } else if (er || pr || her2) {
        updatedInfo.tnbc_status = false;
      }
    }
    
    setEditedInfo(updatedInfo);
    setSuccessMessage(null);
  };

  const handleMutationAdd = () => {
    const mutations = editedInfo.genetic_mutations || [];
    mutations.push({
      gene: '',
      mutation: '',
      origin: '',
      interpretation: ''
    });
    handleFieldChange('genetic_mutations', mutations);
  };

  const handleMutationRemove = (index: number) => {
    const mutations = [...(editedInfo.genetic_mutations || [])];
    mutations.splice(index, 1);
    handleFieldChange('genetic_mutations', mutations);
  };

  const handleMutationChange = (index: number, field: string, value: string) => {
    const mutations = [...(editedInfo.genetic_mutations || [])];
    mutations[index] = { ...mutations[index], [field]: value };
    
    // Reset mutation when gene changes
    if (field === 'gene') {
      mutations[index].mutation = '';
    }
    
    handleFieldChange('genetic_mutations', mutations);
  };

  const handleZipcodeChange = async (zipcode: string) => {
    handleFieldChange('postal_code', zipcode);
    
    // Only lookup for 5-digit US zipcodes
    if (zipcode && zipcode.length === 5 && /^\d{5}$/.test(zipcode)) {
      try {
        const response = await fetch(`https://api.zippopotam.us/us/${zipcode}`);
        if (response.ok) {
          const data = await response.json();
          if (data.places && data.places.length > 0) {
            const place = data.places[0];
            // Auto-populate city and state
            setEditedInfo({
              ...editedInfo,
              postal_code: zipcode,
              city: place['place name'],
              region: place['state']
            });
          }
        }
      } catch (error) {
        // Silently fail - user can still enter manually
        console.log('Zipcode lookup failed:', error);
      }
    }
  };

  const handleSave = async () => {
    try {
      setSaving(true);
      setError(null);
      
      // Save patient info
      await api.patch(`/patient-info/${personId}/`, editedInfo);
      
      // Update patient name if changed
      if (editedName !== patientName) {
        const nameParts = editedName.trim().split(' ');
        const firstName = nameParts[0] || '';
        const lastName = nameParts.slice(1).join(' ') || '';
        
        await api.patch(`/user/`, {
          first_name: firstName,
          last_name: lastName
        });
        
        setPatientName(editedName);
      }
      
      setPatientInfo(editedInfo);
      setSuccessMessage('Patient information saved successfully');
      
      setTimeout(() => setSuccessMessage(null), 3000);
    } catch (err: any) {
      setError(err.response?.data?.error || 'Failed to save patient information');
    } finally {
      setSaving(false);
    }
  };

  const calculateAge = (dateOfBirth: string) => {
    if (!dateOfBirth) return null;
    const today = new Date();
    const birthDate = new Date(dateOfBirth);
    let age = today.getFullYear() - birthDate.getFullYear();
    const monthDiff = today.getMonth() - birthDate.getMonth();
    if (monthDiff < 0 || (monthDiff === 0 && today.getDate() < birthDate.getDate())) {
      age--;
    }
    return age;
  };

  const formatDateForInput = (dateString: string) => {
    if (!dateString) return '';
    try {
      const date = new Date(dateString);
      return date.toISOString().split('T')[0];
    } catch {
      return '';
    }
  };

  const getDiseaseType = () => {
    const disease = editedInfo?.disease?.toLowerCase() || '';
    if (disease.includes('breast')) return 'breast';
    if (disease.includes('lymphoma')) return 'lymphoma';
    if (disease.includes('myeloma')) return 'myeloma';
    if (disease.includes('cll') || disease.includes('chronic lymphocytic')) return 'cll';
    return 'other';
  };

  const getDiseaseTabLabel = () => {
    const diseaseType = getDiseaseType();
    switch (diseaseType) {
      case 'breast':
        return 'Breast Cancer';
      case 'lymphoma':
        return 'Follicular Lymphoma';
      case 'myeloma':
        return 'Multiple Myeloma';
      case 'cll':
        return 'CLL';
      default:
        return 'Disease Specific';
    }
  };

  const getTherapyOptions = (line: 'first' | 'second' | 'later') => {
    const diseaseType = getDiseaseType();

    switch (diseaseType) {
      case 'breast':
        if (line === 'first') return bcFirstLineOptions.length ? bcFirstLineOptions.map(o => o.value) : BREAST_CANCER_FIRST_LINE;
        if (line === 'second') return bcSecondLineOptions.length ? bcSecondLineOptions.map(o => o.value) : BREAST_CANCER_SECOND_LINE;
        return bcLaterLineOptions.length ? bcLaterLineOptions.map(o => o.value) : BREAST_CANCER_LATER_LINE;
      case 'lymphoma':
        if (line === 'first') return LYMPHOMA_FIRST_LINE;
        if (line === 'second') return LYMPHOMA_SECOND_LINE;
        return LYMPHOMA_LATER_LINE;
      case 'myeloma':
        if (line === 'first') return MYELOMA_FIRST_LINE;
        if (line === 'second') return MYELOMA_SECOND_LINE;
        return MYELOMA_LATER_LINE;
      case 'cll':
        if (line === 'first') return CLL_FIRST_LINE;
        if (line === 'second') return CLL_SECOND_LINE;
        return CLL_LATER_LINE;
      default:
        return ['Other'];
    }
  };

  if (loading) {
    return (
      <Box display="flex" justifyContent="center" alignItems="center" minHeight="400px">
        <CircularProgress />
      </Box>
    );
  }

  if (error && !patientInfo) {
    return (
      <Box p={3}>
        <Alert severity="error">{error}</Alert>
        <Button startIcon={<ArrowLeft />} onClick={() => navigate('/')} sx={{ mt: 2 }}>
          Back to Patient List
        </Button>
      </Box>
    );
  }

  const renderTextField = (label: string, field: string, fullWidth: boolean = false, type: string = 'text', disabled: boolean = false) => {
      // For number fields, 0 should show as '0', not empty string
      const fieldValue = editedInfo?.[field];
      const displayValue = fieldValue === 0 ? 0 : (fieldValue || '');
      
      return (
        <Grid item xs={12} md={fullWidth ? 12 : 6}>
          <TextField
            fullWidth
            label={label}
            type={type}
            value={displayValue}
            onChange={(e) => handleFieldChange(field, e.target.value)}
            variant="outlined"
            size="small"
            disabled={disabled}
          />
        </Grid>
      );
    };

  const renderDateField = (label: string, field: string, fullWidth: boolean = false) => {
    return (
      <Grid item xs={12} md={fullWidth ? 12 : 6}>
        <TextField
          fullWidth
          label={label}
          type="date"
          value={formatDateForInput(editedInfo?.[field])}
          onChange={(e) => handleFieldChange(field, e.target.value)}
          variant="outlined"
          size="small"
          InputLabelProps={{ shrink: true }}
        />
      </Grid>
    );
  };

  const renderSelectField = (label: string, field: string, options: string[], fullWidth: boolean = false, disabled: boolean = false, source?: VocabSource | null) => {
    const currentValue = editedInfo?.[field] || '';

    // Add current value to options if it exists but is not in the list
    const displayOptions = [...options];
    if (currentValue && !options.includes(currentValue)) {
      displayOptions.unshift(currentValue);
    }

    return (
      <Grid item xs={12} md={fullWidth ? 12 : 6}>
        <Box sx={{ position: 'relative' }}>
          <FormControl fullWidth size="small">
            <InputLabel>{label}</InputLabel>
            <Select
              value={currentValue}
              label={label}
              onChange={(e) => handleFieldChange(field, e.target.value)}
              disabled={disabled}
            >
              <MenuItem value="">
                <em>None</em>
              </MenuItem>
              {displayOptions.map((option) => (
                <MenuItem key={option} value={option}>
                  {option}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          {source && (
            <Tooltip
              title={
                <Box>
                  <Typography variant="caption" display="block" sx={{ fontWeight: 'bold', mb: 0.5 }}>
                    Source: {source.name}
                  </Typography>
                  <Typography variant="caption" display="block">
                    <a
                      href={source.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{ color: '#90caf9' }}
                      onClick={(e) => e.stopPropagation()}
                    >
                      {source.url} ↗
                    </a>
                  </Typography>
                </Box>
              }
              placement="top"
              arrow
            >
              <Box
                component="span"
                sx={{
                  position: 'absolute',
                  top: -8,
                  right: -8,
                  width: 18,
                  height: 18,
                  borderRadius: '50%',
                  bgcolor: 'primary.main',
                  color: 'white',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  fontSize: '11px',
                  fontWeight: 'bold',
                  cursor: 'help',
                  zIndex: 1,
                  lineHeight: 1,
                  userSelect: 'none',
                }}
              >
                i
              </Box>
            </Tooltip>
          )}
        </Box>
      </Grid>
    );
  };

  const renderMultiSelectField = (label: string, field: string, options: string[], fullWidth: boolean = false, disabled: boolean = false) => {
    const currentValueString = editedInfo?.[field] || '';
    // Split by comma+space or comma, map to trimmed strings
    const currentValueArray = currentValueString ? currentValueString.split(',').map((s: string) => s.trim()).filter(Boolean) : [];
    
    const displayOptions = [...options];
    currentValueArray.forEach((val: string) => {
      if (val && !displayOptions.includes(val)) {
        displayOptions.unshift(val);
      }
    });

    const handleChange = (event: any) => {
      const {
        target: { value },
      } = event;
      // On autofill we get a stringified value.
      const valArray = typeof value === 'string' ? value.split(',') : value;
      handleFieldChange(field, valArray.join(', '));
    };
    
    return (
      <Grid item xs={12} md={fullWidth ? 12 : 6}>
        <FormControl fullWidth size="small">
          <InputLabel>{label}</InputLabel>
          <Select
            multiple
            value={currentValueArray}
            label={label}
            onChange={handleChange}
            disabled={disabled}
            renderValue={(selected) => selected.join(', ')}
          >
            {displayOptions.map((option) => (
              <MenuItem key={option} value={option}>
                <Checkbox checked={currentValueArray.indexOf(option) > -1} />
                <ListItemText primary={option} />
              </MenuItem>
            ))}
          </Select>
        </FormControl>
      </Grid>
    );
  };

  const renderBooleanField = (label: string, field: string) => {
    // Convert boolean to Yes/No for display
    const boolValue = editedInfo?.[field];
    const displayValue = boolValue === true ? 'Yes' : boolValue === false ? 'No' : '';
    
    return (
      <Grid item xs={12} md={6}>
        <FormControl fullWidth size="small">
          <InputLabel>{label}</InputLabel>
          <Select
            value={displayValue}
            label={label}
            onChange={(e) => {
              // Convert Yes/No back to boolean
              const newValue = e.target.value === 'Yes' ? true : e.target.value === 'No' ? false : null;
              handleFieldChange(field, newValue);
            }}
          >
            <MenuItem value="">
              <em>None</em>
            </MenuItem>
            <MenuItem value="Yes">Yes</MenuItem>
            <MenuItem value="No">No</MenuItem>
          </Select>
        </FormControl>
      </Grid>
    );
  };

  const renderBreastCancerTab = () => (
    <Grid container spacing={3}>
      <Grid item xs={12}>
        <Typography variant="h6" gutterBottom>Tumor Characteristics</Typography>
      </Grid>
      {renderSelectField('Histologic Type', 'histologic_type', histologicOptions.length ? histologicOptions.map(o => o.value) : HISTOLOGIC_TYPE_OPTIONS, true, false, histologicSource)}
      {renderSelectField('Menopausal Status', 'menopausal_status', MENOPAUSAL_OPTIONS)}
      {renderSelectField('Tumor Stage', 'tumor_stage', TUMOR_STAGE_OPTIONS, false, false, tumorStageSource)}
      {renderSelectField('Nodes Stage', 'nodes_stage', NODES_STAGE_OPTIONS, false, false, nodesStageSource)}
      {renderSelectField('Staging Modalities', 'staging_modalities', STAGING_MODALITIES_OPTIONS, false, false, stagingModalitySource)}
      {renderSelectField('Distant Metastasis Stage', 'distant_metastasis_stage', DISTANT_METASTASIS_STAGE_OPTIONS, false, false, distantMetSource)}
      {renderBooleanField('Bone-Only Metastasis', 'bone_only_metastasis_status')}
      {renderBooleanField('Measurable Disease by RECIST', 'measurable_disease_by_recist_status')}

      <Grid item xs={12}>
        <Divider sx={{ my: 2 }} />
        <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>
          Receptor Status
        </Typography>
      </Grid>
      {renderSelectField('Estrogen Receptor (ER) Status', 'estrogen_receptor_status', ER_OPTIONS, false, false, erSource)}
      {renderSelectField('Progesterone Receptor (PR) Status', 'progesterone_receptor_status', PR_OPTIONS, false, false, prSource)}
      {renderSelectField('HER2 Status', 'her2_status', HER2_OPTIONS, false, false, her2Source)}
      {renderSelectField('HR Status', 'hr_status', HR_OPTIONS, false, false, hrSource)}
      {renderSelectField('HRD Status', 'hrd_status', HRD_OPTIONS, false, false, hrdSource)}
      {renderSelectField('Androgen Receptor Status', 'androgen_receptor_status', ER_OPTIONS)}
      
      <Grid item xs={12} sm={6}>
        <TextField
          fullWidth
          label="Triple Negative Status (Computed)"
          value={editedInfo.tnbc_status ? 'Yes' : 'No'}
          variant="outlined"
          disabled
          InputProps={{
            readOnly: true,
          }}
          helperText="Automatically computed from ER, PR, and HER2 status"
        />
      </Grid>
      
      <Grid item xs={12}>
        <Divider sx={{ my: 2 }} />
        <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>
          Additional Biomarkers
        </Typography>
      </Grid>
      {renderTextField('Ki-67 Proliferation Index (%)', 'ki67_proliferation_index', false, 'number')}
      {renderTextField('PD-L1 Status (%)', 'pd_l1_tumor_cells', false, 'number')}
      {renderTextField('Oncotype DX Score', 'oncotype_dx_score', false, 'number')}
      
      <Grid item xs={12}>
        <Divider sx={{ my: 2 }} />
        <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>
          Test Information
        </Typography>
      </Grid>
      {renderTextField('Test Methodology', 'test_methodology', true)}
      {renderDateField('Test Date', 'test_date')}
      {renderTextField('Test Specimen Type', 'test_specimen_type', true)}
      {renderTextField('Report Interpretation', 'report_interpretation', true)}
      
      <Grid item xs={12}>
        <Divider sx={{ my: 2 }} />
        <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>
          Genetic Mutations
        </Typography>
      </Grid>
      
      <Grid item xs={12}>
        <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
          <Typography variant="body2" color="text.secondary">
            {(editedInfo.genetic_mutations || []).length} mutation(s) identified
          </Typography>
          <Button
            variant="outlined"
            size="small"
            onClick={handleMutationAdd}
          >
            Add Mutation
          </Button>
        </Box>
        
        {(editedInfo.genetic_mutations || []).map((mutation: any, index: number) => (
          <Box key={index} sx={{ mb: 3, p: 2, border: '1px solid #e0e0e0', borderRadius: 1 }}>
            <Box sx={{ display: 'flex', justifyContent: 'space-between', mb: 2 }}>
              <Typography variant="subtitle2">Mutation {index + 1}</Typography>
              <Button
                size="small"
                color="error"
                onClick={() => handleMutationRemove(index)}
              >
                Remove
              </Button>
            </Box>
            <Grid container spacing={2}>
              <Grid item xs={12} sm={6} md={3}>
                <FormControl fullWidth size="small">
                  <InputLabel>Gene</InputLabel>
                  <Select
                    value={mutation.gene || ''}
                    label="Gene"
                    onChange={(e) => handleMutationChange(index, 'gene', e.target.value)}
                  >
                    {GENE_OPTIONS.map((gene) => (
                      <MenuItem key={gene} value={gene}>{gene}</MenuItem>
                    ))}
                  </Select>
                </FormControl>
              </Grid>
              <Grid item xs={12} sm={6} md={3}>
                <FormControl fullWidth size="small" disabled={!mutation.gene}>
                  <InputLabel>Mutation</InputLabel>
                  <Select
                    value={mutation.mutation || ''}
                    label="Mutation"
                    onChange={(e) => handleMutationChange(index, 'mutation', e.target.value)}
                  >
                    {mutation.gene && MUTATION_OPTIONS[mutation.gene]?.map((mut: string) => (
                      <MenuItem key={mut} value={mut}>{mut}</MenuItem>
                    ))}
                  </Select>
                </FormControl>
              </Grid>
              <Grid item xs={12} sm={6} md={3}>
                <FormControl fullWidth size="small">
                  <InputLabel>Origin</InputLabel>
                  <Select
                    value={mutation.origin || ''}
                    label="Origin"
                    onChange={(e) => handleMutationChange(index, 'origin', e.target.value)}
                  >
                    {ORIGIN_OPTIONS.map((origin) => (
                      <MenuItem key={origin} value={origin}>{origin}</MenuItem>
                    ))}
                  </Select>
                </FormControl>
              </Grid>
              <Grid item xs={12} sm={6} md={3}>
                <FormControl fullWidth size="small">
                  <InputLabel>Interpretation</InputLabel>
                  <Select
                    value={mutation.interpretation || ''}
                    label="Interpretation"
                    onChange={(e) => handleMutationChange(index, 'interpretation', e.target.value)}
                  >
                    {INTERPRETATION_OPTIONS.map((interp) => (
                      <MenuItem key={interp} value={interp}>{interp}</MenuItem>
                    ))}
                  </Select>
                </FormControl>
              </Grid>
            </Grid>
          </Box>
        ))}
        
        {(!editedInfo.genetic_mutations || editedInfo.genetic_mutations.length === 0) && (
          <Typography variant="body2" color="text.secondary" sx={{ fontStyle: 'italic', textAlign: 'center', py: 2 }}>
            No genetic mutations identified. Click "Add Mutation" to add one.
          </Typography>
        )}
      </Grid>
    </Grid>
  );

  const renderLymphomaTab = () => (
    <Grid container spacing={3}>
      <Grid item xs={12}>
        <Typography variant="h6" gutterBottom>Disease Characteristics</Typography>
      </Grid>
      {renderSelectField('Histologic Subtype', 'histologic_type', histologicOptions.length ? histologicOptions.map(o => o.value) : HISTOLOGIC_TYPE_OPTIONS, true, false, histologicSource)}
      {renderSelectField('Ann Arbor Stage', 'stage', STAGE_OPTIONS)}
      {renderSelectField('Tumor Grade', 'tumor_grade', FL_TUMOR_GRADE_OPTIONS, false, false, flGradeSource)}
      {renderSelectField('GELF Criteria', 'gelf_criteria_status', GELF_OPTIONS, false, false, gelfSource)}
      {renderTextField('FLIPI Score', 'flipi_score', false, 'number')}
      {renderSelectField('FLIPI Risk Category', 'flipi_risk_category', FLIPI_RISK_OPTIONS, false, false, flipiSource)}
      {renderSelectField('Bulky Disease', 'bulky_disease', YES_NO_OPTIONS)}
      {renderSelectField('B Symptoms', 'b_symptoms', YES_NO_OPTIONS)}

      <Grid item xs={12}>
        <Divider sx={{ my: 2 }} />
        <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>
          Laboratory Markers
        </Typography>
      </Grid>
      {renderTextField('LDH Level (U/L)', 'ldh_level', false, 'number')}
      {renderTextField('Beta-2 Microglobulin (mg/L)', 'beta2_microglobulin', false, 'number')}
      {renderSelectField('Bone Marrow Involvement', 'bone_marrow_involvement', YES_NO_OPTIONS)}
      {renderTextField('Clonal Bone Marrow B Lymphocytes (%)', 'clonal_bone_marrow_b_lymphocytes', false, 'number')}
      {renderTextField('Number of Nodal Sites', 'number_of_nodal_sites', false, 'number')}
    </Grid>
  );

  const renderMyelomaTab = () => (
    <Grid container spacing={3}>
      <Grid item xs={12}>
        <Typography variant="h6" gutterBottom>Disease Characteristics</Typography>
      </Grid>
      {renderTextField('Myeloma Type', 'myeloma_type')}
      {renderSelectField('ISS Stage', 'stage', ISS_STAGE_OPTIONS)}
      {renderSelectField('R-ISS Stage', 'r_iss_stage', ISS_STAGE_OPTIONS)}
      {renderTextField('Durie-Salmon Stage', 'durie_salmon_stage')}
      {renderSelectField('Progression Status', 'progression', MM_PROGRESSION_OPTIONS, false, false, progressionSource)}
      {renderMultiSelectField('Stem Cell Transplant History', 'stem_cell_transplant_history', STEM_CELL_TRANSPLANT_OPTIONS, true)}
      
      <Grid item xs={12}>
        <Divider sx={{ my: 2 }} />
        <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>
          Myeloma Markers
        </Typography>
      </Grid>
      {renderTextField('M-Protein Type', 'm_protein_type')}
      {renderTextField('Serum M-Protein (g/dL)', 'serum_m_protein', false, 'number')}
      {renderTextField('Urine M-Protein (mg/24h)', 'urine_m_protein', false, 'number')}
      {renderTextField('Free Light Chain Ratio', 'free_light_chain_ratio', false, 'number')}
      {renderTextField('Beta-2 Microglobulin (mg/L)', 'beta2_microglobulin', false, 'number')}
      {renderTextField('LDH Level (U/L)', 'ldh_level', false, 'number')}
      
      <Grid item xs={12}>
        <Divider sx={{ my: 2 }} />
        <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>
          Complications
        </Typography>
      </Grid>
      {renderSelectField('Bone Lesions', 'bone_lesions', YES_NO_OPTIONS)}
      {renderSelectField('Hypercalcemia', 'hypercalcemia', YES_NO_OPTIONS)}
      {renderSelectField('Renal Impairment', 'renal_impairment', YES_NO_OPTIONS)}
      {renderSelectField('Anemia', 'anemia', YES_NO_OPTIONS)}
      {renderTextField('Plasma Cell Percentage (%)', 'plasma_cell_percentage', false, 'number')}
      
      <Grid item xs={12}>
        <Divider sx={{ my: 2 }} />
        <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>
          Cytogenetics
        </Typography>
      </Grid>
      {renderSelectField('Cytogenetic Risk', 'cytogenetic_risk', CYTOGENETIC_RISK_OPTIONS)}
      {renderTextField('Cytogenetic Abnormalities', 'cytogenetic_abnormalities', true)}
      {renderTextField('Genetic Mutations', 'genetic_mutations', true)}
    </Grid>
  );

  const renderCLLTab = () => (
    <Grid container spacing={3}>
      <Grid item xs={12}>
        <Typography variant="h6" gutterBottom>CLL Disease Characteristics</Typography>
      </Grid>
      {renderSelectField('Binet Stage', 'binet_stage', BINET_STAGE_OPTIONS)}
      {renderSelectField('Tumor Burden', 'tumor_burden', TUMOR_BURDEN_OPTIONS)}
      {renderSelectField('Disease Activity', 'disease_activity', DISEASE_ACTIVITY_OPTIONS)}
      {renderSelectField('Richter Transformation', 'richter_transformation', RICHTER_TRANSFORMATION_OPTIONS)}
      {renderMultiSelectField('Protein Expressions', 'protein_expressions', PROTEIN_EXPRESSION_OPTIONS, true)}

      <Grid item xs={12}>
        <Divider sx={{ my: 2 }} />
        <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>
          Laboratory Markers
        </Typography>
      </Grid>
      {renderTextField('Absolute Lymphocyte Count (×10⁹/L)', 'absolute_lymphocyte_count', false, 'number')}
      {renderTextField('Lymphocyte Doubling Time (months)', 'lymphocyte_doubling_time', false, 'number')}
      {renderTextField('Serum Beta-2 Microglobulin (mg/L)', 'serum_beta2_microglobulin_level', false, 'number')}
      {renderTextField('Clonal B-Lymphocyte Count', 'clonal_b_lymphocyte_count', false, 'number')}
      {renderTextField('Clonal Bone Marrow B-Lymphocytes (%)', 'clonal_bone_marrow_b_lymphocytes', false, 'number')}
      {renderTextField('QTcF Value (ms)', 'qtcf_value', false, 'number')}
      {renderTextField('Largest Lymph Node Size (cm)', 'largest_lymph_node_size', false, 'number')}
      {renderTextField('Spleen Size (cm)', 'spleen_size', false, 'number')}

      <Grid item xs={12}>
        <Divider sx={{ my: 2 }} />
        <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>
          Clinical Findings
        </Typography>
      </Grid>
      {renderBooleanField('TP53 Disruption', 'tp53_disruption')}
      {renderBooleanField('Bone Marrow Involvement', 'bone_marrow_involvement')}
      {renderBooleanField('Measurable Disease (IWCLL)', 'measurable_disease_iwcll')}
      {renderBooleanField('Splenomegaly', 'splenomegaly')}
      {renderBooleanField('Hepatomegaly', 'hepatomegaly')}
      {renderBooleanField('Lymphadenopathy', 'lymphadenopathy')}
      {renderBooleanField('Autoimmune Cytopenias Refractory to Steroids', 'autoimmune_cytopenias_refractory_to_steroids')}
      {renderBooleanField('BTK Inhibitor Refractory', 'btk_inhibitor_refractory')}
      {renderBooleanField('BCL-2 Inhibitor Refractory', 'bcl2_inhibitor_refractory')}
    </Grid>
  );

  const renderDiseaseSpecificTab = () => {
    const diseaseType = getDiseaseType();
    switch (diseaseType) {
      case 'breast':
        return renderBreastCancerTab();
      case 'lymphoma':
        return renderLymphomaTab();
      case 'myeloma':
        return renderMyelomaTab();
      case 'cll':
        return renderCLLTab();
      default:
        return (
          <Grid container spacing={3}>
            {renderSelectField('Disease', 'disease', DISEASE_OPTIONS)}
            {renderSelectField('Stage', 'stage', STAGE_OPTIONS)}
            {renderSelectField('Histologic Type', 'histologic_type', histologicOptions.length ? histologicOptions.map(o => o.value) : HISTOLOGIC_TYPE_OPTIONS, true, false, histologicSource)}
            <Grid item xs={12}>
              <Typography variant="body2" color="text.secondary">
                Disease-specific fields are available for Breast Cancer, Follicular Lymphoma, Multiple Myeloma, and CLL.
              </Typography>
            </Grid>
          </Grid>
        );
    }
  };

  const age = editedInfo?.date_of_birth ? calculateAge(editedInfo.date_of_birth) : null;

  return (
    <Box p={3}>
      <Box display="flex" justifyContent="space-between" alignItems="center" mb={3}>
        <Box display="flex" alignItems="center">
          <Button
            startIcon={<ArrowLeft size={20} />}
            onClick={() => navigate('/')}
            sx={{ mr: 2 }}
          >
            Back to Patient List
          </Button>
          <Typography variant="h4">
            {patientName} - Patient ID: {personId}
          </Typography>
        </Box>
        <Button
          variant="contained"
          startIcon={<Save size={20} />}
          onClick={handleSave}
          disabled={saving}
        >
          {saving ? 'Saving...' : 'Save Changes'}
        </Button>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {error}
        </Alert>
      )}

      {successMessage && (
        <Alert severity="success" sx={{ mb: 2 }}>
          {successMessage}
        </Alert>
      )}

      <Paper>
        <Tabs value={activeTab} onChange={handleTabChange} sx={{ borderBottom: 1, borderColor: 'divider' }}>
          <Tab label="General" />
          <Tab label={getDiseaseTabLabel()} />
          <Tab label="Treatment" />
          <Tab label="Blood" />
          <Tab label="Labs" />
          <Tab label="Behavior" />
        </Tabs>

        <TabPanel value={activeTab} index={0}>
          <Grid container spacing={3}>
            <Grid item xs={12} md={6}>
              <TextField
                fullWidth
                label="Patient Name"
                value={editedName}
                onChange={(e) => setEditedName(e.target.value)}
                variant="outlined"
                size="small"
              />
            </Grid>
            {renderDateField('Date of Birth', 'date_of_birth')}
            <Grid item xs={12} md={6}>
              <TextField
                fullWidth
                label="Age"
                value={age || ''}
                variant="outlined"
                size="small"
                disabled
                helperText="Calculated from date of birth"
              />
            </Grid>
            {renderSelectField('Gender', 'gender', GENDER_OPTIONS)}
            {renderTextField('Email', 'email', true, 'email')}

            <Grid item xs={12}>
              <Divider sx={{ my: 2 }} />
              <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>
                Location
              </Typography>
            </Grid>
            
            {renderSelectField('Country', 'country', COUNTRY_OPTIONS)}
            <Grid item xs={12} sm={6}>
              <TextField
                fullWidth
                label="Postal Code / Zip Code"
                value={editedInfo.postal_code || ''}
                onChange={(e) => handleZipcodeChange(e.target.value)}
                variant="outlined"
                helperText="Enter 5-digit US zip code to auto-fill city and state"
              />
            </Grid>
            {renderTextField('City', 'city')}
            {editedInfo.country === 'United States' 
              ? renderSelectField('State', 'region', US_STATES)
              : renderTextField('Region/State', 'region')
            }
            
            <Grid item xs={12}>
              <Divider sx={{ my: 2 }} />
              <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>
                Ethnicity
              </Typography>
            </Grid>
            
            {renderSelectField('Ethnicity', 'ethnicity', ETHNICITY_OPTIONS, true, false, ethnicitySource)}

            <Grid item xs={12}>
              <Divider sx={{ my: 2 }} />
              <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>
                Clinical Summary
              </Typography>
            </Grid>

            {renderSelectField('Disease', 'disease', DISEASE_OPTIONS, false, false, diseaseSource)}
            {renderSelectField('Stage', 'stage', STAGE_OPTIONS, false, false, cancerStageSource)}
            {renderSelectField('Histologic Type', 'histologic_type', histologicOptions.length ? histologicOptions.map(o => o.value) : HISTOLOGIC_TYPE_OPTIONS, true, false, histologicSource)}
            {renderSelectField('ECOG Performance Status', 'ecog_performance_status', ECOG_OPTIONS, false, false, ecogSource)}
            {renderDateField('ECOG Assessment Date', 'ecog_assessment_date')}
            {renderSelectField('Karnofsky Performance Score', 'karnofsky_performance_score', KARNOFSKY_OPTIONS, false, false, karnofskySource)}
            
            <Grid item xs={12}>
              <Divider sx={{ my: 2 }} />
              <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>
                Physical Measurements
              </Typography>
            </Grid>
            
            {renderTextField('Weight (kg)', 'weight', false, 'number')}
            {renderTextField('Height (cm)', 'height', false, 'number')}
            {renderTextField('BMI', 'bmi', false, 'number')}
            {renderTextField('Systolic Blood Pressure (mmHg)', 'systolic_blood_pressure', false, 'number')}
            {renderTextField('Diastolic Blood Pressure (mmHg)', 'diastolic_blood_pressure', false, 'number')}
            {renderTextField('Heart Rate (bpm)', 'heartrate', false, 'number')}
          </Grid>
        </TabPanel>

        <TabPanel value={activeTab} index={1}>
          {renderDiseaseSpecificTab()}
        </TabPanel>

        <TabPanel value={activeTab} index={2}>
          <Typography variant="h6" gutterBottom>Treatment History</Typography>
          <Grid container spacing={3}>
            {renderSelectField('Prior Therapy', 'prior_therapy', YES_NO_OPTIONS, false, true)}
            {renderTextField('Number of Prior Lines', 'therapy_lines_count', false, 'number', true)}
            {renderTextField('Relapse Count', 'relapse_count', false, 'number', false)}
            {renderSelectField('Refractory Status', 'refractory_status', REFRACTORY_STATUS_OPTIONS, true, true)}
            
            <Grid item xs={12}>
              <Divider sx={{ my: 2 }} />
              <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>
                First Line Therapy
              </Typography>
            </Grid>
            {renderSelectField('First Line Therapy', 'first_line_therapy', getTherapyOptions('first'), true, false, getDiseaseType() === 'breast' ? bcFirstLineSource : null)}
            {renderDateField('First Line Start Date', 'first_line_start_date')}
            {renderDateField('First Line End Date', 'first_line_end_date')}
            {renderSelectField('Therapy Intent', 'first_line_intent', THERAPY_INTENT_OPTIONS)}
            {renderSelectField('Reason for Discontinuation', 'first_line_discontinuation_reason', DISCONTINUATION_REASON_OPTIONS)}
            {renderSelectField('First Line Outcome', 'first_line_outcome', THERAPY_OUTCOME_OPTIONS)}

            <Grid item xs={12}>
              <Divider sx={{ my: 2 }} />
              <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>
                Second Line Therapy
              </Typography>
            </Grid>
            {renderSelectField('Second Line Therapy', 'second_line_therapy', getTherapyOptions('second'), true, false, getDiseaseType() === 'breast' ? bcSecondLineSource : null)}
            {renderDateField('Second Line Start Date', 'second_line_start_date')}
            {renderDateField('Second Line End Date', 'second_line_end_date')}
            {renderSelectField('Therapy Intent', 'second_line_intent', THERAPY_INTENT_OPTIONS)}
            {renderSelectField('Reason for Discontinuation', 'second_line_discontinuation_reason', DISCONTINUATION_REASON_OPTIONS)}
            {renderSelectField('Second Line Outcome', 'second_line_outcome', THERAPY_OUTCOME_OPTIONS)}

            <Grid item xs={12}>
              <Divider sx={{ my: 2 }} />
              <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>
                Later Line Therapy
              </Typography>
            </Grid>
            {renderSelectField('Later Line Therapy', 'later_therapy', getTherapyOptions('later'), true, false, getDiseaseType() === 'breast' ? bcLaterLineSource : null)}
            {renderDateField('Later Line Start Date', 'later_start_date')}
            {renderDateField('Later Line End Date', 'later_end_date')}
            {renderSelectField('Therapy Intent', 'later_intent', THERAPY_INTENT_OPTIONS)}
            {renderSelectField('Reason for Discontinuation', 'later_discontinuation_reason', DISCONTINUATION_REASON_OPTIONS)}
            {renderSelectField('Later Line Outcome', 'later_outcome', THERAPY_OUTCOME_OPTIONS)}
            
            <Grid item xs={12}>
              <Divider sx={{ my: 2 }} />
              <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>
                Supportive Therapy
              </Typography>
            </Grid>
            {renderDateField('Supportive Therapy Start Date', 'supportive_therapy_start_date')}
            {renderDateField('Supportive Therapy End Date', 'supportive_therapy_end_date')}
            {renderMultiSelectField('Supportive Therapies', 'supportive_therapies', SUPPORTIVE_THERAPIES_OPTIONS, false)}
            {renderSelectField('Supportive Therapy Intent', 'supportive_therapy_intent', THERAPY_INTENT_OPTIONS)}
            
            <Grid item xs={12}>
              <Divider sx={{ my: 2 }} />
              <Typography variant="h6" gutterBottom sx={{ mt: 2 }}>
                Planned Therapies
              </Typography>
            </Grid>
            {renderSelectField('Planned Therapies', 'planned_therapies', PLANNED_THERAPIES, true)}
          </Grid>
        </TabPanel>

        <TabPanel value={activeTab} index={3}>
          <Typography variant="h6" gutterBottom>Blood Counts</Typography>
          <Grid container spacing={3}>
            {renderTextField('Hemoglobin (g/dL)', 'hemoglobin_g_dl', false, 'number')}
            {renderTextField('Hematocrit (%)', 'hematocrit_percent', false, 'number')}
            {renderTextField('WBC Count (10³/µL)', 'wbc_count_thousand_per_ul', false, 'number')}
            {renderTextField('RBC Count (10⁶/µL)', 'rbc_million_per_ul', false, 'number')}
            {renderTextField('Platelet Count (10³/µL)', 'platelet_count_thousand_per_ul', false, 'number')}
            {renderTextField('ANC (10³/µL)', 'anc_thousand_per_ul', false, 'number')}
            {renderTextField('ALC (10³/µL)', 'alc_thousand_per_ul', false, 'number')}
            {renderTextField('AMC (10³/µL)', 'amc_thousand_per_ul', false, 'number')}
          </Grid>
          
          <Typography variant="h6" gutterBottom sx={{ mt: 3 }}>Electrolytes</Typography>
          <Grid container spacing={3}>
            {renderTextField('Sodium (mEq/L)', 'sodium_meq_l', false, 'number')}
            {renderTextField('Potassium (mEq/L)', 'potassium_meq_l', false, 'number')}
            {renderTextField('Calcium (mg/dL)', 'calcium_mg_dl', false, 'number')}
            {renderTextField('Magnesium (mg/dL)', 'magnesium_mg_dl', false, 'number')}
          </Grid>
          
          <Typography variant="h6" gutterBottom sx={{ mt: 3 }}>Cardiac & Other</Typography>
          <Grid container spacing={3}>
            {renderTextField('Troponin (ng/mL)', 'troponin_ng_ml', false, 'number')}
            {renderTextField('BNP (pg/mL)', 'bnp_pg_ml', false, 'number')}
            {renderTextField('Glucose (mg/dL)', 'glucose_mg_dl', false, 'number')}
            {renderTextField('HbA1c (%)', 'hba1c_percent', false, 'number')}
            {renderTextField('LDH (U/L)', 'ldh_u_l', false, 'number')}
          </Grid>
          
          <Typography variant="h6" gutterBottom sx={{ mt: 3 }}>Coagulation</Typography>
          <Grid container spacing={3}>
            {renderTextField('INR', 'inr', false, 'number')}
            {renderTextField('PT (seconds)', 'pt_seconds', false, 'number')}
            {renderTextField('PTT (seconds)', 'ptt_seconds', false, 'number')}
          </Grid>
          
          <Typography variant="h6" gutterBottom sx={{ mt: 3 }}>Tumor Markers</Typography>
          <Grid container spacing={3}>
            {renderTextField('CEA (ng/mL)', 'cea_ng_ml', false, 'number')}
            {renderTextField('CA 19-9 (U/mL)', 'ca19_9_u_ml', false, 'number')}
            {renderTextField('PSA (ng/mL)', 'psa_ng_ml', false, 'number')}
          </Grid>
        </TabPanel>

        <TabPanel value={activeTab} index={4}>
          <Typography variant="h6" gutterBottom>Laboratory Values</Typography>
          <Grid container spacing={3}>
            <Grid item xs={12}>
              <Typography variant="subtitle1" fontWeight="bold" gutterBottom sx={{ mt: 1 }}>
                Chemistry Panel
              </Typography>
            </Grid>
            {renderTextField('Serum Creatinine (mg/dL)', 'serum_creatinine_level', false, 'number')}
            {renderTextField('Creatinine Clearance Rate', 'creatinine_clearance_rate', false, 'number')}
            {renderTextField('Blood Urea Nitrogen (mg/dL)', 'blood_urea_nitrogen', false, 'number')}
            {renderTextField('eGFR (mL/min/1.73m²)', 'egfr', false, 'number')}
            {renderTextField('Serum Sodium (mEq/L)', 'serum_sodium', false, 'number')}
            {renderTextField('Serum Potassium (mEq/L)', 'serum_potassium', false, 'number')}
            {renderTextField('Serum Calcium (mg/dL)', 'serum_calcium_level', false, 'number')}
            {renderTextField('Magnesium (mg/dL)', 'magnesium', false, 'number')}
            {renderTextField('Phosphorus (mg/dL)', 'phosphorus', false, 'number')}
            {renderTextField('Serum Albumin (g/dL)', 'albumin_level', false, 'number')}
            {renderTextField('Total Protein (g/dL)', 'total_protein', false, 'number')}
            
            <Grid item xs={12}>
              <Typography variant="subtitle1" fontWeight="bold" gutterBottom sx={{ mt: 2 }}>
                Liver Function Tests
              </Typography>
            </Grid>
            {renderTextField('AST (U/L)', 'liver_enzyme_levels_ast', false, 'number')}
            {renderTextField('ALT (U/L)', 'liver_enzyme_levels_alt', false, 'number')}
            {renderTextField('ALP (U/L)', 'liver_enzyme_levels_alp', false, 'number')}
            {renderTextField('Total Bilirubin (mg/dL)', 'serum_bilirubin_level_total', false, 'number')}
            {renderTextField('Direct Bilirubin (mg/dL)', 'serum_bilirubin_level_direct', false, 'number')}
            {renderTextField('Albumin (g/dL)', 'albumin_g_dl', false, 'number')}
            
            <Grid item xs={12}>
              <Typography variant="subtitle1" fontWeight="bold" gutterBottom sx={{ mt: 2 }}>
                Other Markers
              </Typography>
            </Grid>
            {renderTextField('LDH (U/L)', 'ldh', false, 'number')}
            {renderTextField('Alkaline Phosphatase (U/L)', 'alkaline_phosphatase', false, 'number')}
            {renderTextField('Beta-2 Microglobulin (mg/L)', 'beta2_microglobulin', false, 'number')}
            {renderTextField('C-Reactive Protein (mg/L)', 'c_reactive_protein', false, 'number')}
            {renderTextField('ESR (mm/hr)', 'esr', false, 'number')}
          </Grid>
        </TabPanel>

        <TabPanel value={activeTab} index={5}>
          <Typography variant="h6" gutterBottom>Lifestyle & Behavior</Typography>
          <Grid container spacing={3}>
            <Grid item xs={12}>
              <Divider sx={{ my: 2 }} />
              <Typography variant="subtitle1" fontWeight="bold" gutterBottom sx={{ mt: 2 }}>
                Lifestyle Factors
              </Typography>
            </Grid>
            {renderSelectField('Smoking Status', 'smoking_status', SMOKING_STATUS_OPTIONS)}
            {renderTextField('Pack Years (if applicable)', 'pack_years', false, 'number')}
            {renderSelectField('Alcohol Use', 'alcohol_use', ALCOHOL_USE_OPTIONS)}
            {renderTextField('Drinks per Week (if applicable)', 'drinks_per_week', false, 'number')}
            {renderSelectField('Exercise Frequency', 'exercise_frequency', EXERCISE_FREQUENCY_OPTIONS)}
            {renderTextField('Exercise Minutes per Week', 'exercise_minutes_per_week', false, 'number')}
            {renderSelectField('Diet Type', 'diet_type', DIET_TYPE_OPTIONS)}
            
            <Grid item xs={12}>
              <Divider sx={{ my: 2 }} />
              <Typography variant="subtitle1" fontWeight="bold" gutterBottom sx={{ mt: 2 }}>
                Sleep & Wellbeing
              </Typography>
            </Grid>
            {renderTextField('Average Sleep Hours per Night', 'sleep_hours_per_night', false, 'number')}
            {renderSelectField('Sleep Quality', 'sleep_quality', SLEEP_QUALITY_OPTIONS)}
            {renderSelectField('Stress Level', 'stress_level', STRESS_LEVEL_OPTIONS)}
            {renderSelectField('Social Support', 'social_support', SOCIAL_SUPPORT_OPTIONS)}
            
            <Grid item xs={12}>
              <Divider sx={{ my: 2 }} />
              <Typography variant="subtitle1" fontWeight="bold" gutterBottom sx={{ mt: 2 }}>
                Socioeconomic Factors
              </Typography>
            </Grid>
            {renderSelectField('Employment Status', 'employment_status', EMPLOYMENT_STATUS_OPTIONS)}
            {renderSelectField('Education Level', 'education_level', EDUCATION_LEVEL_OPTIONS)}
            {renderSelectField('Marital Status', 'marital_status', MARITAL_STATUS_OPTIONS)}
            {renderSelectField('Insurance Type', 'insurance_type', INSURANCE_TYPE_OPTIONS)}
            {renderTextField('Number of Dependents', 'number_of_dependents', false, 'number')}
            {renderTextField('Annual Household Income (USD)', 'annual_household_income', false, 'number')}
            
            <Grid item xs={12}>
              <Divider sx={{ my: 2 }} />
              <Typography variant="subtitle1" fontWeight="bold" gutterBottom sx={{ mt: 2 }}>
                Reproductive Health
              </Typography>
            </Grid>
            {renderDateField('Pregnancy Test Date', 'pregnancy_test_date')}
            {renderTextField('Pregnancy Test Result', 'pregnancy_test_result_value', true)}
            {renderBooleanField('Using Contraceptives', 'contraceptive_use')}
            
            <Grid item xs={12}>
              <Divider sx={{ my: 2 }} />
              <Typography variant="subtitle1" fontWeight="bold" gutterBottom sx={{ mt: 2 }}>
                Consent and Care Support
              </Typography>
            </Grid>
            {renderBooleanField('Ability to Consent', 'consent_capability')}
            {renderBooleanField('Availability of Caregiver', 'caregiver_availability_status')}
            
            <Grid item xs={12}>
              <Divider sx={{ my: 2 }} />
              <Typography variant="subtitle1" fontWeight="bold" gutterBottom sx={{ mt: 2 }}>
                Mental Health and Substance Use
              </Typography>
            </Grid>
            {renderBooleanField('Mental Health Disorders', 'no_mental_health_disorder_status')}
            {renderBooleanField('Non-prescription Recreational Drug Use', 'no_substance_use_status')}
            {renderTextField('Substance Use Details', 'substance_use_details', true)}
            
            <Grid item xs={12}>
              <Divider sx={{ my: 2 }} />
              <Typography variant="subtitle1" fontWeight="bold" gutterBottom sx={{ mt: 2 }}>
                Environmental and Occupational Risk
              </Typography>
            </Grid>
            {renderBooleanField('Geographic/Occupational/Environmental/Infectious Disease Exposure Risk', 'no_geographic_exposure_risk')}
            {renderTextField('Exposure Risk Details', 'geographic_exposure_risk_details', true)}
          </Grid>
        </TabPanel>
      </Paper>
    </Box>
  );
};

export default PatientDetail;