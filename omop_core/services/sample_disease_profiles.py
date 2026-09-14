"""Disease-specific demo facts, recoverable without inventing standard concepts."""
import hashlib
import json
import random
import re
from datetime import date

from omop_core.services.flipi import calculate_flipi, normalize_grade

PREFIX = 'demo:'
SYSTEM = 'https://healthkey.ai/fhir/CodeSystem/demo-assessment'
COMMON_FIELDS = {'ecog_performance_status', 'hemoglobin_g_dl', 'ldh_u_l', 'platelet_count_thousand_per_ul',
                 'anc_thousand_per_ul', 'albumin_g_dl', 'serum_calcium_mg_dl', 'serum_creatinine_mg_dl'}
FL_FIELDS = {'tumor_grade', 'gelf_criteria_status', 'gelf_criteria_options', 'flipi_score_options',
             'number_of_nodal_sites', 'bulky_disease', 'b_symptoms', 'bone_marrow_involvement',
             'largest_lymph_node_size', 'splenomegaly', 'spleen_size', 'ldh_upper_limit_normal',
             'serum_beta2_microglobulin_level'}
MM_FIELDS = {'myeloma_type', 'sct_eligibility', 'stem_cell_transplant_history', 'sct_date',
             'kappa_lambda_ratio', 'mrd_status', 'cytogenetic_markers', 'progression'}
BC_FIELDS = {'tumor_grade', 'menopausal_status', 'oncotype_dx_score', 'pd_l1_assay',
             'pd_l1_combined_positive_score', 'bone_only_metastasis_status', 'therapy_intent'}
PROFILE_FIELDS = COMMON_FIELDS | FL_FIELDS | MM_FIELDS | BC_FIELDS
PROFILE_UNITS = {'hemoglobin_g_dl': 'g/dL', 'ldh_u_l': 'U/L', 'ldh_upper_limit_normal': 'U/L',
                 'platelet_count_thousand_per_ul': '10*3/uL', 'anc_thousand_per_ul': '10*3/uL',
                 'albumin_g_dl': 'g/dL', 'serum_calcium_mg_dl': 'mg/dL', 'serum_creatinine_mg_dl': 'mg/dL',
                 'largest_lymph_node_size': 'cm', 'spleen_size': 'cm', 'serum_beta2_microglobulin_level': 'mg/L'}
GELF_FACTORS = {
    'large_mass': 'A nodal or extranodal mass > 7 cm',
    'multiple_large_nodes': 'At least 3 nodal areas, each > 3 cm',
    'b_symptoms': 'Lymphoma-related B symptoms',
    'compression': 'Organ compression or threatened organ function',
    'splenomegaly': 'Symptomatic splenomegaly',
    'effusion': 'Lymphoma-related pleural effusion or ascites',
    'circulating_cells': 'Circulating lymphoma cells > 5 × 10⁹/L',
    'cytopenia': 'Disease-related neutropenia < 1 × 10⁹/L or platelets < 100 × 10⁹/L',
}


def missing(value):
    return value is None or value == [] or (isinstance(value, str) and value.strip().lower() in {
        '', 'unknown', 'n/a', 'not recorded', 'not available',
    })


