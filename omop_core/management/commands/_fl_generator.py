"""
FL (Follicular Lymphoma) FHIR Bundle generator — internal module.

Imported by generate_fhir_bundle.py when --disease fl is passed.
Not a management command itself.

Regimen catalogue is loaded from the OMOP Concept/ConceptRelationship tables
at runtime using HemOnc vocabulary. FL_LOT_WEIGHTS in lot_regimens.py encodes
which regimens are used at each line of therapy and their relative frequencies.
"""

import random
from datetime import date, datetime, timedelta

from omop_core.services.lot_regimens import (
    FL_CONDITION_CONCEPT_ID,
    FL_LOT_WEIGHTS,
    load_hemonc_regimens_for_disease,
)

# ---------------------------------------------------------------------------
# LOINC codes
# ---------------------------------------------------------------------------
_L = {
    'hgb':        '718-7',
    'plt':        '777-3',
    'wbc':        '6690-2',
    'rbc':        '789-8',
    'anc':        '26499-4',
    'calcium':    '17861-6',
    'creatinine': '2160-0',
    'egfr':       '69405-9',
    'bun':        '3094-0',
    'sodium':     '2951-2',
    'potassium':  '2823-3',
    'ast':        '1920-8',
    'alt':        '1742-6',
    'alp':        '6768-6',
    'tbili':      '1975-2',
    'albumin':    '1751-7',
    'ldh':        '2532-0',
    'b2m':        '1952-1',
    'ecog':       '89247-1',
    'kps':        '89243-0',
    'bm_b_cells': '85319-5',
}

# ---------------------------------------------------------------------------
# Radiation regimens — generate a FHIR Procedure, not a MedicationStatement.
# Not present as distinct HemOnc Regimen class concepts; handled separately.
# ---------------------------------------------------------------------------
_RADIATION_REGIMENS = {'ISRT', 'IFRT', 'Rituximab + ISRT', 'Rituximab + IFRT'}

# ---------------------------------------------------------------------------
# Aliases: HemOnc drug names → _DRUG_INFO key (only needed where they differ)
# ---------------------------------------------------------------------------
_HEMONC_DRUG_ALIAS: dict[str, str] = {
    'axicabtagene ciloleucel': 'axicabtagene',
}

# ---------------------------------------------------------------------------
# RxNorm / concept IDs for individual drugs
# ---------------------------------------------------------------------------
_DRUG_INFO: dict[str, tuple[str, str]] = {
    'rituximab':            ('121191',  'Rituximab (Rituxan)'),
    'bendamustine':         ('699871',  'Bendamustine (Treanda)'),
    'cyclophosphamide':     ('3002',    'Cyclophosphamide'),
    'doxorubicin':          ('3639',    'Doxorubicin'),
    'vincristine':          ('11384',   'Vincristine'),
    'prednisone':           ('8638',    'Prednisone'),
    'obinutuzumab':         ('1517319', 'Obinutuzumab (Gazyva)'),
    'lenalidomide':         ('337535',  'Lenalidomide (Revlimid)'),
    'tazemetostat':         ('2388032', 'Tazemetostat (Tazverik)'),
    'axicabtagene':         ('1986200', 'Axicabtagene ciloleucel (Yescarta)'),
    'tisagenlecleucel':     ('2049126', 'Tisagenlecleucel (Kymriah)'),
    'mosunetuzumab':        ('2647309', 'Mosunetuzumab (Lunsumio)'),
    'glofitamab':           ('2794909', 'Glofitamab (Columvi)'),
    'epcoritamab':          ('2794910', 'Epcoritamab (Epkinly)'),
    'copanlisib':           ('1860485', 'Copanlisib (Aliqopa)'),
    'idelalisib':           ('1721006', 'Idelalisib (Zydelig)'),
    'fludarabine':          ('3249',    'Fludarabine'),
    'chlorambucil':         ('2393',    'Chlorambucil'),
    'gemcitabine':          ('44785',   'Gemcitabine'),
    'oxaliplatin':          ('77997',   'Oxaliplatin'),
    'cisplatin':            ('2555',    'Cisplatin'),
    'dexamethasone':        ('3264',    'Dexamethasone'),
    'etoposide':            ('3423',    'Etoposide'),
    'cytarabine':           ('2585',    'Cytarabine (Ara-C)'),
    'methylprednisolone':   ('41493',   'Methylprednisolone'),
    # Radiation sentinel keys — never emitted as MedicationStatement
    'isrt':  None,
    'ifrt':  None,
}

