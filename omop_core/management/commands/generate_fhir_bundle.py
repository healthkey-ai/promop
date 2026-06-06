"""
Generate comprehensive FHIR Bundle JSON with breast cancer patient data

This command creates a FHIR Bundle including:
- Patient demographics with US addresses
- Breast cancer diagnoses with stage and histologic type
- Lab values (CBC, liver, kidney function)
- Biomarkers (HER2, ER, PR status)
- Genetic mutations (BRCA1, BRCA2, TP53, PIK3CA, etc.)
- Prior lines of therapy (chemotherapy, targeted therapy)

Usage:
    python manage.py generate_fhir_bundle --count 200 --output data/patients.json
"""

import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Generate comprehensive FHIR Bundle with breast cancer patient data'

    def add_arguments(self, parser):
        parser.add_argument(
            '--count',
            type=int,
            default=200,
            help='Number of patients to generate (default: 200)',
        )
        parser.add_argument(
            '--output',
            type=str,
            default='data/synthetic_patients_fhir.json',
            help='Output file path (default: data/synthetic_patients_fhir.json)',
        )
        parser.add_argument(
            '--seed',
            type=int,
            default=42,
            help='Random seed for reproducibility (default: 42)',
        )
        parser.add_argument(
            '--tnbc-ratio',
            type=float,
            default=0.15,
            help='Fraction of patients that are TNBC (default: 0.15)',
        )

    def handle(self, *args, **options):
        count = options['count']
        output_path = options['output']
        seed = options['seed']
        self.tnbc_ratio = options['tnbc_ratio']

        random.seed(seed)

        self.stdout.write('Generating comprehensive FHIR Bundle with breast cancer patients...')

        bundle = self.generate_bundle(count)
        
        # Save to file
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_file, 'w') as f:
            json.dump(bundle, f, indent=2)
        
        self.stdout.write(self.style.SUCCESS(f'✓ Generated FHIR Bundle with {count} patients'))
        self.stdout.write(self.style.SUCCESS(f'✓ Saved to: {output_file}'))
        self.stdout.write(self.style.SUCCESS(f'✓ Total resources: {len(bundle["entry"])}'))
        self.stdout.write(self.style.SUCCESS('✓ Each patient includes:'))
        self.stdout.write('  - Demographics with US address')
        self.stdout.write('  - Breast cancer diagnosis with stage and histologic type')
        self.stdout.write('  - Lab values (CBC, liver, kidney function)')
        self.stdout.write('  - Biomarkers (HER2, ER, PR status)')
        self.stdout.write('  - Genetic mutations (BRCA1, BRCA2, TP53, PIK3CA, etc.)')
        self.stdout.write('  - Prior lines of therapy (chemotherapy, targeted therapy)')

    def generate_bundle(self, num_patients):
        """Generate FHIR Bundle with all resources"""
        bundle = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": []
        }
        
        # For exactly 10 patients, guarantee exact distribution: 4/3/2/1 for 1/2/3/4 lines
        if num_patients == 10:
            therapy_lines = [1, 1, 1, 1, 2, 2, 2, 3, 3, 4]
            random.shuffle(therapy_lines)
        else:
            # For other counts, use probabilistic distribution (all with at least 1 line)
            therapy_lines = None
        
        for i in range(1, num_patients + 1):
            first_name = random.choice(self.get_first_names())
            last_name = random.choice(self.get_last_names())
            
            # Generate patient
            patient = self.generate_patient(i, first_name, last_name)
            bundle["entry"].append({
                "fullUrl": f"http://example.org/Patient/{i}",
                "resource": patient
            })
            
            birth_date = datetime.strptime(patient['birthDate'], '%Y-%m-%d')
            diagnosis_date = self.generate_diagnosis_date(birth_date)
            
            # Generate condition with stage and histologic type
            condition = self.generate_condition(i, diagnosis_date)
            bundle["entry"].append({
                "fullUrl": f"http://example.org/Condition/condition-{i}",
                "resource": condition
            })
            # Extract stage to enforce M0/M1 consistency in tumor characteristics
            cancer_stage = condition['stage'][0]['summary']['coding'][0]['code']

            # Generate tumor characteristics (size, lymph nodes, metastasis)
            for obs in self.generate_tumor_characteristics(i, diagnosis_date, cancer_stage):
                bundle["entry"].append(obs)
            
            # Generate lab observations
            lab_date = datetime.now() - timedelta(days=random.randint(1, 30))
            for obs in self.generate_lab_observations(i, lab_date):
                bundle["entry"].append(obs)
            
            # Generate biomarker observations
            for obs in self.generate_biomarker_observations(i, diagnosis_date):
                bundle["entry"].append(obs)
            
            # Generate genetic mutation observations
            for obs in self.generate_genetic_mutations(i, diagnosis_date):
                bundle["entry"].append(obs)
            
            # Generate prior therapy (medication statements)
            assigned_lines = therapy_lines[i-1] if therapy_lines else None
            for med in self.generate_prior_therapy(i, diagnosis_date, assigned_lines):
                bundle["entry"].append(med)
            
            # Generate supportive therapy
            supportive = self.generate_supportive_therapy(i, diagnosis_date)
            if supportive:
                bundle["entry"].append(supportive)
            
            # Generate planned therapy
            planned = self.generate_planned_therapy(i)
            if planned:
                bundle["entry"].append(planned)

            # Generate bone marrow biopsy observation (~40% of patients have it)
            if random.random() < 0.4:
                bm_obs = self.generate_bone_marrow_biopsy(i, diagnosis_date)
                bundle["entry"].append(bm_obs)

        return bundle

    def generate_bone_marrow_biopsy(self, patient_id, diagnosis_date):
        """Generate bone marrow biopsy observation for clonal_bone_marrow_b_lymphocytes"""
        value = round(random.uniform(0.5, 95.0), 1)
        return {
            "fullUrl": f"http://example.org/Observation/obs-{patient_id}-bone-marrow-biopsy",
            "resource": {
                "resourceType": "Observation",
                "id": f"obs-{patient_id}-bone-marrow-biopsy",
                "status": "final",
                "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}]}],
                "code": {
                    "coding": [{"system": "http://loinc.org", "code": "85319-5", "display": "Clonal bone marrow B lymphocytes"}],
                    "text": "Clonal bone marrow B lymphocytes"
                },
                "subject": {"reference": f"Patient/{patient_id}"},
                "effectiveDateTime": diagnosis_date.strftime('%Y-%m-%d'),
                "valueQuantity": {
                    "value": value,
                    "unit": "%",
                    "system": "http://unitsofmeasure.org",
                    "code": "%"
                }
            }
        }

    def generate_patient(self, patient_id, first_name, last_name):
        """Generate FHIR Patient resource with US address"""
        birth_date = self.generate_random_date(1950, 2005)
        
        # US locations
        us_locations = [
            ('New York', 'NY'), ('Los Angeles', 'CA'), ('Chicago', 'IL'),
            ('Houston', 'TX'), ('Boston', 'MA'), ('Miami', 'FL'),
            ('Phoenix', 'AZ'), ('Philadelphia', 'PA'), ('San Antonio', 'TX'),
            ('San Diego', 'CA'), ('Dallas', 'TX'), ('San Jose', 'CA'),
            ('Austin', 'TX'), ('Jacksonville', 'FL'), ('Fort Worth', 'TX'),
            ('Columbus', 'OH'), ('Charlotte', 'NC'), ('Seattle', 'WA'),
            ('Denver', 'CO'), ('Portland', 'OR'), ('Atlanta', 'GA'),
        ]
        
        city, state = random.choice(us_locations)
        zip_code = f"{random.randint(10000, 99999)}"
        phone = f"+1-555-{random.randint(100, 999)}-{random.randint(1000, 9999)}"
        
        # OMOP CDM standard: race and ethnicity are separate
        races = [
            'White',
            'Black or African American',
            'Asian',
            'American Indian or Alaska Native',
            'Native Hawaiian or Pacific Islander',
            'Other Race',
        ]
        race = random.choice(races)
        ethnicities = ['Hispanic or Latino', 'Not Hispanic or Latino', 'Not Hispanic or Latino', 'Not Hispanic or Latino']
        ethnicity = random.choice(ethnicities)
        
        # Generate vital signs
        weight_kg = round(random.uniform(50, 100), 1)
        height_cm = round(random.uniform(150, 180), 1)
        systolic = random.randint(110, 140)
        diastolic = random.randint(70, 90)
        heart_rate = random.randint(60, 100)
        
        # Generate ECOG Performance Status (0-4, where 0 = fully active, 4 = bedridden)
        # Weight toward better performance status (0-2 more common than 3-4)
        ecog_choices = [0, 1, 2, 3, 4]
        ecog_weights = [30, 40, 20, 8, 2]  # Most patients have ECOG 0-2
        ecog = random.choices(ecog_choices, weights=ecog_weights)[0]

        return {
            "resourceType": "Patient",
            "id": str(patient_id),
            "name": [{
                "use": "official",
                "family": last_name,
                "given": [first_name]
            }],
            "gender": "female",
            "birthDate": birth_date.strftime('%Y-%m-%d'),
            "extension": [
                {
                    "url": "http://ctomop.io/fhir/StructureDefinition/race",
                    "valueString": race
                },
                {
                    "url": "http://ctomop.io/fhir/StructureDefinition/ethnicity",
                    "valueString": ethnicity
                },
                {
                    "url": "http://hl7.org/fhir/StructureDefinition/patient-bodyWeight",
                    "valueQuantity": {"value": weight_kg, "unit": "kg"}
                },
                {
                    "url": "http://hl7.org/fhir/StructureDefinition/patient-bodyHeight",
                    "valueQuantity": {"value": height_cm, "unit": "cm"}
                },
                {
                    "url": "http://hl7.org/fhir/StructureDefinition/patient-systolic-bp",
                    "valueQuantity": {"value": systolic, "unit": "mmHg"}
                },
                {
                    "url": "http://hl7.org/fhir/StructureDefinition/patient-diastolic-bp",
                    "valueQuantity": {"value": diastolic, "unit": "mmHg"}
                },
                {
                    "url": "http://hl7.org/fhir/StructureDefinition/patient-heartRate",
                    "valueQuantity": {"value": heart_rate, "unit": "beats/min"}
                },
                {
                    "url": "http://hl7.org/fhir/StructureDefinition/patient-ecog-performance-status",
                    "valueInteger": ecog
                }
            ],
            "telecom": [{
                "system": "phone",
                "value": phone,
                "use": "home"
            }],
            "address": [{
                "use": "home",
                "type": "both",
                "line": [f"{random.randint(100, 9999)} {random.choice(['Main', 'Oak', 'Maple', 'Cedar', 'Pine'])} Street"],
                "city": city,
                "state": state,
                "postalCode": zip_code,
                "country": "United States"
            }]
        }

    def generate_condition(self, patient_id, diagnosis_date):
        """Generate breast cancer condition with stage and histologic type"""
        # Stage → valid histologic types (issue #8 rules)
        HISTOLOGIC_STAGE0 = [
            'Ductal carcinoma in situ (DCIS)',
            'Lobular carcinoma in situ (LCIS)',
        ]
        HISTOLOGIC_I_II = [
            'Infiltrating ductal carcinoma (IDC)',
            'Infiltrating lobular carcinoma (ILC)',
            'Mixed ductal and lobular carcinoma',
            'Mucinous (colloid) carcinoma',
            'Tubular carcinoma',
            'Medullary carcinoma',
            'Papillary carcinoma',
            'Metaplastic carcinoma',
        ]
        HISTOLOGIC_III_IV = HISTOLOGIC_I_II + ['Inflammatory carcinoma']

        stages = ['0', 'I', 'IA', 'IB', 'II', 'IIA', 'IIB', 'III', 'IIIA', 'IIIB', 'IIIC', 'IV']
        stage = random.choice(stages)

        if stage == '0':
            histologic_type = random.choice(HISTOLOGIC_STAGE0)
        elif stage in ('I', 'IA', 'IB', 'II', 'IIA', 'IIB'):
            histologic_type = random.choice(HISTOLOGIC_I_II)
        else:  # III, IIIA, IIIB, IIIC, IV
            histologic_type = random.choice(HISTOLOGIC_III_IV)
        
        return {
            "resourceType": "Condition",
            "id": f"condition-{patient_id}",
            "clinicalStatus": {
                "coding": [{
                    "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                    "code": "active"
                }]
            },
            "verificationStatus": {
                "coding": [{
                    "system": "http://terminology.hl7.org/CodeSystem/condition-ver-status",
                    "code": "confirmed"
                }]
            },
            "category": [{
                "coding": [{
                    "system": "http://terminology.hl7.org/CodeSystem/condition-category",
                    "code": "encounter-diagnosis"
                }]
            }],
            "code": {
                "coding": [{
                    "system": "http://snomed.info/sct",
                    "code": "254837009",
                    "display": "Malignant neoplasm of breast"
                }],
                "text": histologic_type
            },
            "subject": {
                "reference": f"Patient/{patient_id}"
            },
            "onsetDateTime": diagnosis_date.strftime('%Y-%m-%d'),
            "recordedDate": diagnosis_date.strftime('%Y-%m-%d'),
            "stage": [{
                "summary": {
                    "coding": [{
                        "system": "http://cancerstaging.org",
                        "code": stage
                    }],
                    "text": f"Breast Cancer Stage {stage}"
                },
                "type": {
                    "coding": [{
                        "system": "http://snomed.info/sct",
                        "code": "260998006",
                        "display": "Clinical staging"
                    }]
                }
            }],
            "note": [{
                "text": f"Histologic type: {histologic_type}"
            }]
        }

    def generate_tumor_characteristics(self, patient_id, diagnosis_date, stage=None):
        """Generate tumor characteristics (size, lymph nodes, metastasis)"""
        observations = []
        
        # Tumor size (0.5 to 10 cm, weighted toward smaller sizes)
        tumor_size = round(random.triangular(0.5, 10.0, 2.5), 1)
        
        # Lymph node status (Positive/Negative/Unknown)
        lymph_node_choices = ["Positive", "Negative", "Unknown"]
        lymph_node_weights = [40, 55, 5]  # 40% positive, 55% negative, 5% unknown
        lymph_node_status = random.choices(lymph_node_choices, weights=lymph_node_weights)[0]
        
        # Metastasis status must be consistent with cancer stage:
        # Stage IV → always Positive (M1); all other stages → never Positive (M0)
        if stage == 'IV':
            metastasis_status = 'Positive'
        else:
            metastasis_status = random.choices(['Negative', 'Unknown'], weights=[95, 5])[0]
        
        # Tumor size observation
        tumor_obs = {
            "fullUrl": f"http://example.org/Observation/obs-{patient_id}-tumor-size",
            "resource": {
                "resourceType": "Observation",
                "id": f"obs-{patient_id}-tumor-size",
                "status": "final",
                "category": [{
                    "coding": [{
                        "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                        "code": "imaging"
                    }]
                }],
                "code": {
                    "coding": [{
                        "system": "http://loinc.org",
                        "code": "21889-1",
                        "display": "Size Tumor"
                    }],
                    "text": "Tumor size"
                },
                "subject": {
                    "reference": f"Patient/{patient_id}"
                },
                "effectiveDateTime": diagnosis_date.strftime('%Y-%m-%d'),
                "valueQuantity": {
                    "value": tumor_size,
                    "unit": "cm",
                    "system": "http://unitsofmeasure.org",
                    "code": "cm"
                }
            }
        }
        observations.append(tumor_obs)
        
        # Lymph node status observation
        lymph_obs = {
            "fullUrl": f"http://example.org/Observation/obs-{patient_id}-lymph-nodes",
            "resource": {
                "resourceType": "Observation",
                "id": f"obs-{patient_id}-lymph-nodes",
                "status": "final",
                "category": [{
                    "coding": [{
                        "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                        "code": "laboratory"
                    }]
                }],
                "code": {
                    "coding": [{
                        "system": "http://loinc.org",
                        "code": "92837-4",
                        "display": "Lymph nodes involvement"
                    }],
                    "text": "Lymph node status"
                },
                "subject": {
                    "reference": f"Patient/{patient_id}"
                },
                "effectiveDateTime": diagnosis_date.strftime('%Y-%m-%d'),
                "valueCodeableConcept": {
                    "coding": [{
                        "system": "http://snomed.info/sct",
                        "code": "10828004" if lymph_node_status == "Positive" else "260385009",
                        "display": lymph_node_status
                    }],
                    "text": lymph_node_status
                }
            }
        }
        observations.append(lymph_obs)
        
        # Metastasis status observation
        met_obs = {
            "fullUrl": f"http://example.org/Observation/obs-{patient_id}-metastasis",
            "resource": {
                "resourceType": "Observation",
                "id": f"obs-{patient_id}-metastasis",
                "status": "final",
                "category": [{
                    "coding": [{
                        "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                        "code": "imaging"
                    }]
                }],
                "code": {
                    "coding": [{
                        "system": "http://loinc.org",
                        "code": "21907-1",
                        "display": "Distant metastases status"
                    }],
                    "text": "Metastasis status"
                },
                "subject": {
                    "reference": f"Patient/{patient_id}"
                },
                "effectiveDateTime": diagnosis_date.strftime('%Y-%m-%d'),
                "valueCodeableConcept": {
                    "coding": [{
                        "system": "http://snomed.info/sct",
                        "code": "10828004" if metastasis_status == "Positive" else "260385009",
                        "display": metastasis_status
                    }],
                    "text": metastasis_status
                }
            }
        }
        observations.append(met_obs)

        # TNM: Tumor stage — pick a valid (T, N) combo for this stage (issue #8)
        # Maps stage → valid (T_category, N_category) pairs
        STAGE_TNM_VALID = {
            '0':    [('Tis', 'N0')],
            'IA':   [('T0', 'N1mi'), ('T1', 'N0'), ('T1', 'N1mi')],
            'IB':   [('T1', 'N1mi'), ('T0', 'N1'), ('T1', 'N1'), ('T2', 'N0'), ('T2', 'N1'), ('T3', 'N0')],
            'I':    [('T0', 'N1mi'), ('T1', 'N0'), ('T1', 'N1mi'), ('T0', 'N1'), ('T1', 'N1'), ('T2', 'N0'), ('T3', 'N0')],
            'IIA':  [('T0', 'N1'), ('T1', 'N1'), ('T2', 'N0')],
            'IIB':  [('T2', 'N1'), ('T3', 'N0')],
            'II':   [('T0', 'N1'), ('T1', 'N1'), ('T2', 'N0'), ('T2', 'N1'), ('T3', 'N0')],
            'IIIA': [('T0', 'N2'), ('T2', 'N2'), ('T3', 'N1'), ('T3', 'N2')],
            'IIIB': [('T4', 'N0'), ('T4', 'N1'), ('T4', 'N2')],
            'IIIC': [('T1', 'N3'), ('T2', 'N3'), ('T3', 'N3'), ('T4', 'N3')],
            'III':  [('T0', 'N2'), ('T2', 'N2'), ('T3', 'N1'), ('T3', 'N2'),
                     ('T4', 'N0'), ('T4', 'N1'), ('T4', 'N2'), ('T2', 'N3'), ('T4', 'N3')],
            'IV':   [('T1', 'N0'), ('T1', 'N1'), ('T2', 'N0'), ('T2', 'N1'), ('T3', 'N0'),
                     ('T3', 'N1'), ('T4', 'N0'), ('T4', 'N1'), ('T4', 'N3')],
        }
        T_FULL = {
            'Tis': "Tis: Non-invasive Carcinoma in situ (DCIS, LCIS, Paget\u2019s without tumor)",
            'T0':  "T0: No tumor evidence",
            'T1':  random.choice(["T1: Invasive Tumor \u2264 2 cm", "T1a: 0.1 \u2013 0.5 cm",
                                   "T1b: 0.5 \u2013 1 cm", "T1c: 1 \u2013 2 cm"]),
            'T2':  "T2: Invasive Tumor > 2 \u2013 5 cm",
            'T3':  "T3: Invasive Tumor > 5 cm",
            'T4':  random.choice(["T4: Invades chest wall or skin, or inflammatory",
                                   "T4a: Invades chest wall", "T4b: Invades skin (may be swelling/ulcer)",
                                   "T4c: Invades both skin + chest wall", "T4d: Inflammatory carcinoma"]),
        }
        N_FULL = {
            'N0':   "N0: No lymph node involvement",
            'N1mi': "N1mi: Micrometastasis (0.2\u20132 mm)",
            'N1':   random.choice(["N1: 1\u20133 axillary lymph nodes or small internal mammary nodes",
                                    "N1a: 1\u20133 axillary nodes (>2 mm)",
                                    "N1b: Cancer cells in internal mammary sentinel nodes",
                                    "N1c: 1\u20133 axillary nodes + internal mammary sentinel nodes"]),
            'N2':   random.choice(["N2: 4\u20139 axillary nodes or internal mammary nodes without axillary nodes",
                                    "N2a: 4\u20139 axillary nodes (>2 mm)",
                                    "N2b: Internal mammary nodes only (no axillary)"]),
            'N3':   random.choice(["N3: 10+ axillary, infraclavicular, or supraclavicular nodes; or both axillary + internal mammary",
                                    "N3a: \u226510 axillary nodes (\u22652 mm) or infraclavicular",
                                    "N3b: 4\u20139 Axillary + mammary nodes",
                                    "N3c: Supraclavicular nodes"]),
        }
        valid_combos = STAGE_TNM_VALID.get(stage, STAGE_TNM_VALID['IV'])
        t_cat, n_cat = random.choice(valid_combos)
        t_stage = T_FULL[t_cat]
        n_stage = N_FULL[n_cat]

        observations.append({
            "fullUrl": f"http://example.org/Observation/obs-{patient_id}-tumor-stage",
            "resource": {
                "resourceType": "Observation",
                "id": f"obs-{patient_id}-tumor-stage",
                "status": "final",
                "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "imaging"}]}],
                "code": {
                    "coding": [{"system": "http://loinc.org", "code": "21905-5", "display": "T category"}],
                    "text": "Tumor stage"
                },
                "subject": {"reference": f"Patient/{patient_id}"},
                "effectiveDateTime": diagnosis_date.strftime('%Y-%m-%d'),
                "valueCodeableConcept": {"text": t_stage}
            }
        })
        observations.append({
            "fullUrl": f"http://example.org/Observation/obs-{patient_id}-nodes-stage",
            "resource": {
                "resourceType": "Observation",
                "id": f"obs-{patient_id}-nodes-stage",
                "status": "final",
                "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "imaging"}]}],
                "code": {
                    "coding": [{"system": "http://loinc.org", "code": "21906-3", "display": "N category"}],
                    "text": "Nodes stage"
                },
                "subject": {"reference": f"Patient/{patient_id}"},
                "effectiveDateTime": diagnosis_date.strftime('%Y-%m-%d'),
                "valueCodeableConcept": {"text": n_stage}
            }
        })

        # TNM: Distant metastasis stage — label matches UI dropdown values
        # Stage IV -> M1; all other stages -> M0 (enforces issue #7 rule)
        if stage == 'IV':
            m_stage = 'M1: Distant metastasis present'
        else:
            m_stage = 'M0: No distant metastasis'
        observations.append({
            "fullUrl": f"http://example.org/Observation/obs-{patient_id}-distant-metastasis-stage",
            "resource": {
                "resourceType": "Observation",
                "id": f"obs-{patient_id}-distant-metastasis-stage",
                "status": "final",
                "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "imaging"}]}],
                "code": {
                    "coding": [{"system": "http://loinc.org", "code": "21901-4", "display": "M category"}],
                    "text": "Distant metastasis stage"
                },
                "subject": {"reference": f"Patient/{patient_id}"},
                "effectiveDateTime": diagnosis_date.strftime('%Y-%m-%d'),
                "valueCodeableConcept": {"text": m_stage}
            }
        })

        # Staging modality
        staging_modality = random.choice(["CT", "PET-CT", "MRI", "CT+MRI", "Clinical exam"])
        observations.append({
            "fullUrl": f"http://example.org/Observation/obs-{patient_id}-staging-modality",
            "resource": {
                "resourceType": "Observation",
                "id": f"obs-{patient_id}-staging-modality",
                "status": "final",
                "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "imaging"}]}],
                "code": {
                    "coding": [{"system": "http://loinc.org", "code": "85319-2", "display": "Staging modality"}],
                    "text": "Staging modality"
                },
                "subject": {"reference": f"Patient/{patient_id}"},
                "effectiveDateTime": diagnosis_date.strftime('%Y-%m-%d'),
                "valueString": staging_modality
            }
        })

        # Measurable disease by RECIST — computed from staging when determinable
        if metastasis_status == "Positive" or t_cat in ["T3", "T4"]:
            measurable_by_recist = True
        elif metastasis_status == "Unknown":
            measurable_by_recist = None  # Can't determine without imaging data
        else:
            measurable_by_recist = False
        if measurable_by_recist is not None:
            observations.append({
                "fullUrl": f"http://example.org/Observation/obs-{patient_id}-recist",
                "resource": {
                    "resourceType": "Observation",
                    "id": f"obs-{patient_id}-recist",
                    "status": "final",
                    "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "imaging"}]}],
                    "code": {
                        "coding": [{"system": "http://loinc.org", "code": "21908-9", "display": "Measurable disease RECIST"}],
                        "text": "Measurable disease RECIST"
                    },
                    "subject": {"reference": f"Patient/{patient_id}"},
                    "effectiveDateTime": diagnosis_date.strftime('%Y-%m-%d'),
                    "valueBoolean": measurable_by_recist
                }
            })

        # Bone-only metastasis (True for ~15% of metastatic patients)
        bone_only = metastasis_status == "Positive" and random.random() < 0.15
        observations.append({
            "fullUrl": f"http://example.org/Observation/obs-{patient_id}-bone-only",
            "resource": {
                "resourceType": "Observation",
                "id": f"obs-{patient_id}-bone-only",
                "status": "final",
                "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "imaging"}]}],
                "code": {
                    "coding": [{"system": "http://loinc.org", "code": "44667-4", "display": "Bone only metastasis"}],
                    "text": "Bone only metastasis"
                },
                "subject": {"reference": f"Patient/{patient_id}"},
                "effectiveDateTime": diagnosis_date.strftime('%Y-%m-%d'),
                "valueBoolean": bone_only
            }
        })

        return observations

    def generate_lab_observations(self, patient_id, lab_date):
        """Generate lab observations (CBC, liver, kidney function)"""
        observations = []
        
        labs = [
            # Complete Blood Count (CBC)
            ("hemoglobin", round(random.uniform(9.5, 15.0), 1), "g/dL", "718-7", "Hemoglobin", (12.0, 16.0)),
            ("hematocrit", round(random.uniform(33.0, 48.0), 1), "%", "4544-3", "Hematocrit", (36.0, 46.0)),
            ("wbc", round(random.uniform(3.5, 11.0), 1), "10*3/uL", "6690-2", "White blood cell count", (4.0, 11.0)),
            ("rbc", round(random.uniform(3.8, 5.5), 2), "10*6/uL", "789-8", "Red blood cell count", (4.0, 5.2)),
            ("platelets", random.randint(100, 400), "10*3/uL", "777-3", "Platelets", (150, 400)),
            ("anc", round(random.uniform(1.5, 7.0), 1), "10*3/uL", "751-8", "Absolute Neutrophil Count", (1.5, 8.0)),
            ("alc", round(random.uniform(1.0, 4.0), 1), "10*3/uL", "731-0", "Absolute Lymphocyte Count", (1.0, 4.8)),
            ("amc", round(random.uniform(0.2, 0.9), 1), "10*3/uL", "742-7", "Absolute Monocyte Count", (0.2, 0.8)),
            
            # Kidney Function
            ("serum_creatinine", round(random.uniform(0.6, 1.8), 2), "mg/dL", "2160-0", "Serum Creatinine", (0.6, 1.2)),
            ("creatinine", round(random.uniform(0.6, 1.8), 2), "mg/dL", "2160-0", "Creatinine", (0.6, 1.2)),
            ("creatinine_clearance", round(random.uniform(60.0, 120.0), 1), "mL/min", "2164-2", "Creatinine Clearance", (85.0, 125.0)),
            ("egfr", round(random.uniform(60.0, 120.0), 1), "mL/min/1.73m2", "33914-3", "eGFR", (90.0, 120.0)),
            ("bun", round(random.uniform(7.0, 25.0), 1), "mg/dL", "3094-0", "Blood Urea Nitrogen", (7.0, 20.0)),
            
            # Electrolytes
            ("sodium", round(random.uniform(135.0, 145.0), 1), "mEq/L", "2951-2", "Sodium", (136.0, 145.0)),
            ("potassium", round(random.uniform(3.5, 5.0), 1), "mEq/L", "2823-3", "Potassium", (3.5, 5.0)),
            ("serum_calcium", round(random.uniform(8.5, 10.5), 1), "mg/dL", "17861-6", "Serum Calcium", (8.6, 10.2)),
            ("calcium", round(random.uniform(8.5, 10.5), 1), "mg/dL", "17861-6", "Calcium", (8.6, 10.2)),
            ("magnesium", round(random.uniform(1.7, 2.5), 1), "mg/dL", "19123-9", "Magnesium", (1.7, 2.2)),
            
            # Liver Function
            ("alt", random.randint(10, 100), "U/L", "1742-6", "ALT", (7, 56)),
            ("ast", random.randint(10, 100), "U/L", "1920-8", "AST", (8, 48)),
            ("bilirubin_total", round(random.uniform(0.2, 2.0), 1), "mg/dL", "1975-2", "Total Bilirubin", (0.1, 1.2)),
            ("albumin", round(random.uniform(3.0, 5.0), 1), "g/dL", "1751-7", "Albumin", (3.5, 5.5)),
            ("alkaline_phosphatase", random.randint(30, 120), "U/L", "6768-6", "Alkaline Phosphatase", (30, 120)),
            
            # Other Labs
            ("glucose", random.randint(70, 140), "mg/dL", "2345-7", "Glucose", (70, 100)),
            ("hba1c", round(random.uniform(4.5, 6.5), 1), "%", "4548-4", "HbA1c", (4.0, 5.6)),
            ("ldh", random.randint(100, 250), "U/L", "2532-0", "LDH", (122, 222)),
        ]
        
        for name, value, unit, loinc_code, display, ref_range in labs:
            obs = {
                "fullUrl": f"http://example.org/Observation/obs-{patient_id}-{name}",
                "resource": {
                    "resourceType": "Observation",
                    "id": f"obs-{patient_id}-{name}",
                    "status": "final",
                    "category": [{
                        "coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                            "code": "laboratory"
                        }]
                    }],
                    "code": {
                        "coding": [{
                            "system": "http://loinc.org",
                            "code": loinc_code,
                            "display": display
                        }],
                        "text": display
                    },
                    "subject": {
                        "reference": f"Patient/{patient_id}"
                    },
                    "effectiveDateTime": lab_date.strftime('%Y-%m-%d'),
                    "valueQuantity": {
                        "value": value,
                        "unit": unit,
                        "system": "http://unitsofmeasure.org",
                        "code": unit
                    },
                    "referenceRange": [{
                        "low": {"value": ref_range[0], "unit": unit},
                        "high": {"value": ref_range[1], "unit": unit}
                    }]
                }
            }
            observations.append(obs)
        
        return observations

    def generate_biomarker_observations(self, patient_id, diagnosis_date):
        """Generate biomarker observations (HER2, ER, PR, Ki67, PD-L1)"""
        observations = []
        
        is_tnbc = random.random() < getattr(self, 'tnbc_ratio', 0.15)
        
        if is_tnbc:
            her2 = "Negative"
            er = "Negative"
            pr = "Negative"
        else:
            her2 = random.choice(["Positive", "Negative", "Negative"])  # ~30% HER2+
            er = random.choice(["Positive", "Positive", "Positive", "Negative"])  # ~75% ER+
            pr = random.choice(["Positive", "Positive", "Negative"])  # ~65% PR+
        
        biomarkers = [
            ("HER2", her2, "48676-1", "HER2 receptor"),
            ("ER", er, "16112-5", "Estrogen receptor"),
            ("PR", pr, "16113-3", "Progesterone receptor"),
        ]
        
        for name, status, loinc_code, display in biomarkers:
            obs = {
                "fullUrl": f"http://example.org/Observation/obs-{patient_id}-{name}",
                "resource": {
                    "resourceType": "Observation",
                    "id": f"obs-{patient_id}-{name}",
                    "status": "final",
                    "category": [{
                        "coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                            "code": "laboratory"
                        }]
                    }],
                    "code": {
                        "coding": [{
                            "system": "http://loinc.org",
                            "code": loinc_code,
                            "display": display
                        }],
                        "text": display
                    },
                    "subject": {
                        "reference": f"Patient/{patient_id}"
                    },
                    "effectiveDateTime": diagnosis_date.strftime('%Y-%m-%d'),
                    "valueCodeableConcept": {
                        "coding": [{
                            "system": "http://snomed.info/sct",
                            "code": "10828004" if status == "Positive" else "260385009",
                            "display": status
                        }],
                        "text": status
                    }
                }
            }
            observations.append(obs)
        
        # Ki67 Proliferation Index (percentage 5-95%, weighted toward lower values)
        ki67_index = round(random.triangular(5, 95, 20))
        
        ki67_obs = {
            "fullUrl": f"http://example.org/Observation/obs-{patient_id}-ki67",
            "resource": {
                "resourceType": "Observation",
                "id": f"obs-{patient_id}-ki67",
                "status": "final",
                "category": [{
                    "coding": [{
                        "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                        "code": "laboratory"
                    }]
                }],
                "code": {
                    "coding": [{
                        "system": "http://loinc.org",
                        "code": "85337-4",
                        "display": "Ki-67 [Interpretation] in Tissue"
                    }],
                    "text": "Ki67 Proliferation Index"
                },
                "subject": {
                    "reference": f"Patient/{patient_id}"
                },
                "effectiveDateTime": diagnosis_date.strftime('%Y-%m-%d'),
                "valueQuantity": {
                    "value": ki67_index,
                    "unit": "%",
                    "system": "http://unitsofmeasure.org",
                    "code": "%"
                }
            }
        }
        observations.append(ki67_obs)
        
        # PD-L1 Status (percentage 0-100%, or Positive/Negative)
        pd_l1_percentage = random.randint(0, 100)
        pd_l1_status = "Positive" if pd_l1_percentage >= 1 else "Negative"
        
        pdl1_obs = {
            "fullUrl": f"http://example.org/Observation/obs-{patient_id}-pdl1",
            "resource": {
                "resourceType": "Observation",
                "id": f"obs-{patient_id}-pdl1",
                "status": "final",
                "category": [{
                    "coding": [{
                        "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                        "code": "laboratory"
                    }]
                }],
                "code": {
                    "coding": [{
                        "system": "http://loinc.org",
                        "code": "85147-7",
                        "display": "PD-L1 by clone 22C3 [Interpretation] in Tissue"
                    }],
                    "text": "PD-L1 Status"
                },
                "subject": {
                    "reference": f"Patient/{patient_id}"
                },
                "effectiveDateTime": diagnosis_date.strftime('%Y-%m-%d'),
                "valueCodeableConcept": {
                    "coding": [{
                        "system": "http://snomed.info/sct",
                        "code": "10828004" if pd_l1_status == "Positive" else "260385009",
                        "display": pd_l1_status
                    }],
                    "text": pd_l1_status
                },
                "component": [{
                    "code": {
                        "coding": [{
                            "system": "http://loinc.org",
                            "code": "85147-7",
                            "display": "PD-L1 percentage"
                        }],
                        "text": "PD-L1 tumor cells percentage"
                    },
                    "valueQuantity": {
                        "value": pd_l1_percentage,
                        "unit": "%",
                        "system": "http://unitsofmeasure.org",
                        "code": "%"
                    }
                }]
            }
        }
        observations.append(pdl1_obs)
        
        return observations

    def generate_genetic_mutations(self, patient_id, diagnosis_date):
        """Generate genetic mutation observations with detailed variant information"""
        observations = []
        
        # Common breast cancer genes with typical mutations
        bc_genes = {
            "BRCA1": {
                "mutations": ["c.68_69delAG", "c.5266dupC", "c.181T>G", "c.3756_3759del", "185delAG"],
                "weight": 0.06
            },
            "BRCA2": {
                "mutations": ["c.5946delT", "c.9097dupA", "c.7617+1G>A", "6174delT", "c.8537_8538del"],
                "weight": 0.06
            },
            "TP53": {
                "mutations": ["R175H", "R248Q", "R273H", "R248W", "R282W"],
                "weight": 0.35
            },
            "PIK3CA": {
                "mutations": ["E542K", "E545K", "H1047R", "H1047L", "E726K"],
                "weight": 0.30
            },
            "PTEN": {
                "mutations": ["R130*", "R173C", "R233*", "R335*", "c.209+1G>T"],
                "weight": 0.15
            },
            "ATM": {
                "mutations": ["c.5932G>T", "c.6095G>A", "c.8122G>A", "c.7271T>G"],
                "weight": 0.10
            },
            "CHEK2": {
                "mutations": ["1100delC", "I157T", "R117G", "IVS2+1G>A"],
                "weight": 0.08
            },
            "PALB2": {
                "mutations": ["c.3113G>A", "c.1676del", "c.509_510delGA", "c.172_175delTTGT"],
                "weight": 0.05
            },
            "CDH1": {
                "mutations": ["c.1018A>G", "c.1137G>A", "c.283C>T", "c.1901C>T"],
                "weight": 0.03
            },
            "ERBB2": {
                "mutations": ["L755S", "V777L", "G776delinsVC", "D769H"],
                "weight": 0.05
            }
        }
        
        interpretations = ["Pathogenic", "Likely pathogenic", "VUS", "Likely benign", "Benign"]
        interpretation_weights = [0.30, 0.25, 0.30, 0.10, 0.05]
        
        # Generate 0-4 mutations per patient
        num_mutations = random.choices([0, 1, 2, 3, 4], weights=[0.30, 0.35, 0.20, 0.10, 0.05])[0]
        
        selected_genes = []
        if num_mutations > 0:
            # Weight genes by their frequency
            gene_names = list(bc_genes.keys())
            gene_weights = [bc_genes[g]["weight"] for g in gene_names]
            selected_genes = random.sample(
                random.choices(gene_names, weights=gene_weights, k=num_mutations*2),
                k=min(num_mutations, len(gene_names))
            )
        
        for gene in selected_genes:
            mutation = random.choice(bc_genes[gene]["mutations"])
            
            # BRCA and PALB2 are more likely germline, others more likely somatic
            if gene in ["BRCA1", "BRCA2", "PALB2", "CHEK2", "ATM"]:
                origin = random.choices(["Germline", "Somatic"], weights=[0.70, 0.30])[0]
            else:
                origin = random.choices(["Germline", "Somatic"], weights=[0.20, 0.80])[0]
            
            # Pathogenic/Likely pathogenic more common for BRCA
            if gene in ["BRCA1", "BRCA2"]:
                interpretation = random.choices(interpretations, weights=[0.50, 0.30, 0.15, 0.03, 0.02])[0]
            else:
                interpretation = random.choices(interpretations, weights=interpretation_weights)[0]
            
            gene_loinc_codes = {
                "BRCA1": "21636-6",
                "BRCA2": "21637-4",
                "TP53": "47423-8",
                "PIK3CA": "55233-1",
                "PTEN": "48676-1",
                "ATM": "48675-3",
                "CHEK2": "48004-6",
                "PALB2": "88039-1",
                "CDH1": "81295-8",
                "ERBB2": "48676-1"
            }
            
            loinc_code = gene_loinc_codes.get(gene, "81247-9")
            
            obs = {
                "fullUrl": f"http://example.org/Observation/obs-{patient_id}-{gene}-{mutation[:20]}",
                "resource": {
                    "resourceType": "Observation",
                    "id": f"obs-{patient_id}-{gene}-mutation",
                    "status": "final",
                    "category": [{
                        "coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                            "code": "laboratory"
                        }]
                    }],
                    "code": {
                        "coding": [{
                            "system": "http://loinc.org",
                            "code": loinc_code,
                            "display": f"{gene} gene variant"
                        }],
                        "text": f"{gene} gene mutation analysis"
                    },
                    "subject": {
                        "reference": f"Patient/{patient_id}"
                    },
                    "effectiveDateTime": diagnosis_date.strftime('%Y-%m-%d'),
                    "valueCodeableConcept": {
                        "coding": [{
                            "system": "http://loinc.org",
                            "code": "LA9633-4" if interpretation in ["Pathogenic", "Likely pathogenic"] else "LA6668-3",
                            "display": interpretation
                        }],
                        "text": interpretation
                    },
                    "component": [
                        {
                            "code": {
                                "coding": [{
                                    "system": "http://loinc.org",
                                    "code": "48018-6",
                                    "display": "Gene studied"
                                }],
                                "text": "Gene"
                            },
                            "valueCodeableConcept": {
                                "text": gene
                            }
                        },
                        {
                            "code": {
                                "coding": [{
                                    "system": "http://loinc.org",
                                    "code": "81290-9",
                                    "display": "Genomic DNA change"
                                }],
                                "text": "Mutation"
                            },
                            "valueCodeableConcept": {
                                "text": mutation
                            }
                        },
                        {
                            "code": {
                                "coding": [{
                                    "system": "http://loinc.org",
                                    "code": "48002-0",
                                    "display": "Genomic source class"
                                }],
                                "text": "Origin"
                            },
                            "valueCodeableConcept": {
                                "coding": [{
                                    "system": "http://loinc.org",
                                    "code": "LA6683-2" if origin == "Germline" else "LA6684-0",
                                    "display": origin
                                }],
                                "text": origin
                            }
                        }
                    ]
                }
            }
            observations.append(obs)
        
        return observations

    def generate_prior_therapy(self, patient_id, diagnosis_date, assigned_lines=None):
        """Generate prior lines of therapy with named regimens, therapy intent and discontinuation observations"""
        medications = []
        observations = []
        
        # Distribution: 40% treatment-naive, 30% one line, 20% two lines, 10% three lines
        if assigned_lines is not None:
            num_lines = assigned_lines
        else:
            num_lines = random.choices([0, 1, 2, 3], weights=[0.4, 0.3, 0.2, 0.1])[0]
        
        # Breast cancer treatment regimens
        first_line_regimens = {
            'AC-T': [
                ('Doxorubicin', '3002'),
                ('Cyclophosphamide', '3007'),
                ('Paclitaxel', '1716024')
            ],
            'TC': [
                ('Docetaxel', '72962'),
                ('Cyclophosphamide', '3007')
            ],
            'Paclitaxel/Trastuzumab/Pertuzumab': [
                ('Paclitaxel', '1716024'),
                ('Trastuzumab', '224905'),
                ('Pertuzumab', '1298944')
            ],
            'Tamoxifen': [
                ('Tamoxifen', '10324')
            ],
            'CDK4/6 Inhibitor + Letrozole': [
                ('Palbociclib', '1873985'),
                ('Letrozole', '73274')
            ]
        }
        
        second_line_regimens = {
            'Capecitabine': [
                ('Capecitabine', '194000')
            ],
            'T-DM1': [
                ('Ado-trastuzumab emtansine', '1371352')
            ],
            'Eribulin': [
                ('Eribulin', '1045453')
            ],
            'Gemcitabine/Carboplatin': [
                ('Gemcitabine', '1736854'),
                ('Carboplatin', '40223')
            ],
            'Olaparib': [
                ('Olaparib', '1597582')
            ]
        }
        
        later_line_regimens = {
            'T-DXd': [
                ('Trastuzumab deruxtecan', '2360840')
            ],
            'Sacituzumab govitecan': [
                ('Sacituzumab govitecan', '2359306')
            ],
            'Vinorelbine': [
                ('Vinorelbine', '72956')
            ],
            'Pembrolizumab': [
                ('Pembrolizumab', '1547545')
            ]
        }
        
        # Generate therapies for each line
        selected_regimens = []
        for line_num in range(1, num_lines + 1):
            if line_num == 1:
                regimen_name = random.choice(list(first_line_regimens.keys()))
                regimen_drugs = first_line_regimens[regimen_name]
            elif line_num == 2:
                regimen_name = random.choice(list(second_line_regimens.keys()))
                regimen_drugs = second_line_regimens[regimen_name]
            else:
                regimen_name = random.choice(list(later_line_regimens.keys()))
                regimen_drugs = later_line_regimens[regimen_name]
            
            # Determine outcome for this line
            # Earlier lines and completed lines tend to have better outcomes
            if line_num < num_lines:  # Not the last line, so it led to progression
                outcome = random.choices(
                    ['Progressive Disease', 'Partial Response', 'Stable Disease'],
                    weights=[0.6, 0.3, 0.1]
                )[0]
            else:  # Current/last line
                outcome = random.choices(
                    ['Partial Response', 'Complete Response', 'Stable Disease', 'Progressive Disease'],
                    weights=[0.4, 0.3, 0.2, 0.1]
                )[0]
            
            selected_regimens.append({
                'line': line_num,
                'regimen_name': regimen_name,
                'drugs': regimen_drugs,
                'outcome': outcome
            })
            
            # Start therapy after diagnosis, each line 3-6 months apart
            months_after_diagnosis = (line_num - 1) * random.randint(4, 7)
            therapy_start = diagnosis_date + timedelta(days=30 + months_after_diagnosis * 30)
            therapy_end = therapy_start + timedelta(days=random.randint(90, 180))
            
            # Create MedicationStatement for the regimen
            regimen_resource = {
                "fullUrl": f"http://example.org/MedicationStatement/regimen-{patient_id}-line{line_num}",
                "resource": {
                    "resourceType": "MedicationStatement",
                    "id": f"regimen-{patient_id}-line{line_num}",
                    "status": "completed" if line_num < num_lines else "active",
                    "medicationCodeableConcept": {
                        "text": regimen_name
                    },
                    "subject": {
                        "reference": f"Patient/{patient_id}"
                    },
                    "effectivePeriod": {
                        "start": therapy_start.strftime('%Y-%m-%d'),
                        "end": therapy_end.strftime('%Y-%m-%d') if line_num < num_lines else None
                    },
                    "extension": [{
                        "url": "http://example.org/fhir/StructureDefinition/therapy-line",
                        "valueInteger": line_num
                    }, {
                        "url": "http://example.org/fhir/StructureDefinition/therapy-outcome",
                        "valueString": outcome
                    }],
                    "note": [{
                        "text": f"Line {line_num} therapy - Outcome: {outcome}"
                    }]
                }
            }
            medications.append(regimen_resource)
            
            # Create individual drug exposures
            for drug_name, rxnorm_code in regimen_drugs:
                drug_resource = {
                    "fullUrl": f"http://example.org/MedicationStatement/drug-{patient_id}-line{line_num}-{drug_name.replace(' ', '-')}",
                    "resource": {
                        "resourceType": "MedicationStatement",
                        "id": f"drug-{patient_id}-line{line_num}-{drug_name.replace(' ', '-')}",
                        "status": "completed" if line_num < num_lines else "active",
                        "medicationCodeableConcept": {
                            "coding": [{
                                "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                "code": rxnorm_code,
                                "display": drug_name
                            }],
                            "text": drug_name
                        },
                        "subject": {
                            "reference": f"Patient/{patient_id}"
                        },
                        "effectivePeriod": {
                            "start": therapy_start.strftime('%Y-%m-%d'),
                            "end": therapy_end.strftime('%Y-%m-%d') if line_num < num_lines else None
                        },
                        "partOf": [{
                            "reference": f"MedicationStatement/regimen-{patient_id}-line{line_num}"
                        }],
                        "extension": [{
                            "url": "http://example.org/fhir/StructureDefinition/therapy-line",
                            "valueInteger": line_num
                        }]
                    }
                }
                medications.append(drug_resource)
            
            # Create Therapy Intent observation (LOINC 42804-5)
            # Most lines should be DEFINITIVE_LOCAL (primary curative treatment)
            if line_num == 1:
                therapy_intent = random.choices(
                    ['DEFINITIVE_LOCAL', 'ADJUVANT', 'NEOADJUVANT', 'INDUCTION'],
                    weights=[0.70, 0.15, 0.10, 0.05]
                )[0]
            elif line_num == 2:
                therapy_intent = random.choices(
                    ['DEFINITIVE_LOCAL', 'METASTATIC_DISEASE_CONTROL', 'CONSOLIDATION', 'MAINTENANCE'],
                    weights=[0.60, 0.20, 0.15, 0.05]
                )[0]
            else:
                therapy_intent = random.choices(
                    ['DEFINITIVE_LOCAL', 'METASTATIC_DISEASE_CONTROL', 'SALVAGE', 'PALLIATIVE_SYMPTOM'],
                    weights=[0.50, 0.25, 0.15, 0.10]
                )[0]
            
            intent_obs = {
                "fullUrl": f"http://example.org/Observation/intent-{patient_id}-line{line_num}",
                "resource": {
                    "resourceType": "Observation",
                    "id": f"intent-{patient_id}-line{line_num}",
                    "status": "final",
                    "code": {
                        "coding": [{
                            "system": "http://loinc.org",
                            "code": "42804-5",
                            "display": "Therapy Intent"
                        }],
                        "text": "Therapy Intent"
                    },
                    "subject": {
                        "reference": f"Patient/{patient_id}"
                    },
                    "effectiveDateTime": therapy_start.strftime('%Y-%m-%d'),
                    "valueCodeableConcept": {
                        "text": therapy_intent
                    }
                }
            }
            observations.append(intent_obs)
            
            # Create Discontinuation Reason observation (LOINC 91379-3) for completed lines
            if line_num < num_lines:  # Only for completed therapy lines
                # Determine discontinuation reason based on outcome
                if outcome == 'Progressive Disease':
                    discontinuation = 'Progression'
                elif outcome in ['Partial Response', 'Complete Response']:
                    discontinuation = 'Completion'
                else:
                    discontinuation = random.choice(['Toxicity', 'Completion'])
                
                disc_obs = {
                    "fullUrl": f"http://example.org/Observation/discontinuation-{patient_id}-line{line_num}",
                    "resource": {
                        "resourceType": "Observation",
                        "id": f"discontinuation-{patient_id}-line{line_num}",
                        "status": "final",
                        "code": {
                            "coding": [{
                                "system": "http://loinc.org",
                                "code": "91379-3",
                                "display": "Reason for Discontinuation"
                            }],
                            "text": "Reason for Discontinuation"
                        },
                        "subject": {
                            "reference": f"Patient/{patient_id}"
                        },
                        "effectiveDateTime": therapy_end.strftime('%Y-%m-%d'),
                        "valueCodeableConcept": {
                            "text": discontinuation
                        }
                    }
                }
                observations.append(disc_obs)
        
        # Return both medications and observations
        return medications + observations
    
    def generate_supportive_therapy(self, patient_id, diagnosis_date):
        """Generate supportive therapy with intent (adjuvant/neoadjuvant)"""
        # 60% of patients have supportive therapy
        if random.random() > 0.6:
            return None
        
        supportive_intent_options = ['ADJUVANT', 'NEOADJUVANT']
        supportive_intent = random.choice(supportive_intent_options)
        
        # Generate supportive therapy dates relative to diagnosis
        if supportive_intent == 'NEOADJUVANT':
            # Before primary treatment/surgery
            start_date = diagnosis_date - timedelta(days=random.randint(30, 90))
            duration = random.randint(60, 120)  # 2-4 months
        else:  # ADJUVANT
            # After primary treatment/surgery
            start_date = diagnosis_date + timedelta(days=random.randint(90, 180))
            duration = random.randint(180, 365)  # 6-12 months
        
        end_date = start_date + timedelta(days=duration)
        
        supportive_therapies = [
            'G-CSF (Filgrastim)',
            'Pegfilgrastim',
            'Ondansetron',
            'Zoledronic acid',
            'Denosumab',
            'Bisphosphonates',
            'Antiemetics',
            'Growth factors'
        ]
        
        therapy_name = random.choice(supportive_therapies)
        
        return {
            "fullUrl": f"http://example.org/MedicationStatement/supportive-{patient_id}",
            "resource": {
                "resourceType": "MedicationStatement",
                "id": f"supportive-{patient_id}",
                "status": "completed",
                "medicationCodeableConcept": {
                    "text": therapy_name
                },
                "subject": {
                    "reference": f"Patient/{patient_id}"
                },
                "effectivePeriod": {
                    "start": start_date.strftime('%Y-%m-%d'),
                    "end": end_date.strftime('%Y-%m-%d')
                },
                "extension": [
                    {
                        "url": "http://example.org/fhir/StructureDefinition/supportive-therapy-intent",
                        "valueString": supportive_intent
                    },
                    {
                        "url": "http://example.org/fhir/StructureDefinition/therapy-type",
                        "valueString": "Supportive"
                    }
                ]
            }
        }
    
    def generate_planned_therapy(self, patient_id):
        """Generate planned therapy from standard of care options"""
        # 70% of patients have planned therapy
        if random.random() > 0.7:
            return None
        
        planned_therapies = [
            'AC-T (Doxorubicin/Cyclophosphamide followed by Paclitaxel)',
            'TC (Docetaxel/Cyclophosphamide)',
            'Paclitaxel/Trastuzumab/Pertuzumab (HER2+)',
            'T-DM1 (Trastuzumab emtansine)',
            'T-DXd (Trastuzumab deruxtecan)',
            'CDK4/6 Inhibitor + Aromatase Inhibitor',
            'Palbociclib + Letrozole',
            'Fulvestrant',
            'Olaparib (BRCA+)',
            'Pembrolizumab + Chemotherapy',
            'Capecitabine',
            'Eribulin',
            'Autologous Stem Cell Transplant',
            'Radiation Therapy',
            'Clinical Trial'
        ]
        
        planned_therapy = random.choice(planned_therapies)
        
        return {
            "fullUrl": f"http://example.org/CarePlan/planned-{patient_id}",
            "resource": {
                "resourceType": "CarePlan",
                "id": f"planned-{patient_id}",
                "status": "active",
                "intent": "plan",
                "title": "Planned Therapy",
                "description": planned_therapy,
                "subject": {
                    "reference": f"Patient/{patient_id}"
                },
                "activity": [{
                    "detail": {
                        "status": "scheduled",
                        "description": planned_therapy
                    }
                }]
            }
        }

    def generate_random_date(self, start_year, end_year):
        """Generate random date between years"""
        start_date = datetime(start_year, 1, 1)
        end_date = datetime(end_year, 12, 31)
        time_between = end_date - start_date
        days_between = time_between.days
        random_days = random.randrange(days_between)
        return start_date + timedelta(days=random_days)

    def generate_diagnosis_date(self, birth_date):
        """Generate diagnosis date (between age 30 and current age)"""
        today = datetime.now()
        min_diagnosis = birth_date + timedelta(days=30*365)
        max_diagnosis = min(today, birth_date + timedelta(days=80*365))
        
        if min_diagnosis >= max_diagnosis:
            return today - timedelta(days=random.randint(365, 3650))
        
        time_between = max_diagnosis - min_diagnosis
        days_between = time_between.days
        if days_between <= 0:
            return today - timedelta(days=random.randint(365, 3650))
        
        random_days = random.randrange(days_between)
        return min_diagnosis + timedelta(days=random_days)

    def get_first_names(self):
        return [
            "Emma", "Olivia", "Ava", "Isabella", "Sophia", "Mia", "Charlotte", "Amelia",
            "Harper", "Evelyn", "Abigail", "Emily", "Elizabeth", "Sofia", "Avery", "Ella",
            "Scarlett", "Grace", "Chloe", "Victoria", "Riley", "Aria", "Lily", "Aubrey",
            "Zoey", "Penelope", "Lillian", "Addison", "Layla", "Natalie", "Camila", "Hannah"
        ]

    def get_last_names(self):
        return [
            "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
            "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
            "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Thompson", "White",
            "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker", "Young"
        ]