def profile_values(record, disease):
    """Fill gaps coherently from existing inputs; never rewrite recorded answers."""
    rng = random.Random(hashlib.sha256(f'profile:{record.person_id}:{disease}'.encode()).hexdigest())
    values = {}
    def get(field, default=None):
        value = values.get(field, getattr(record, field, None))
        return default if missing(value) else value
    def put(field, value):
        if missing(getattr(record, field, None)) and value is not None:
            values[field] = value
        return get(field)
    stage = str(getattr(record, 'stage', '') or '').upper().replace('STAGE ', '').strip()
    advanced = stage.startswith(('III', 'IV'))
    dob = getattr(record, 'date_of_birth', None)
    diagnosed = getattr(record, 'diagnosis_date', None) or date.today()
    age = diagnosed.year - dob.year - ((diagnosed.month, diagnosed.day) < (dob.month, dob.day)) if dob else getattr(record, 'patient_age', None)
    put('ecog_performance_status', rng.choices([0, 1, 2], [40, 45, 15])[0])
    if disease == 'FL':
        put('tumor_grade', rng.choices(['1', '2', '3A'], [35, 40, 25])[0])
        put('hemoglobin_g_dl', round(rng.uniform(10, 15.5), 1))
        put('ldh_u_l', rng.randint(130, 310))
        put('ldh_upper_limit_normal', 225)
        put('platelet_count_thousand_per_ul', rng.randint(90, 320))
        put('anc_thousand_per_ul', round(rng.uniform(1.2, 6), 1))
        put('number_of_nodal_sites', rng.randint(3, 8) if advanced else rng.randint(1, 3))
        put('bulky_disease', rng.random() < .25)
        put('largest_lymph_node_size', round(rng.uniform(7.1, 10) if get('bulky_disease') else rng.uniform(1, 6.8), 1))
        put('b_symptoms', rng.random() < .2)
        # Marrow involvement is extranodal disease: keep synthetic positives in stage IV.
        put('bone_marrow_involvement', stage.startswith('IV') and rng.random() < .65)
        put('splenomegaly', advanced and rng.random() < .2)
        put('spleen_size', round(rng.uniform(17, 22) if get('splenomegaly') else rng.uniform(10, 13), 1))
        put('serum_beta2_microglobulin_level', round(rng.uniform(1.2, 5), 1))
        # Null and explicit empty selections differ: preserve an assessed score of zero.
        if (getattr(record, 'flipi_score_options', None) is None
                and getattr(record, 'flipi_score', None) is None and age is not None):
            factors = [key for key, present in [
                ('age', age > 60), ('stage', advanced), ('hemoglobin', float(get('hemoglobin_g_dl')) < 12),
                ('nodalAreas', int(get('number_of_nodal_sites')) > 4),
                ('ldh', float(get('ldh_u_l')) > float(get('ldh_upper_limit_normal'))),
            ] if present]
            values['flipi_score_options'] = ','.join(factors)
        if getattr(record, 'gelf_criteria_options', None) is None:
            factors = []
            if float(get('largest_lymph_node_size')) > 7: factors.append('large_mass')
            if get('b_symptoms'): factors.append('b_symptoms')
            if get('splenomegaly'): factors.append('splenomegaly')
            if float(get('platelet_count_thousand_per_ul')) < 100 or float(get('anc_thousand_per_ul')) < 1:
                factors.append('cytopenia')
            # Nodal count and marrow positivity alone do not establish GELF.
            values['gelf_criteria_options'] = ','.join(factors)
            put('gelf_criteria_status', 'Met' if factors else 'Not Met')
    elif disease == 'MM':
        kappa, lam = get('kappa_flc'), get('lambda_flc')
        light_chain = 'kappa' if kappa is None or lam is None or float(kappa) >= float(lam) else 'lambda'
        put('myeloma_type', rng.choice(['IgG', 'IgA', 'Light-chain']) + ' ' + light_chain)
        if kappa is not None and lam is not None and float(lam) > 0:
            put('kappa_lambda_ratio', round(float(kappa) / float(lam), 4))
        # Eligibility is a labeled synthetic assessment, never inferred as a clinical decision.
        put('sct_eligibility', [rng.choice(['eligible for autologous SCT', 'ineligible for autologous SCT'])])
        status = str(get('condition_clinical_status', 'active')).lower()
        put('progression', 'Relapsed' if status in {'relapse', 'progressing'} else 'Stable Disease')
        # MRD results require an assessment; don't fabricate negative results for every patient.
        outcome = str(get('second_line_outcome', get('first_line_outcome', ''))).lower()
        if status == 'remission' or outcome in {'cr', 'complete response', 'stringent complete response', 'scr'}:
            put('mrd_status', rng.choice(['Positive', 'Negative']))
    elif disease == 'BC':
        put('tumor_grade', rng.choice(['1', '2', '3']))
        if age is not None:
            put('menopausal_status', 'Post-menopausal' if age >= 55 else 'Pre-menopausal' if age < 45 else 'Peri-menopausal')
        er = str(get('estrogen_receptor_status', '')).lower()
        pr = str(get('progesterone_receptor_status', '')).lower()
        her2 = str(get('her2_status', '')).lower()
        metastatic = stage.startswith('IV') or str(get('distant_metastasis_stage', '')).startswith('M1')
        hr_positive = er == 'positive' or pr == 'positive'
        early = stage.startswith(('I', 'II')) and not advanced
        nodes = str(get('nodes_stage', '')).upper()
        if hr_positive and her2 == 'negative' and early and nodes.startswith(('N0', 'N1')):
            put('oncotype_dx_score', rng.randint(5, 38))
        if er == pr == her2 == 'negative' and metastatic:
            put('pd_l1_assay', '22C3')
            put('pd_l1_combined_positive_score', rng.choice([0, 2, 5, 10, 15, 25, 40]))
        if metastatic: put('bone_only_metastasis_status', rng.random() < .3)
        put('therapy_intent', 'Metastatic' if metastatic else rng.choice(['Adjuvant', 'Neoadjuvant']))
    return values