_US_LOCATIONS = [
    ('New York', 'NY'), ('Los Angeles', 'CA'), ('Chicago', 'IL'), ('Houston', 'TX'),
    ('Boston', 'MA'), ('Miami', 'FL'), ('Phoenix', 'AZ'), ('Philadelphia', 'PA'),
    ('Seattle', 'WA'), ('Denver', 'CO'), ('Atlanta', 'GA'), ('Minneapolis', 'MN'),
    ('Cleveland', 'OH'), ('Portland', 'OR'), ('Nashville', 'TN'),
]
_FIRST_NAMES = [
    'James', 'Robert', 'John', 'Michael', 'William', 'David', 'Richard', 'Joseph',
    'Mary', 'Patricia', 'Jennifer', 'Linda', 'Barbara', 'Elizabeth', 'Susan', 'Jessica',
    'Thomas', 'Charles', 'Gary', 'George', 'Dorothy', 'Helen', 'Sandra', 'Donna',
]
_LAST_NAMES = [
    'Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis',
    'Wilson', 'Anderson', 'Taylor', 'Thomas', 'Jackson', 'White', 'Harris', 'Martin',
    'Thompson', 'Young', 'Robinson', 'Lewis', 'Walker', 'Hall', 'Allen', 'Wright',
]
_ETHNICITIES       = ['Caucasian/White', 'Hispanic/Latino', 'Black/African-American', 'Asian', 'Native American']
_ETHNICITY_WEIGHTS = [72, 10, 10, 6, 2]
_OUTCOMES          = ['Progressive Disease', 'Stable Disease', 'Partial Response', 'Complete Response']
_OUTCOME_W_PREV    = [65, 10, 18, 7]
_OUTCOME_W_LAST    = [20, 20, 35, 25]


def _wc(items, weights):
    return random.choices(items, weights=weights, k=1)[0]


def _pick_regimen(regimen_list, early_stage=False):
    """Choose a regimen, optionally excluding radiation for non-early-stage patients.

    Each entry in regimen_list is a 4-tuple: (name, drugs, weight, concept_id).
    Returns (name, drugs, concept_id).
    """
    if not early_stage:
        eligible = [(n, d, w, cid) for n, d, w, cid in regimen_list if n not in _RADIATION_REGIMENS]
    else:
        eligible = list(regimen_list)
    if not eligible:
        raise RuntimeError(
            f"No eligible regimens to pick from (early_stage={early_stage}, "
            f"pool_size={len(regimen_list)}). Check FL_LOT_WEIGHTS and the HemOnc DB."
        )
    names, drugs_lists, weights, concept_ids = zip(*eligible)
    idx = random.choices(range(len(names)), weights=list(weights), k=1)[0]
    return names[idx], list(drugs_lists[idx]), concept_ids[idx]