def read_profile_rows(rows):
    """Read explicit demo field identities; no concept-name substring guesses."""
    from omop_core.models import PatientRecord
    fields = {f.name: f for f in PatientRecord._meta.fields}
    result = {}
    rows = sorted(rows, key=lambda row: (getattr(row, 'measurement_date', None) or getattr(row, 'observation_date', None) or date.min, row.pk), reverse=True)
    for row in rows:
        source = getattr(row, 'measurement_source_value', None) or getattr(row, 'observation_source_value', '') or ''
        key = row.value_source_value or ''
        if not key.startswith(PREFIX): key = source
        if not key.startswith(PREFIX): continue
        field = key[len(PREFIX):]
        if field not in PROFILE_FIELDS or field in result: continue
        value = row.value_as_number if row.value_as_number is not None else row.value_as_string
        if value is None: continue
        try:
            if fields[field].get_internal_type() == 'JSONField':
                value = json.loads(str(value)) if str(value).startswith('[') else [s.strip() for s in str(value).split(',') if s.strip()]
            elif fields[field].get_internal_type() == 'BooleanField': value = str(value).lower() in {'true', 'yes', '1', '1.00000'}
            else: value = fields[field].to_python(value)
            if field == 'tumor_grade': value = normalize_grade(value)
            result[field] = value
        except (ValueError, TypeError):
            continue
    return result


def profile_fhir_observation(patient_id, field, value, assessment_date):
    """Explicit local code system for facts without a verified standard code."""
    resource = {
        'resourceType': 'Observation', 'id': f'{patient_id}-demo-{field}', 'status': 'final',
        'subject': {'reference': f'Patient/{patient_id}'},
        'code': {'coding': [{'system': SYSTEM, 'code': PREFIX + field, 'display': field.replace('_', ' ')}]},
        'category': [{'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/observation-category', 'code': 'survey'}]}],
        'effectiveDateTime': str(assessment_date),
    }
    if isinstance(value, bool): resource['valueBoolean'] = value
    elif isinstance(value, (float, int)):
        resource['valueQuantity'] = {'value': value}
        if field in PROFILE_UNITS:
            resource['valueQuantity'].update(unit=PROFILE_UNITS[field], code=PROFILE_UNITS[field], system='http://unitsofmeasure.org')
    else: resource['valueString'] = json.dumps(value) if isinstance(value, list) else str(value)
    return resource