class FLBundleGenerator:
    """Generates a FHIR R4 Bundle for Follicular Lymphoma patients."""

    def __init__(self, watch_wait_ratio=0.20):
        self.watch_wait_ratio = watch_wait_ratio
        self._first_line_regimens, self._later_line_regimens = self._build_regimen_lists()

    def _build_regimen_lists(self):
        """Load FL regimen catalog from HemOnc Concept table, filtered by FL_LOT_WEIGHTS.

        Each entry is a 4-tuple: (concept_name, drugs, weight, concept_id).
        Radiation regimens (not in HemOnc) are appended as special cases with concept_id=None.
        """
        try:
            catalog = load_hemonc_regimens_for_disease(FL_CONDITION_CONCEPT_ID)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load FL regimen catalog from OMOP Concept table: {exc}"
            ) from exc

        if not catalog:
            raise RuntimeError(
                "FL regimen catalog is empty. Ensure the HemOnc vocabulary is loaded in the "
                f"OMOP concept/concept_relationship tables (condition_concept_id={FL_CONDITION_CONCEPT_ID})."
            )

        first_line: list[tuple] = []
        later_line: list[tuple] = []

        for reg in catalog:
            cid = reg['concept_id']
            w1, wl = FL_LOT_WEIGHTS.get(cid, (0, 0))
            if w1 == 0 and wl == 0:
                continue
            # Map HemOnc drug names to _DRUG_INFO keys (aliases where needed)
            drugs = [_HEMONC_DRUG_ALIAS.get(d, d) for d in reg['drugs']]
            if w1 > 0:
                first_line.append((reg['concept_name'], drugs, w1, cid))
            if wl > 0:
                later_line.append((reg['concept_name'], drugs, wl, cid))

        # Radiation regimens are not modelled as HemOnc Regimen class concepts.
        # They apply only to early-stage (I–II) patients and generate FHIR Procedures.
        first_line.extend([
            ('ISRT',             [],            10, None),
            ('Rituximab + ISRT', ['rituximab'],  8, None),
        ])

        if not later_line:
            raise RuntimeError(
                "FL later-line regimen list is empty. Ensure FL_LOT_WEIGHTS has entries with "
                f"later_weight > 0, and the HemOnc DB has those concept IDs. "
                f"DB returned {len(catalog)} regimen(s) for condition_concept_id={FL_CONDITION_CONCEPT_ID}."
            )

        return first_line, later_line

    def generate_bundle(self, count):
        bundle = {'resourceType': 'Bundle', 'type': 'collection', 'entry': []}
        for i in range(1, count + 1):
            self._add_patient(bundle, i)
        return bundle

    # ------------------------------------------------------------------
    # Bundle assembly
    # ------------------------------------------------------------------

    def _add_patient(self, bundle, pid):
        p         = self._profile(pid)
        diag_date = self._random_date(2015, 2023)
        lab_date  = datetime.now() - timedelta(days=random.randint(1, 60))

        def _entry(resource):
            rt, rid = resource['resourceType'], resource['id']
            bundle['entry'].append({'fullUrl': f'http://example.org/{rt}/{rid}', 'resource': resource})

        _entry(self._patient_resource(p))
        _entry(self._condition_resource(p, diag_date))
        for obs in self._fl_labs(p, lab_date):
            _entry(obs)
        for obs in self._performance_obs(p, lab_date):
            _entry(obs)
        for obs in self._fl_specific_obs(p, diag_date):
            _entry(obs)
        if not p['watch_and_wait']:
            for resource in self._therapy_resources(p, diag_date):
                _entry(resource)
            if p['maintenance_rituximab']:
                _entry(self._maintenance_rituximab(p, diag_date))

    # ------------------------------------------------------------------
    # Patient profile
    # ------------------------------------------------------------------

    def _profile(self, pid):
        p = {'id': str(pid)}

        p['gender']     = _wc(['male', 'female'], [52, 48])
        p['age']        = int(random.triangular(42, 82, 62))
        p['ethnicity']  = _wc(_ETHNICITIES, _ETHNICITY_WEIGHTS)
        p['first_name'] = random.choice(_FIRST_NAMES)
        p['last_name']  = random.choice(_LAST_NAMES)
        p['city'], p['state'] = random.choice(_US_LOCATIONS)

        if p['gender'] == 'male':
            p['weight'] = round(random.uniform(62, 105), 1)
            p['height'] = round(random.uniform(165, 187), 1)
        else:
            p['weight'] = round(random.uniform(50, 92), 1)
            p['height'] = round(random.uniform(153, 175), 1)
        p['systolic_bp']  = random.randint(108, 155)
        p['diastolic_bp'] = random.randint(60, 95)
        p['heart_rate']   = random.randint(58, 100)

        p['ann_arbor_stage'] = _wc(['I', 'II', 'III', 'IV'], [5, 8, 35, 52])
        p['early_stage']     = p['ann_arbor_stage'] in ('I', 'II')

        b_sym_prob   = {'I': 0.05, 'II': 0.10, 'III': 0.18, 'IV': 0.25}[p['ann_arbor_stage']]
        p['b_symptoms']    = random.random() < b_sym_prob
        p['bulky_disease'] = random.random() < 0.20

        p['grade']    = _wc([1, 2, 3], [32, 35, 33])
        p['grade_3b'] = (p['grade'] == 3) and (random.random() < 0.35)

        p['nodal_sites'] = random.randint(1, 4) if p['early_stage'] else random.randint(1, 10)

        bm_prob = {'I': 0.05, 'II': 0.10, 'III': 0.35, 'IV': 0.55}[p['ann_arbor_stage']]
        p['bone_marrow_involvement'] = random.random() < bm_prob
        p['bm_b_cells_pct'] = (round(random.uniform(10, 65), 1) if p['bone_marrow_involvement']
                                else round(random.uniform(0.5, 8.0), 1))

        anemia_prob  = {'I': 0.10, 'II': 0.18, 'III': 0.30, 'IV': 0.45}[p['ann_arbor_stage']]
        p['hemoglobin'] = (round(random.uniform(7.5, 11.9), 1) if random.random() < anemia_prob
                           else round(random.uniform(12.0, 16.5), 1))
        p['platelets']  = random.randint(80, 420)
        p['wbc']        = round(random.uniform(2.8, 12.0), 1)
        p['anc']        = round(random.uniform(1.0, 8.0), 2)
        p['rbc']        = round(random.uniform(2.5, 5.5), 2)

        ldh_elev_prob = 0.40 if (p['ann_arbor_stage'] == 'IV' or p['b_symptoms']) else 0.20
        p['ldh']         = (random.randint(226, 480) if random.random() < ldh_elev_prob
                            else random.randint(90, 225))
        p['ldh_elevated'] = p['ldh'] > 225

        b2m_elev_prob = {'I': 0.10, 'II': 0.18, 'III': 0.35, 'IV': 0.55}[p['ann_arbor_stage']]
        p['b2m'] = (round(random.uniform(3.1, 10.5), 2) if random.random() < b2m_elev_prob
                    else round(random.uniform(0.8, 3.0), 2))

        p['calcium']    = round(random.uniform(8.5, 10.8), 1)
        p['creatinine'] = round(random.uniform(0.6, 1.4), 2)
        p['egfr']       = random.randint(55, 120)
        p['bun']        = round(random.uniform(8, 28), 1)
        p['sodium']     = round(random.uniform(136, 144), 1)
        p['potassium']  = round(random.uniform(3.5, 5.0), 1)
        p['ast']        = random.randint(10, 55)
        p['alt']        = random.randint(8, 55)
        p['alp']        = random.randint(40, 120)
        p['tbili']      = round(random.uniform(0.2, 1.5), 2)
        p['albumin']    = round(random.uniform(3.2, 5.0), 1)

        flipi = sum([
            p['age'] > 60,
            p['ann_arbor_stage'] in ('III', 'IV'),
            p['hemoglobin'] < 12.0,
            p['nodal_sites'] > 4,
            p['ldh_elevated'],
        ])
        p['flipi_score'] = flipi
        p['flipi_risk']  = 'low' if flipi <= 1 else ('intermediate' if flipi == 2 else 'high')

        gelf_met = (p['bulky_disease'] or p['b_symptoms'] or p['nodal_sites'] >= 3 or
                    p['bone_marrow_involvement'] or p['ldh_elevated'] or p['b2m'] > 3.0)
        p['gelf_criteria'] = 'meets GELF criteria' if gelf_met else 'does not meet GELF criteria'

        ecog_w = {'I': [55, 35, 8, 2, 0], 'II': [45, 40, 12, 3, 0],
                  'III': [35, 42, 18, 4, 1], 'IV': [25, 40, 25, 8, 2]}
        p['ecog'] = _wc([0, 1, 2, 3, 4], ecog_w[p['ann_arbor_stage']])
        p['kps']  = max(20, 100 - p['ecog'] * 20)

        waw_eligible = (flipi <= 1 and not p['b_symptoms'] and not p['bulky_disease']
                        and p['grade'] <= 2)
        p['watch_and_wait'] = waw_eligible and (random.random() < self.watch_wait_ratio)

        if p['watch_and_wait']:
            p['prior_lines'] = 0
        elif p['flipi_risk'] == 'high':
            p['prior_lines'] = _wc([1, 2, 3, 4, 5], [20, 28, 25, 17, 10])
        elif p['flipi_risk'] == 'intermediate':
            p['prior_lines'] = _wc([1, 2, 3, 4, 5], [35, 30, 20, 10, 5])
        else:
            p['prior_lines'] = _wc([1, 2, 3, 4, 5], [55, 25, 12, 5, 3])

        p['maintenance_rituximab'] = (not p['watch_and_wait']) and (random.random() < 0.50)
        transformation_prob = min(0.25, p['prior_lines'] * 0.04)
        p['transformed'] = (not p['watch_and_wait']) and (random.random() < transformation_prob)

        return p

    # ------------------------------------------------------------------
    # FHIR resources
    # ------------------------------------------------------------------

    def _patient_resource(self, p):
        birth_year = date.today().year - p['age']
        birth_date = f"{birth_year}-{random.randint(1,12):02d}-{random.randint(1,28):02d}"
        base = 'http://ctomop.io/fhir/StructureDefinition/'
        return {
            'resourceType': 'Patient',
            'id': p['id'],
            'name': [{'use': 'official', 'family': p['last_name'], 'given': [p['first_name']]}],
            'gender': p['gender'],
            'birthDate': birth_date,
            'address': [{
                'use': 'home', 'type': 'both',
                'line': [f"{random.randint(100, 9999)} {random.choice(['Main','Oak','Maple','Cedar'])} St"],
                'city': p['city'], 'state': p['state'],
                'postalCode': f"{random.randint(10000, 99999)}",
                'country': 'United States',
            }],
            'telecom': [{'system': 'phone',
                         'value': f"+1-555-{random.randint(100,999)}-{random.randint(1000,9999)}",
                         'use': 'home'}],
            'extension': [
                {'url': f'{base}ethnicity',                 'valueString':  p['ethnicity']},
                {'url': f'{base}bodyWeight',                'valueQuantity': {'value': p['weight'], 'unit': 'kg'}},
                {'url': f'{base}bodyHeight',                'valueQuantity': {'value': p['height'], 'unit': 'cm'}},
                {'url': f'{base}ecog-performance-status',   'valueInteger': p['ecog']},
                {'url': f'{base}karnofsky-score',           'valueInteger': p['kps']},
                {'url': f'{base}systolic-bp',               'valueQuantity': {'value': p['systolic_bp'], 'unit': 'mmHg'}},
                {'url': f'{base}diastolic-bp',              'valueQuantity': {'value': p['diastolic_bp'], 'unit': 'mmHg'}},
                {'url': f'{base}heartRate',                 'valueQuantity': {'value': p['heart_rate'], 'unit': 'beats/min'}},
                {'url': f'{base}fl-tumor-grade',            'valueInteger': p['grade']},
                {'url': f'{base}fl-flipi-score',            'valueInteger': p['flipi_score']},
                {'url': f'{base}fl-flipi-risk-category',    'valueString':  p['flipi_risk']},
                {'url': f'{base}fl-gelf-criteria',          'valueString':  p['gelf_criteria']},
                {'url': f'{base}fl-bone-marrow-involvement','valueBoolean': p['bone_marrow_involvement']},
                {'url': f'{base}fl-b-symptoms',             'valueBoolean': p['b_symptoms']},
                {'url': f'{base}fl-bulky-disease',          'valueBoolean': p['bulky_disease']},
                {'url': f'{base}fl-nodal-sites',            'valueInteger': p['nodal_sites']},
                {'url': f'{base}fl-watch-and-wait',         'valueBoolean': p['watch_and_wait']},
                {'url': f'{base}fl-transformed',            'valueBoolean': p['transformed']},
            ],
        }

    def _condition_resource(self, p, diag_date):
        stage_suffix = p['ann_arbor_stage'] + ('B' if p['b_symptoms'] else '')
        return {
            'resourceType': 'Condition',
            'id': f"cond-fl-{p['id']}",
            'clinicalStatus': {'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/condition-clinical', 'code': 'active'}]},
            'verificationStatus': {'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/condition-ver-status', 'code': 'confirmed'}]},
            'category': [{'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/condition-category', 'code': 'encounter-diagnosis'}]}],
            'code': {
                'coding': [
                    {'system': 'http://snomed.info/sct',        'code': '413448000', 'display': 'Follicular non-Hodgkin lymphoma'},
                    {'system': 'http://hl7.org/fhir/sid/icd-10-cm', 'code': 'C82.90', 'display': 'Follicular lymphoma, unspecified'},
                ],
                'text': 'Follicular Lymphoma',
            },
            'subject':       {'reference': f"Patient/{p['id']}"},
            'onsetDateTime':  diag_date.strftime('%Y-%m-%d'),
            'recordedDate':   diag_date.strftime('%Y-%m-%d'),
            'stage': [{
                'summary': {
                    'coding': [{'system': 'http://cancerstaging.org', 'code': f"AnnArbor{p['ann_arbor_stage']}"}],
                    'text': f"Follicular Lymphoma Ann Arbor Stage {stage_suffix}",
                },
                'type': {'coding': [{'system': 'http://snomed.info/sct', 'code': '260998006', 'display': 'Ann Arbor staging'}]},
            }],
            'note': [{'text': (
                f"Ann Arbor {stage_suffix}, Grade {p['grade']}{'b' if p['grade_3b'] else ''}, "
                f"FLIPI {p['flipi_score']} ({p['flipi_risk']}), "
                f"GELF: {p['gelf_criteria']}, nodal sites: {p['nodal_sites']}, "
                f"BM: {'positive' if p['bone_marrow_involvement'] else 'negative'}, "
                f"prior lines: {p['prior_lines']}"
            )}],
        }

    # ------------------------------------------------------------------
    # Observations
    # ------------------------------------------------------------------

    def _obs(self, pid, obs_id, loinc, display, category, date_str, value_type, value, unit=None):
        resource = {
            'resourceType': 'Observation', 'id': obs_id, 'status': 'final',
            'category': [{'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/observation-category', 'code': category}]}],
            'code': {'coding': [{'system': 'http://loinc.org', 'code': loinc, 'display': display}], 'text': display},
            'subject': {'reference': f'Patient/{pid}'},
            'effectiveDateTime': date_str,
        }
        if value_type == 'quantity':
            resource['valueQuantity'] = {'value': value, 'unit': unit, 'system': 'http://unitsofmeasure.org', 'code': unit}
        elif value_type == 'string':
            resource['valueString'] = value
        elif value_type == 'boolean':
            resource['valueBoolean'] = value
        elif value_type == 'integer':
            resource['valueInteger'] = value
        return resource

    def _q_obs(self, pid, key, loinc, display, value, unit, date_str, category='laboratory'):
        return self._obs(pid, f"obs-{pid}-{key}", loinc, display, category, date_str, 'quantity', value, unit)

    def _fl_labs(self, p, lab_date):
        pid, dt = p['id'], lab_date.strftime('%Y-%m-%d')
        return [self._q_obs(pid, key, loinc, disp, val, unit, dt)
                for key, loinc, disp, val, unit in [
                    ('hgb',        _L['hgb'],       'Hemoglobin',          p['hemoglobin'],  'g/dL'),
                    ('plt',        _L['plt'],       'Platelet count',       p['platelets'],   '10*3/uL'),
                    ('wbc',        _L['wbc'],       'WBC',                  p['wbc'],         '10*3/uL'),
                    ('rbc',        _L['rbc'],       'RBC',                  p['rbc'],         '10*6/uL'),
                    ('anc',        _L['anc'],       'ANC',                  p['anc'],         '10*3/uL'),
                    ('calcium',    _L['calcium'],   'Serum calcium',        p['calcium'],     'mg/dL'),
                    ('creatinine', _L['creatinine'],'Serum creatinine',     p['creatinine'],  'mg/dL'),
                    ('egfr',       _L['egfr'],      'eGFR',                 p['egfr'],        'mL/min/1.73m2'),
                    ('bun',        _L['bun'],       'BUN',                  p['bun'],         'mg/dL'),
                    ('sodium',     _L['sodium'],    'Sodium',               p['sodium'],      'mmol/L'),
                    ('potassium',  _L['potassium'], 'Potassium',            p['potassium'],   'mmol/L'),
                    ('ast',        _L['ast'],       'AST',                  p['ast'],         'U/L'),
                    ('alt',        _L['alt'],       'ALT',                  p['alt'],         'U/L'),
                    ('alp',        _L['alp'],       'Alkaline phosphatase', p['alp'],         'U/L'),
                    ('tbili',      _L['tbili'],     'Total bilirubin',      p['tbili'],       'mg/dL'),
                    ('albumin',    _L['albumin'],   'Albumin',              p['albumin'],     'g/dL'),
                    ('ldh',        _L['ldh'],       'LDH',                  p['ldh'],         'U/L'),
                    ('b2m',        _L['b2m'],       'Beta-2 microglobulin', p['b2m'],         'mg/L'),
                ]]

    def _performance_obs(self, p, lab_date):
        pid, dt = p['id'], lab_date.strftime('%Y-%m-%d')
        return [
            self._obs(pid, f"obs-{pid}-ecog", _L['ecog'], 'ECOG Performance Status', 'survey', dt, 'integer', p['ecog']),
            self._obs(pid, f"obs-{pid}-kps",  _L['kps'],  'Karnofsky Performance Status', 'survey', dt, 'integer', p['kps']),
            self._obs(pid, f"obs-{pid}-bp-sys", '8480-6', 'Systolic blood pressure', 'vital-signs', dt, 'quantity', p['systolic_bp'], 'mmHg'),
            self._obs(pid, f"obs-{pid}-bp-dia", '8462-4', 'Diastolic blood pressure', 'vital-signs', dt, 'quantity', p['diastolic_bp'], 'mmHg'),
        ]

    def _fl_specific_obs(self, p, diag_date):
        pid, dt = p['id'], diag_date.strftime('%Y-%m-%d')
        grade_text = f"Grade {p['grade']}{'b' if p['grade_3b'] else 'a' if p['grade'] == 3 else ''}"
        return [
            self._q_obs(pid, 'bm-b-cells', _L['bm_b_cells'],
                        'Clonal B lymphocytes in bone marrow biopsy (%)', p['bm_b_cells_pct'], '%', dt),
            self._obs(pid, f"obs-{pid}-prior-lines", '21861-0', 'Prior lines of therapy', 'laboratory', dt, 'integer', p['prior_lines']),
            self._obs(pid, f"obs-{pid}-fl-grade", '44648-4', 'Histologic grade', 'laboratory', dt, 'string', grade_text),
            self._obs(pid, f"obs-{pid}-fl-transformed", 'fl-transformed-dlbcl', 'Histologic transformation to DLBCL', 'laboratory', dt, 'boolean', p['transformed']),
            self._obs(pid, f"obs-{pid}-flipi", 'LP95826-0', 'FLIPI score', 'survey', dt, 'integer', p['flipi_score']),
            self._obs(pid, f"obs-{pid}-nodal-sites", '21912-1', 'Number of involved nodal sites', 'laboratory', dt, 'integer', p['nodal_sites']),
        ]

    # ------------------------------------------------------------------
    # Therapy resources (MedicationStatement + Procedure for radiation)
    # ------------------------------------------------------------------

    def _therapy_resources(self, p, diag_date):
        resources = []
        if p['prior_lines'] == 0:
            return resources
        line_start = diag_date + timedelta(days=random.randint(14, 60))
        for line_num in range(1, p['prior_lines'] + 1):
            is_last = line_num == p['prior_lines']
            regimen_list = self._first_line_regimens if line_num == 1 else self._later_line_regimens
            name, drugs, concept_id = _pick_regimen(regimen_list, early_stage=p['early_stage'])
            duration_days = random.randint(84, 365)
            line_end  = line_start + timedelta(days=duration_days)
            outcome   = _wc(_OUTCOMES, _OUTCOME_W_LAST if is_last else _OUTCOME_W_PREV)
            start_str, end_str = line_start.strftime('%Y-%m-%d'), line_end.strftime('%Y-%m-%d')

            if name in _RADIATION_REGIMENS:
                resources.append(self._radiation_procedure(p, name, line_num, start_str, end_str, outcome))
                # Rituximab + ISRT/IFRT: also emit a MedicationStatement for rituximab
                if 'rituximab' in drugs:
                    resources.append(self._drug_med_statement(
                        p, 'rituximab', line_num, is_last, start_str, end_str,
                        partof=f"proc-rt-{p['id']}-line{line_num}",
                    ))
            else:
                regimen_id = f"med-{p['id']}-line{line_num}-regimen"
                codings = [{'system': 'http://ctomop.io/fhir/fl-regimen', 'code': name}]
                if concept_id:
                    codings.append({
                        'system': 'http://ohdsi.org/omop/HemOnc',
                        'code': str(concept_id),
                        'display': name,
                    })
                resources.append({
                    'resourceType': 'MedicationStatement',
                    'id': regimen_id,
                    'status': 'completed' if not is_last else 'active',
                    'medicationCodeableConcept': {'coding': codings, 'text': name},
                    'subject': {'reference': f"Patient/{p['id']}"},
                    'effectivePeriod': {'start': start_str, 'end': end_str},
                    'extension': [
                        {'url': 'http://ctomop.io/fhir/StructureDefinition/therapy-line', 'valueInteger': line_num},
                        {'url': 'http://ctomop.io/fhir/StructureDefinition/therapy-outcome', 'valueString': outcome},
                    ],
                    'note': [{'text': f"Line {line_num}: {name} — {outcome}"}],
                })
                for drug_key in drugs:
                    if drug_key not in _DRUG_INFO or _DRUG_INFO[drug_key] is None:
                        continue
                    resources.append(self._drug_med_statement(
                        p, drug_key, line_num, is_last, start_str, end_str, partof=regimen_id,
                    ))

            line_start = line_end + timedelta(days=random.randint(28, 84))
        return resources

    def _drug_med_statement(self, p, drug_key, line_num, is_last, start_str, end_str, partof=None):
        if drug_key not in _DRUG_INFO or _DRUG_INFO[drug_key] is None:
            raise ValueError(f"_drug_med_statement called with invalid key '{drug_key}'")
        rxcui, display = _DRUG_INFO[drug_key]
        resource = {
            'resourceType': 'MedicationStatement',
            'id': f"med-{p['id']}-line{line_num}-{drug_key}",
            'status': 'completed' if not is_last else 'active',
            'medicationCodeableConcept': {
                'coding': [{'system': 'http://www.nlm.nih.gov/research/umls/rxnorm', 'code': rxcui, 'display': display}],
                'text': display,
            },
            'subject': {'reference': f"Patient/{p['id']}"},
            'effectivePeriod': {'start': start_str, 'end': end_str},
            'extension': [{'url': 'http://ctomop.io/fhir/StructureDefinition/therapy-line', 'valueInteger': line_num}],
        }
        if partof:
            resource['partOf'] = [{'reference': f"MedicationStatement/{partof}"}]
        return resource

    def _radiation_procedure(self, p, regimen_name, line_num, start_str, end_str, outcome):
        """FHIR Procedure for ISRT/IFRT radiotherapy lines."""
        is_isrt = 'ISRT' in regimen_name
        snomed_code    = '108290001' if is_isrt else '33195004'
        snomed_display = 'Radiation oncology AND/OR radiotherapy' if is_isrt else 'Involved field radiotherapy'
        return {
            'resourceType': 'Procedure',
            'id': f"proc-rt-{p['id']}-line{line_num}",
            'status': 'completed',
            'category': {'coding': [{'system': 'http://snomed.info/sct', 'code': '367336001', 'display': 'Radiation oncology'}]},
            'code': {
                'coding': [{'system': 'http://snomed.info/sct', 'code': snomed_code, 'display': snomed_display}],
                'text': regimen_name,
            },
            'subject': {'reference': f"Patient/{p['id']}"},
            'performedPeriod': {'start': start_str, 'end': end_str},
            'extension': [
                {'url': 'http://ctomop.io/fhir/StructureDefinition/therapy-line', 'valueInteger': line_num},
                {'url': 'http://ctomop.io/fhir/StructureDefinition/therapy-outcome', 'valueString': outcome},
            ],
            'note': [{'text': f"Line {line_num}: {regimen_name} — {outcome}"}],
        }

    def _maintenance_rituximab(self, p, diag_date):
        maint_start = diag_date + timedelta(days=random.randint(180, 300))
        maint_end   = maint_start + timedelta(days=random.randint(540, 730))
        return {
            'resourceType': 'MedicationStatement',
            'id': f"med-{p['id']}-maintenance-rituximab",
            'status': 'completed',
            'medicationCodeableConcept': {
                'coding': [{'system': 'http://www.nlm.nih.gov/research/umls/rxnorm', 'code': '121191', 'display': 'Rituximab (Rituxan)'}],
                'text': 'Rituximab maintenance',
            },
            'subject': {'reference': f"Patient/{p['id']}"},
            'effectivePeriod': {'start': maint_start.strftime('%Y-%m-%d'), 'end': maint_end.strftime('%Y-%m-%d')},
            'extension': [
                {'url': 'http://ctomop.io/fhir/StructureDefinition/therapy-line', 'valueInteger': 1},
                {'url': 'http://ctomop.io/fhir/StructureDefinition/fl-maintenance', 'valueBoolean': True},
            ],
            'note': [{'text': 'Rituximab maintenance post first-line induction'}],
        }

    @staticmethod
    def _random_date(year_from, year_to):
        start = datetime(year_from, 1, 1)
        end   = datetime(year_to, 12, 31)
        return start + timedelta(days=random.randint(0, (end - start).days))