def recover_sample_source_fields(record, rows, disease):
    """Repair known legacy generator encodings only inside explicit demo cohorts.

    Several historical generators used nonexistent/mismatched LOINC codes.
    Those source keys are compatibility inputs here, never standard mappings.
    """
    from omop_core.services.patient_record_service import _LOINC_LAB_FIELDS
    from omop_core.services.cytogenetics import normalise_cytogenetic_markers
    result = read_profile_rows(rows)
    legacy = {
        'FL': {'44648-4': 'tumor_grade', '21912-1': 'number_of_nodal_sites'},
        'MM': {'57905-2': 'myeloma_type', '69548-6': 'cytogenetic_markers',
               'mm-cytogenetic-markers': 'cytogenetic_markers', 'mm-sct-date': 'sct_date',
               'mm-sct-history': 'stem_cell_transplant_history', 'mm-sct-eligibility': 'sct_eligibility'},
        'BC': {},
    }[disease]
    for row in rows:
        source = getattr(row, 'measurement_source_value', None) or getattr(row, 'observation_source_value', '') or ''
        field = legacy.get(source)
        if source in _LOINC_LAB_FIELDS:
            candidate = _LOINC_LAB_FIELDS[source][0]
            if candidate in COMMON_FIELDS: field = candidate
        if source == '21908-9': field = 'stage'
        if source == '21908-9-riss' and disease == 'MM': field = 'stage'
        if not field or field in result: continue
        value = row.value_as_number if row.value_as_number is not None else row.value_as_string
        if value is None: continue
        try:
            if field == 'tumor_grade': value = normalize_grade(value)
            elif field == 'cytogenetic_markers': value = normalise_cytogenetic_markers(value)
            elif field in {'stem_cell_transplant_history', 'sct_eligibility'}:
                value = [s.strip() for s in str(value).split(',') if s.strip()]
            elif field == 'sct_date': value = date.fromisoformat(str(value)[:10])
            elif field == 'number_of_nodal_sites': value = int(value)
            if field == 'stage' and disease == 'FL':
                stage_match = re.search(r'\b(IV|III|II|I)([AB])?\b', str(value).upper())
                if stage_match:
                    value = stage_match.group(1)
                    if stage_match.group(2): result.setdefault('b_symptoms', stage_match.group(2) == 'B')
            if field == 'stage' and isinstance(value, str) and not re.search(r'\b[IVX]+|\b[0-4]', value.upper()): continue
            result[field] = value
            if source == '2532-0' and getattr(row, 'range_high', None):
                result.setdefault('ldh_upper_limit_normal', int(row.range_high))
        except (ValueError, TypeError):
            continue
    # Prefer the specific R-ISS assertion over a coexisting ISS result.
    if disease == 'MM':
        riss = next((r.value_as_string for r in rows if getattr(r, 'measurement_source_value', None) == '21908-9-riss' or getattr(r, 'observation_source_value', None) == '21908-9-riss'), None)
        if riss: result['stage'] = riss
    return result


def complete_demo_fhir_bundle(bundle, disease):
    """Add disease-profile source facts to newly generated bundles, before import."""
    from types import SimpleNamespace
    from omop_core.services.patient_record_service import _LOINC_LAB_FIELDS
    resources = [entry['resource'] for entry in bundle.get('entry', []) if 'resource' in entry]
    by_patient = {}
    for resource in resources:
        if resource.get('resourceType') == 'Patient':
            by_patient[resource['id']] = SimpleNamespace(
                person_id=resource['id'], date_of_birth=date.fromisoformat(resource['birthDate']) if resource.get('birthDate') else None,
                diagnosis_date=None, stage=None,
            )
    code_fields = {code: field for code, (field, _) in _LOINC_LAB_FIELDS.items()}
    code_fields.update({'16112-5': 'estrogen_receptor_status', '16113-3': 'progesterone_receptor_status',
                        '48676-1': 'her2_status', '21908-9': 'stage', '21908-9-riss': 'stage',
                        '21901-4': 'distant_metastasis_stage', '21906-3': 'nodes_stage'})
    for resource in resources:
        reference = (resource.get('subject') or {}).get('reference', '').split('/')[-1]
        record = by_patient.get(reference)
        if record is None: continue
        if resource.get('resourceType') == 'Condition' and resource.get('onsetDateTime'):
            onset = date.fromisoformat(resource['onsetDateTime'][:10])
            if record.diagnosis_date is None or onset < record.diagnosis_date: record.diagnosis_date = onset
        if resource.get('resourceType') != 'Observation': continue
        coding = (resource.get('code', {}).get('coding') or [{}])[0]
        code = coding.get('code', '')
        field = code[len(PREFIX):] if code.startswith(PREFIX) else code_fields.get(code)
        if not field: continue
        value = resource.get('valueQuantity', {}).get('value', resource.get('valueInteger', resource.get('valueBoolean', resource.get('valueString'))))
        if value is None:
            answer = resource.get('valueCodeableConcept', {})
            value = answer.get('text') or next((c.get('display') or c.get('code') for c in answer.get('coding', [])), None)
        if value is not None: setattr(record, field, value)
    # Both command-line importers batch at Patient boundaries. Insert the new
    # facts beside their Patient, not after the last patient's resources.
    completed = []
    for entry in bundle.get('entry', []):
        completed.append(entry)
        resource = entry.get('resource', {})
        if resource.get('resourceType') != 'Patient':
            continue
        patient_id = resource['id']
        record = by_patient[patient_id]
        for field, value in profile_values(record, disease).items():
            completed.append({'resource': profile_fhir_observation(patient_id, field, value, record.diagnosis_date or date.today())})
    bundle['entry'] = completed
    return bundle
