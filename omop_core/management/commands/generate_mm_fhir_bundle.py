"""
Generate FHIR R4 Bundle JSON with multiple myeloma patient data optimised for
EXACT trial matching.

Each patient includes all attributes screened by the EXACT eligibility engine:
  - Demographics (age 50-85, mixed gender, ethnicity)
  - MM diagnosis with ISS/RISS staging
  - Cytogenetic risk markers (del17p, t(4;14), t(14;16), 1q amp/gain, hyperdiploidy, del13q)
  - Disease burden labs: M-protein (serum/urine), FLC kappa/lambda, plasma cells %
  - ISS components: beta-2 microglobulin, albumin
  - CRAB criteria: haemoglobin, calcium, creatinine/CrCl/eGFR, bone lesions
  - SLiM criteria: plasma cells ≥60%, FLC ratio ≥100
  - ECOG / Karnofsky performance status, peripheral neuropathy grade
  - Prior lines of therapy (MM-specific regimens) with outcomes
  - Stem cell transplant history (ASCT/allo/tandem/pre/post/ineligible)
  - Refractory status (lenalidomide, bortezomib, daratumumab, PI, IMiD, CD38)
  - Plasma cell leukemia status
  - Measurable disease (IMWG)
  - Standard labs: CBC (ANC, WBC, RBC, Hgb, plt), CMP (Na, K, Ca, BUN, creatinine),
    LFTs (AST, ALT, ALP, bilirubin, albumin), LDH, ejection fraction

Usage:
    python manage.py generate_mm_fhir_bundle --count 100 --output data/mm_patients.json
"""

import json
import random
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from django.core.management.base import BaseCommand

from omop_core.services.lot_regimens import MYELOMA_REGIMEN_CONCEPT_IDS

# ---------------------------------------------------------------------------
# LOINC codes used for lab observations
# ---------------------------------------------------------------------------
_L = {
    'hgb':        '718-7',    # Hemoglobin [Mass/vol] in Blood
    'plt':        '777-3',    # Platelets [#/vol] in Blood
    'wbc':        '6690-2',   # Leukocytes [#/vol] in Blood
    'rbc':        '789-8',    # Erythrocytes [#/vol] in Blood
    'anc':        '26499-4',  # Neutrophils [#/vol] in Blood
    'calcium':    '17861-6',  # Calcium [Mass/vol] in Serum
    'creatinine': '2160-0',   # Creatinine [Mass/vol] in Serum
    'crcl':       '2164-2',   # Creatinine renal clearance in 24h Urine
    'egfr':       '69405-9',  # GFR/1.73sq M.predicted
    'bun':        '3094-0',   # BUN [Mass/vol] in Serum
    'sodium':     '2951-2',   # Sodium [Moles/vol] in Serum
    'potassium':  '2823-3',   # Potassium [Moles/vol] in Serum
    'ast':        '1920-8',   # AST [Enzymatic activity/vol] in Serum
    'alt':        '1742-6',   # ALT [Enzymatic activity/vol] in Serum
    'alp':        '6768-6',   # Alkaline phosphatase [Enzymatic activity/vol]
    'tbili':      '1975-2',   # Bilirubin.total [Mass/vol] in Serum
    'dbili':      '1968-7',   # Bilirubin.direct [Mass/vol] in Serum
    'albumin':    '1751-7',   # Albumin [Mass/vol] in Serum
    'ldh':        '2532-0',   # LDH [Enzymatic activity/vol] in Serum
    'b2m':        '1952-1',   # Beta-2 microglobulin [Mass/vol] in Serum
    'serum_m':    '51435-6',  # Protein M-spike [Mass/vol] in Serum
    'urine_m':    '32730-5',  # Protein M-spike [Mass/time] in 24h Urine
    'kflc':       '33944-8',  # Kappa free light chains [Mass/vol] in Serum
    'lflc':       '33945-5',  # Lambda free light chains [Mass/vol] in Serum
    'bmpc':       '26098-4',  # Plasma cells [%] in Bone marrow
    'ef':         '8806-2',   # Left ventricular Ejection fraction by US
    'ecog':       '89247-1',  # ECOG Performance Status score
    'kps':        '89243-0',  # Karnofsky Performance Status score
}

# ---------------------------------------------------------------------------
# MM treatment regimen catalogue
# ---------------------------------------------------------------------------
_EARLY_REGIMENS = [
    ('VRd',  ['bortezomib', 'lenalidomide', 'dexamethasone'],  30),
    ('Rd',   ['lenalidomide', 'dexamethasone'],                 12),
    ('KRd',  ['carfilzomib', 'lenalidomide', 'dexamethasone'],  12),
    ('VCd',  ['bortezomib', 'cyclophosphamide', 'dexamethasone'], 8),
    ('DRd',  ['daratumumab', 'lenalidomide', 'dexamethasone'],  15),
    ('DKRd', ['daratumumab', 'carfilzomib', 'lenalidomide', 'dexamethasone'], 8),
    ('DVd',  ['daratumumab', 'bortezomib', 'dexamethasone'],    8),
    ('IsaVRd', ['isatuximab', 'bortezomib', 'lenalidomide', 'dexamethasone'], 4),  # Isa-RVd (HemOnc 37557069)
    ('Td',   ['thalidomide', 'dexamethasone'],                  3),
]

_LATER_REGIMENS = [
    ('Pd',         ['pomalidomide', 'dexamethasone'],                         18),
    ('KPd',        ['carfilzomib', 'pomalidomide', 'dexamethasone'],           10),
    ('DPd',        ['daratumumab', 'pomalidomide', 'dexamethasone'],           10),
    ('IsaPd',      ['isatuximab', 'pomalidomide', 'dexamethasone'],             5),
    ('EloPd',      ['elotuzumab', 'pomalidomide', 'dexamethasone'],             4),
    ('SVd',        ['selinexor', 'bortezomib', 'dexamethasone'],                8),  # SVd absorbs removed Sd weight
    ('Teclistamab',['teclistamab'],                                             4),
    ('Belantamab', ['belantamab'],                                              3),
    ('Ciltacabtagene', ['ciltacabtagene'],                                      3),
    ('Idecabtagene',   ['idecabtagene'],                                        2),
    ('DKRd',       ['daratumumab', 'carfilzomib', 'lenalidomide', 'dexamethasone'], 6),
    ('KRd',        ['carfilzomib', 'lenalidomide', 'dexamethasone'],            5),
    ('DVd',        ['daratumumab', 'bortezomib', 'dexamethasone'],              5),
    ('Melphalan+pred', ['melphalan', 'prednisone'],                             4),  # absorbed VPd + VenetoBd weight
    ('IxaRd',      ['ixazomib', 'lenalidomide', 'dexamethasone'],               6),
]

_DRUG_INFO = {
    'bortezomib':    ('387544',   'Bortezomib (Velcade)'),
    'carfilzomib':   ('1279374',  'Carfilzomib (Kyprolis)'),
    'ixazomib':      ('1855505',  'Ixazomib (Ninlaro)'),
    'lenalidomide':  ('337535',   'Lenalidomide (Revlimid)'),
    'pomalidomide':  ('1295214',  'Pomalidomide (Pomalyst)'),
    'thalidomide':   ('10237',    'Thalidomide (Thalomid)'),
    'daratumumab':   ('1811191',  'Daratumumab (Darzalex)'),
    'isatuximab':    ('2180126',  'Isatuximab (Sarclisa)'),
    'elotuzumab':    ('1855498',  'Elotuzumab (Empliciti)'),
    'dexamethasone': ('3264',     'Dexamethasone'),
    'prednisone':    ('8638',     'Prednisone'),
    'cyclophosphamide': ('3002',  'Cyclophosphamide'),
    'melphalan':     ('6862',     'Melphalan (Alkeran)'),
    'venetoclax':    ('1860484',  'Venetoclax (Venclexta)'),
    'selinexor':     ('2177028',  'Selinexor (Xpovio)'),
    'ciltacabtagene':('2445279',  'Ciltacabtagene autoleucel (Carvykti)'),
    'idecabtagene':  ('2370313',  'Idecabtagene vicleucel (Abecma)'),
    'teclistamab':   ('2574521',  'Teclistamab (Tecvayli)'),
    'belantamab':    ('2372498',  'Belantamab mafodotin (Blenrep)'),
}

_HIGH_RISK_CYTO = {'del17p', 't(4;14)', 't(14;16)', 't(14;20)', '1q_amp'}

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

_SCT_VALUES = [
    'completedASCT', 'eligibleForASCT', 'ineligibleForASCT',
    'preASCT', 'postASCT', 'neverReceivedSCT', 'sctIneligible',
    'relapsedPostASCT', 'completedTandemSCT', 'completedAllogeneicSCT',
]

# Maps the old internal sct_history code to the new StemCellTransplant vocabulary
# strings (comma-joined for the FHIR valueString, split by the upload handler).
_SCT_HISTORY_TO_VOCAB = {
    'completedASCT':          ['autologous SCT'],
    'relapsedPostASCT':       ['autologous SCT'],
    'postASCT':               ['autologous SCT'],
    'completedTandemSCT':     ['autologous SCT', 'tandem SCT'],
    'completedAllogeneicSCT': ['allogeneic SCT'],
    # Candidates/ineligible have no completed transplant
    'preASCT':          [],
    'eligibleForASCT':  [],
    'ineligibleForASCT': [],
    'neverReceivedSCT': [],
    'sctIneligible':    [],
}

# Maps old sct_history code to SctEligibility vocab strings.
_SCT_HISTORY_TO_ELIGIBILITY = {
    'completedASCT':          ['eligible for autologous SCT'],
    'relapsedPostASCT':       ['eligible for autologous SCT'],
    'postASCT':               ['eligible for autologous SCT'],
    'completedTandemSCT':     ['eligible for autologous SCT'],
    'completedAllogeneicSCT': ['eligible for allogeneic SCT'],
    'preASCT':                ['eligible for autologous SCT'],
    'eligibleForASCT':        ['eligible for autologous SCT'],
    'ineligibleForASCT':      ['ineligible for autologous SCT'],
    'neverReceivedSCT':       ['ineligible for autologous SCT'],
    'sctIneligible':          ['ineligible for autologous SCT'],
}

_ETHNICITIES = ['Caucasian/White', 'Hispanic/Latino', 'Black/African-American', 'Asian', 'Native American']
_ETHNICITY_WEIGHTS = [65, 12, 15, 6, 2]  # Approximate US MM demographics

_OUTCOMES = ['Progressive Disease', 'Stable Disease', 'Partial Response', 'Very Good Partial Response', 'Complete Response']
_OUTCOME_WEIGHTS_PENULTIMATE = [60, 15, 18, 5, 2]   # Lines that ended in progression
_OUTCOME_WEIGHTS_LAST = [15, 20, 35, 20, 10]          # Current/most recent line


def _weighted_choice(items, weights):
    return random.choices(items, weights=weights, k=1)[0]


def _reg(regimen_list):
    names = [r[0] for r in regimen_list]
    drugs_list = [r[1] for r in regimen_list]
    weights = [r[2] for r in regimen_list]
    idx = random.choices(range(len(names)), weights=weights, k=1)[0]
    return names[idx], drugs_list[idx]


class Command(BaseCommand):
    help = 'Generate FHIR R4 Bundle with multiple myeloma patients for EXACT trial matching'

    def add_arguments(self, parser):
        parser.add_argument('--count', type=int, default=100,
                            help='Number of patients to generate (default: 100)')
        parser.add_argument('--output', type=str, default='data/mm_patients_fhir.json',
                            help='Output file path')
        parser.add_argument('--seed', type=int, default=None,
                            help='Random seed for reproducibility (default: random each run)')
        parser.add_argument('--rrmm-ratio', type=float, default=0.80, dest='rrmm_ratio',
                            help='Fraction of patients with ≥1 prior line (RRMM; default 0.80)')

    def handle(self, *args, **options):
        count = options['count']
        output_path = options['output']
        random.seed(options['seed'])
        self.rrmm_ratio = options['rrmm_ratio']

        self.stdout.write(f'Generating {count} multiple myeloma patients…')
        bundle = self._generate_bundle(count)

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, 'w') as f:
            json.dump(bundle, f, indent=2)

        n_entries = len(bundle['entry'])
        self.stdout.write(self.style.SUCCESS(
            f'✓ {count} patients, {n_entries} FHIR resources → {out}'
        ))

    # ------------------------------------------------------------------
    # Bundle assembly
    # ------------------------------------------------------------------

    def _generate_bundle(self, count):
        bundle = {'resourceType': 'Bundle', 'type': 'collection', 'entry': []}
        for i in range(1, count + 1):
            self._add_patient(bundle, i)
        return bundle

    def _add_patient(self, bundle, pid):
        p = self._profile(pid)
        diag_date = self._random_date(2015, 2023)
        lab_date = datetime.now() - timedelta(days=random.randint(1, 60))

        def _entry(resource):
            rt = resource['resourceType']
            rid = resource['id']
            bundle['entry'].append({
                'fullUrl': f'http://example.org/{rt}/{rid}',
                'resource': resource,
            })

        _entry(self._patient_resource(p))
        _entry(self._condition_resource(p, diag_date))

        for obs in self._mm_labs(p, lab_date):
            _entry(obs)

        for obs in self._performance_obs(p, lab_date):
            _entry(obs)

        for obs in self._cytogenetics_obs(p, diag_date):
            _entry(obs)

        for obs in self._disease_burden_obs(p, diag_date):
            _entry(obs)

        _entry(self._bone_marrow_obs(p, diag_date))

        if p['sct_history'] in ('completedASCT', 'relapsedPostASCT', 'completedTandemSCT',
                                 'completedAllogeneicSCT', 'postASCT'):
            _entry(self._asct_procedure(p, diag_date))

        for med in self._therapy_medications(p, diag_date):
            _entry(med)

        for obs in self._mm_specific_obs(p, diag_date):
            _entry(obs)

    # ------------------------------------------------------------------
    # Patient profile — all correlated clinical values
    # ------------------------------------------------------------------

    def _profile(self, pid):
        p = {'id': str(pid)}

        # Demographics
        p['gender'] = _weighted_choice(['male', 'female'], [55, 45])
        p['age'] = int(random.triangular(45, 85, 68))
        p['ethnicity'] = _weighted_choice(_ETHNICITIES, _ETHNICITY_WEIGHTS)
        p['first_name'] = random.choice(_FIRST_NAMES)
        p['last_name'] = random.choice(_LAST_NAMES)
        city, state = random.choice(_US_LOCATIONS)
        p['city'], p['state'] = city, state

        if p['gender'] == 'male':
            p['weight'] = round(random.uniform(62, 105), 1)
            p['height'] = round(random.uniform(165, 187), 1)
        else:
            p['weight'] = round(random.uniform(50, 92), 1)
            p['height'] = round(random.uniform(153, 175), 1)
        p['bmi'] = round(p['weight'] / (p['height'] / 100) ** 2, 1)
        p['systolic_bp'] = random.randint(105, 160)
        p['diastolic_bp'] = random.randint(60, 95)
        p['heart_rate'] = random.randint(58, 105)

        # Cytogenetic risk
        cyto = []
        if random.random() < 0.10: cyto.append('del17p')
        if random.random() < 0.14: cyto.append('t(4;14)')
        if random.random() < 0.05: cyto.append('t(14;16)')
        if random.random() < 0.02: cyto.append('t(14;20)')
        if random.random() < 0.10:
            cyto.append('1q_amp')
        elif random.random() < 0.35:
            cyto.append('1q_gain')
        if random.random() < 0.50: cyto.append('hyperdiploidy')
        if random.random() < 0.45: cyto.append('del13q')
        p['cytogenetics'] = cyto
        p['high_risk_cyto'] = bool(cyto and _HIGH_RISK_CYTO & set(cyto))

        # ISS stage (drives lab ranges)
        p['iss_stage'] = _weighted_choice(['I', 'II', 'III'], [33, 37, 30])

        # Beta-2 microglobulin + albumin (ISS components)
        if p['iss_stage'] == 'I':
            p['b2m'] = round(random.uniform(1.0, 3.4), 2)
            p['albumin'] = round(random.uniform(3.5, 5.0), 1)
        elif p['iss_stage'] == 'II':
            if random.random() < 0.70:
                p['b2m'] = round(random.uniform(3.5, 5.4), 2)
                p['albumin'] = round(random.uniform(3.0, 5.0), 1)
            else:
                p['b2m'] = round(random.uniform(1.5, 3.4), 2)
                p['albumin'] = round(random.uniform(2.5, 3.4), 1)
        else:
            p['b2m'] = round(random.uniform(5.5, 15.0), 2)
            p['albumin'] = round(random.uniform(2.0, 4.2), 1)

        # LDH (elevated in ~15% overall, more in ISS III / high-risk)
        ldh_elev_prob = 0.30 if (p['high_risk_cyto'] or p['iss_stage'] == 'III') else 0.10
        if random.random() < ldh_elev_prob:
            p['ldh'] = random.randint(226, 520)
        else:
            p['ldh'] = random.randint(120, 225)

        # RISS stage
        ldh_high = p['ldh'] > 225
        if p['iss_stage'] == 'I' and not p['high_risk_cyto'] and not ldh_high:
            p['riss_stage'] = 'I'
        elif p['iss_stage'] == 'III' or (p['high_risk_cyto'] and ldh_high):
            p['riss_stage'] = 'III'
        else:
            p['riss_stage'] = 'II'

        # Disease progression status
        p['progression'] = _weighted_choice(['active', 'smoldering'], [93, 7])

        # Prior lines of therapy — always ≥1 so every patient has therapy data
        p['prior_lines'] = _weighted_choice([1, 2, 3, 4, 5], [30, 30, 20, 12, 8])

        # SCT history correlated with prior lines
        nl = p['prior_lines']
        if nl == 1:
            sct = _weighted_choice(['completedASCT', 'eligibleForASCT', 'ineligibleForASCT', 'neverReceivedSCT'],
                                   [55, 15, 20, 10])
        elif nl == 2:
            sct = _weighted_choice(['completedASCT', 'relapsedPostASCT', 'postASCT', 'ineligibleForASCT', 'sctIneligible'],
                                   [40, 25, 10, 15, 10])
        else:
            sct = _weighted_choice(['relapsedPostASCT', 'completedASCT', 'ineligibleForASCT', 'sctIneligible', 'completedTandemSCT'],
                                   [35, 25, 20, 10, 10])
        p['sct_history'] = sct
        p['sct_types']       = _SCT_HISTORY_TO_VOCAB.get(sct, [])
        p['sct_eligibility'] = _SCT_HISTORY_TO_ELIGIBILITY.get(sct, [])
        # Only patients who actually completed a transplant get a date
        if p['sct_types']:
            days_ago = random.randint(365, 365 * 8)
            p['sct_date'] = (datetime.today() - timedelta(days=days_ago)).strftime('%Y-%m-%d')
        else:
            p['sct_date'] = None

        # Refractory status
        refractory = []
        if nl >= 2:
            if random.random() < 0.78: refractory.append('lenalidomide')
            if random.random() < 0.65: refractory.append('bortezomib')
            if nl >= 3 and random.random() < 0.45: refractory.append('daratumumab')
        elif nl == 1:
            if random.random() < 0.32: refractory.append('lenalidomide')
            if random.random() < 0.22: refractory.append('bortezomib')
        p['refractory'] = refractory

        # CRAB criteria
        anemia_prob = {'I': 0.22, 'II': 0.42, 'III': 0.65}[p['iss_stage']]
        p['hemoglobin'] = (round(random.uniform(6.5, 9.9), 1) if random.random() < anemia_prob
                          else round(random.uniform(10.0, 14.5), 1))
        p['crab_anemia'] = p['hemoglobin'] < 10.0

        ca_prob = {'I': 0.07, 'II': 0.12, 'III': 0.20}[p['iss_stage']]
        p['calcium'] = (round(random.uniform(11.0, 13.8), 1) if random.random() < ca_prob
                       else round(random.uniform(8.4, 10.8), 1))
        p['crab_calcium'] = p['calcium'] > 11.0

        renal_prob = {'I': 0.10, 'II': 0.20, 'III': 0.35}[p['iss_stage']]
        if random.random() < renal_prob:
            p['crcl'] = random.randint(15, 39)
            p['creatinine'] = round(random.uniform(2.0, 5.5), 2)
            p['egfr'] = random.randint(10, 39)
        elif random.random() < 0.25:
            p['crcl'] = random.randint(40, 59)
            p['creatinine'] = round(random.uniform(1.2, 2.0), 2)
            p['egfr'] = random.randint(40, 59)
        else:
            p['crcl'] = random.randint(60, 120)
            p['creatinine'] = round(random.uniform(0.6, 1.2), 2)
            p['egfr'] = random.randint(60, 120)
        p['crab_renal'] = p['crcl'] < 40

        bone_prob = {'I': 0.28, 'II': 0.50, 'III': 0.70}[p['iss_stage']]
        p['bone_lesions'] = random.random() < bone_prob
        p['crab_bone'] = p['bone_lesions']
        p['meets_crab'] = any([p['crab_anemia'], p['crab_calcium'], p['crab_renal'], p['crab_bone']])

        # M-protein and FLC
        disease_type = _weighted_choice(['IgG', 'IgA', 'IgM', 'light_chain_only', 'non_secretory'],
                                       [50, 20, 3, 17, 10])
        p['disease_type'] = disease_type

        if disease_type == 'non_secretory':
            p['serum_m'] = 0.0
            p['urine_m'] = 0.0
        elif disease_type == 'light_chain_only':
            p['serum_m'] = 0.0
            p['urine_m'] = round(random.uniform(200, 5000), 1)
        else:
            base_hi = {'I': 3.5, 'II': 5.5, 'III': 9.0}[p['iss_stage']]
            p['serum_m'] = round(random.uniform(0.3, base_hi), 2)
            p['urine_m'] = round(random.uniform(0, 400), 1)

        kappa_involved = random.random() < 0.50
        if disease_type == 'non_secretory':
            p['kappa_flc'] = round(random.uniform(3.3, 19.4), 1)
            p['lambda_flc'] = round(random.uniform(5.7, 26.3), 1)
        elif kappa_involved:
            p['kappa_flc'] = round(random.uniform(50, 5000), 1)
            p['lambda_flc'] = round(random.uniform(5.7, 26.3), 1)
        else:
            p['kappa_flc'] = round(random.uniform(3.3, 19.4), 1)
            p['lambda_flc'] = round(random.uniform(50, 5000), 1)

        flc_max = max(p['kappa_flc'], p['lambda_flc'])
        flc_min = min(p['kappa_flc'], p['lambda_flc']) or 0.01
        flc_ratio = flc_max / flc_min
        p['measurable_disease_imwg'] = (
            p['serum_m'] >= 0.5 or
            p['urine_m'] >= 200 or
            (flc_max >= 100 and flc_ratio >= 100)
        )
        p['meets_slim'] = False  # determined per plasma_cells below

        # Bone marrow plasma cells
        slim_prob = 0.20
        p['plasma_cells_pct'] = (random.randint(60, 95) if random.random() < slim_prob
                                  else random.randint(3, 59))
        p['meets_slim'] = p['plasma_cells_pct'] >= 60

        # ECOG / KPS
        ecog_w = {'I': [35, 45, 15, 4, 1], 'II': [25, 40, 25, 8, 2], 'III': [15, 35, 30, 15, 5]}[p['iss_stage']]
        p['ecog'] = _weighted_choice([0, 1, 2, 3, 4], ecog_w)
        p['kps'] = max(20, 100 - p['ecog'] * 20)

        # Peripheral neuropathy grade
        prior_pi = nl > 0
        p['peripheral_neuropathy_grade'] = (
            _weighted_choice([0, 1, 2, 3], [40, 35, 20, 5]) if prior_pi
            else _weighted_choice([0, 1, 2, 3], [70, 20, 8, 2])
        )

        # Plasma cell leukemia
        p['plasma_cell_leukemia'] = random.random() < 0.03

        # CBC (remaining)
        plt_low_prob = 0.30 if (p['meets_crab'] or nl > 2) else 0.10
        p['platelets'] = (random.randint(40, 149) if random.random() < plt_low_prob
                         else random.randint(150, 420))
        p['wbc'] = round(random.uniform(2.5, 9.5), 1)
        p['anc'] = round(random.uniform(0.8, 6.5), 2)
        p['rbc'] = round(random.uniform(2.3, 5.2), 2)
        p['bun'] = round(random.uniform(8, 35), 1)
        p['sodium'] = round(random.uniform(133, 145), 1)
        p['potassium'] = round(random.uniform(3.4, 5.2), 1)

        # LFTs
        p['ast'] = random.randint(10, 65)
        p['alt'] = random.randint(8, 65)
        p['alp'] = random.randint(38, 130)
        p['tbili'] = round(random.uniform(0.2, 1.8), 2)
        p['dbili'] = round(random.uniform(0.1, 0.6), 2)

        # EF
        p['ef'] = random.randint(48, 72)

        return p

    # ------------------------------------------------------------------
    # FHIR resource generators
    # ------------------------------------------------------------------

    def _patient_resource(self, p):
        birth_year = 2024 - p['age']
        birth_date = f"{birth_year}-{random.randint(1,12):02d}-{random.randint(1,28):02d}"
        gender_fhir = 'male' if p['gender'] == 'male' else 'female'

        return {
            'resourceType': 'Patient',
            'id': p['id'],
            'name': [{'use': 'official', 'family': p['last_name'], 'given': [p['first_name']]}],
            'gender': gender_fhir,
            'birthDate': birth_date,
            'address': [{
                'use': 'home', 'type': 'both',
                'line': [f"{random.randint(100, 9999)} {random.choice(['Main','Oak','Maple','Cedar'])} St"],
                'city': p['city'], 'state': p['state'],
                'postalCode': f"{random.randint(10000, 99999)}",
                'country': 'United States',
            }],
            'telecom': [{'system': 'phone', 'value': f"+1-555-{random.randint(100,999)}-{random.randint(1000,9999)}", 'use': 'home'}],
            'extension': [
                # ctomop-recognised extensions (exact URLs the FHIR importer parses)
                {'url': 'http://ctomop.io/fhir/StructureDefinition/ethnicity',
                 'valueString': p['ethnicity']},
                {'url': 'http://ctomop.io/fhir/StructureDefinition/bodyWeight',
                 'valueQuantity': {'value': p['weight'], 'unit': 'kg'}},
                {'url': 'http://ctomop.io/fhir/StructureDefinition/bodyHeight',
                 'valueQuantity': {'value': p['height'], 'unit': 'cm'}},
                {'url': 'http://ctomop.io/fhir/StructureDefinition/ecog-performance-status',
                 'valueInteger': p['ecog']},
                {'url': 'http://ctomop.io/fhir/StructureDefinition/karnofsky-score',
                 'valueInteger': p['kps']},
                {'url': 'http://ctomop.io/fhir/StructureDefinition/systolic-bp',
                 'valueQuantity': {'value': p['systolic_bp'], 'unit': 'mmHg'}},
                {'url': 'http://ctomop.io/fhir/StructureDefinition/diastolic-bp',
                 'valueQuantity': {'value': p['diastolic_bp'], 'unit': 'mmHg'}},
                {'url': 'http://ctomop.io/fhir/StructureDefinition/heartRate',
                 'valueQuantity': {'value': p['heart_rate'], 'unit': 'beats/min'}},
                # MM-specific extensions — omit entirely when empty so re-import
                # doesn't silently leave stale values in the DB (upload handler
                # treats absent extension as "no change", not "clear").
                *([{'url': 'http://ctomop.io/fhir/StructureDefinition/mm-sct-history',
                    'valueString': ','.join(p['sct_types'])}] if p['sct_types'] else []),
                *([{'url': 'http://ctomop.io/fhir/StructureDefinition/mm-sct-date',
                    'valueString': p['sct_date']}] if p['sct_date'] else []),
                *([{'url': 'http://ctomop.io/fhir/StructureDefinition/mm-sct-eligibility',
                    'valueString': ','.join(p['sct_eligibility'])}] if p['sct_eligibility'] else []),
                {'url': 'http://ctomop.io/fhir/StructureDefinition/mm-cytogenetic-markers',
                 'valueString': ','.join(p['cytogenetics']) if p['cytogenetics'] else ''},
                {'url': 'http://ctomop.io/fhir/StructureDefinition/mm-refractory-status',
                 'valueString': ','.join(p['refractory']) if p['refractory'] else ''},
                {'url': 'http://ctomop.io/fhir/StructureDefinition/mm-plasma-cell-leukemia',
                 'valueBoolean': p['plasma_cell_leukemia']},
                {'url': 'http://ctomop.io/fhir/StructureDefinition/mm-disease-progression',
                 'valueString': p['progression']},
                {'url': 'http://ctomop.io/fhir/StructureDefinition/mm-measurable-disease-imwg',
                 'valueBoolean': p['measurable_disease_imwg']},
            ],
        }

    def _condition_resource(self, p, diag_date):
        return {
            'resourceType': 'Condition',
            'id': f"cond-mm-{p['id']}",
            'clinicalStatus': {'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/condition-clinical',
                                           'code': 'active'}]},
            'verificationStatus': {'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/condition-ver-status',
                                               'code': 'confirmed'}]},
            'category': [{'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/condition-category',
                                      'code': 'encounter-diagnosis'}]}],
            'code': {
                'coding': [
                    {'system': 'http://snomed.info/sct', 'code': '55921005', 'display': 'Multiple myeloma'},
                    {'system': 'http://hl7.org/fhir/sid/icd-10-cm', 'code': 'C90.00',
                     'display': 'Multiple myeloma, not having achieved remission'},
                ],
                'text': 'Multiple Myeloma',
            },
            'subject': {'reference': f"Patient/{p['id']}"},
            'onsetDateTime': diag_date.strftime('%Y-%m-%d'),
            'recordedDate': diag_date.strftime('%Y-%m-%d'),
            'stage': [
                {
                    'summary': {
                        'coding': [{'system': 'http://cancerstaging.org', 'code': f"ISS{p['iss_stage']}"}],
                        'text': f"ISS Stage {p['iss_stage']}",
                    },
                    'type': {'coding': [{'system': 'http://snomed.info/sct', 'code': '260998006',
                                         'display': 'ISS staging'}]},
                },
                {
                    'summary': {
                        'coding': [{'system': 'http://cancerstaging.org', 'code': f"RISS{p['riss_stage']}"}],
                        'text': f"R-ISS Stage {p['riss_stage']}",
                    },
                    'type': {'coding': [{'system': 'http://snomed.info/sct', 'code': '260998006',
                                         'display': 'R-ISS staging'}]},
                },
            ],
            'note': [{'text': (
                f"ISS {p['iss_stage']}, R-ISS {p['riss_stage']}, "
                f"cytogenetics: {','.join(p['cytogenetics']) or 'standard risk'}, "
                f"progression: {p['progression']}, "
                f"prior lines: {p['prior_lines']}, "
                f"SCT: {p['sct_history']}"
            )}],
        }

    def _obs(self, pid, obs_id, loinc, display, category, date_str, value_type, value, unit=None, unit_system=None):
        """Build a minimal FHIR Observation entry (not wrapped in fullUrl yet)."""
        resource = {
            'resourceType': 'Observation',
            'id': obs_id,
            'status': 'final',
            'category': [{'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/observation-category',
                                       'code': category}]}],
            'code': {'coding': [{'system': 'http://loinc.org', 'code': loinc, 'display': display}],
                     'text': display},
            'subject': {'reference': f'Patient/{pid}'},
            'effectiveDateTime': date_str,
        }
        if value_type == 'quantity':
            resource['valueQuantity'] = {'value': value, 'unit': unit,
                                          'system': unit_system or 'http://unitsofmeasure.org', 'code': unit}
        elif value_type == 'string':
            resource['valueString'] = value
        elif value_type == 'boolean':
            resource['valueBoolean'] = value
        elif value_type == 'integer':
            resource['valueInteger'] = value
        elif value_type == 'codeable':
            resource['valueCodeableConcept'] = value
        return resource

    def _q_obs(self, pid, key, loinc, display, value, unit, date_str, category='laboratory'):
        return self._obs(pid, f"obs-{pid}-{key}", loinc, display, category, date_str, 'quantity', value, unit)

    def _mm_labs(self, p, lab_date):
        pid = p['id']
        dt = lab_date.strftime('%Y-%m-%d')
        labs = [
            ('hgb',        _L['hgb'],       'Hemoglobin',           p['hemoglobin'],  'g/dL'),
            ('plt',        _L['plt'],       'Platelet count',        p['platelets'],   '10*3/uL'),
            ('wbc',        _L['wbc'],       'WBC',                   p['wbc'],         '10*3/uL'),
            ('rbc',        _L['rbc'],       'RBC',                   p['rbc'],         '10*6/uL'),
            ('anc',        _L['anc'],       'ANC',                   p['anc'],         '10*3/uL'),
            ('calcium',    _L['calcium'],   'Serum calcium',         p['calcium'],     'mg/dL'),
            ('creatinine', _L['creatinine'],'Serum creatinine',      p['creatinine'],  'mg/dL'),
            ('crcl',       _L['crcl'],      'Creatinine clearance',  p['crcl'],        'mL/min'),
            ('egfr',       _L['egfr'],      'eGFR',                  p['egfr'],        'mL/min/1.73m2'),
            ('bun',        _L['bun'],       'BUN',                   p['bun'],         'mg/dL'),
            ('sodium',     _L['sodium'],    'Sodium',                p['sodium'],      'mmol/L'),
            ('potassium',  _L['potassium'], 'Potassium',             p['potassium'],   'mmol/L'),
            ('ast',        _L['ast'],       'AST',                   p['ast'],         'U/L'),
            ('alt',        _L['alt'],       'ALT',                   p['alt'],         'U/L'),
            ('alp',        _L['alp'],       'Alkaline phosphatase',  p['alp'],         'U/L'),
            ('tbili',      _L['tbili'],     'Total bilirubin',       p['tbili'],       'mg/dL'),
            ('dbili',      _L['dbili'],     'Direct bilirubin',      p['dbili'],       'mg/dL'),
            ('albumin',    _L['albumin'],   'Albumin',               p['albumin'],     'g/dL'),
            ('ldh',        _L['ldh'],       'LDH',                   p['ldh'],         'U/L'),
            ('b2m',        _L['b2m'],       'Beta-2 microglobulin',  p['b2m'],         'mg/L'),
            ('ef',         _L['ef'],        'Ejection fraction',     p['ef'],          '%'),
        ]
        return [self._q_obs(pid, key, loinc, disp, val, unit, dt) for key, loinc, disp, val, unit in labs]

    def _performance_obs(self, p, lab_date):
        pid = p['id']
        dt = lab_date.strftime('%Y-%m-%d')
        return [
            self._obs(pid, f"obs-{pid}-ecog", _L['ecog'], 'ECOG Performance Status', 'survey', dt, 'integer', p['ecog']),
            self._obs(pid, f"obs-{pid}-kps", _L['kps'], 'Karnofsky Performance Status', 'survey', dt, 'integer', p['kps']),
            self._obs(pid, f"obs-{pid}-pn-grade", '73643-8', 'Peripheral neuropathy grade',
                      'survey', dt, 'integer', p['peripheral_neuropathy_grade']),
            self._obs(pid, f"obs-{pid}-bp-sys", '8480-6', 'Systolic blood pressure',
                      'vital-signs', dt, 'quantity', p['systolic_bp'], 'mmHg'),
            self._obs(pid, f"obs-{pid}-bp-dia", '8462-4', 'Diastolic blood pressure',
                      'vital-signs', dt, 'quantity', p['diastolic_bp'], 'mmHg'),
        ]

    def _cytogenetics_obs(self, p, diag_date):
        pid = p['id']
        dt = diag_date.strftime('%Y-%m-%d')
        markers = p['cytogenetics']
        obs_list = []

        # Single summary observation with all markers as valueString
        summary_val = ','.join(markers) if markers else 'standard risk — no high-risk markers detected'
        obs_list.append(self._obs(
            pid, f"obs-{pid}-cytogenetics", '69548-6',
            'Cytogenomic microarray result (MM)',
            'laboratory', dt, 'string', summary_val,
        ))

        # Individual boolean observations for EXACT-relevant markers
        for marker, loinc, display in [
            ('del17p',      '72838-3', 'TP53/17p deletion'),
            ('t(4;14)',     '72842-5', 'FGFR3/IGH translocation t(4;14)'),
            ('t(14;16)',    '81250-3', 'MAF/IGH translocation t(14;16)'),
            ('1q_gain',     '81249-5', '1q21 gain/amplification'),
            ('1q_amp',      '81249-5', '1q21 amplification (≥4 copies)'),
            ('hyperdiploidy','81248-7','Hyperdiploidy'),
            ('del13q',      '72840-9', '13q deletion'),
        ]:
            # Avoid duplicate 1q obs id
            mk_id = marker.replace('(', '').replace(')', '').replace(',', '').replace('/', '-')
            obs_list.append(self._obs(
                pid, f"obs-{pid}-cyto-{mk_id}", loinc, display,
                'laboratory', dt, 'boolean', marker in markers,
            ))
        return obs_list

    def _disease_burden_obs(self, p, diag_date):
        pid = p['id']
        dt = diag_date.strftime('%Y-%m-%d')
        obs_list = [
            self._q_obs(pid, 'serum-m',  _L['serum_m'], 'M-protein serum spike', p['serum_m'], 'g/dL', dt),
            self._q_obs(pid, 'urine-m',  _L['urine_m'], 'M-protein urine 24h',   p['urine_m'], 'mg/24h', dt),
            self._q_obs(pid, 'kflc',     _L['kflc'],   'Kappa free light chains', p['kappa_flc'], 'mg/L', dt),
            self._q_obs(pid, 'lflc',     _L['lflc'],   'Lambda free light chains', p['lambda_flc'], 'mg/L', dt),
        ]
        obs_list.append(self._obs(
            pid, f"obs-{pid}-disease-type", '57905-2', 'Myeloma immunoglobulin type',
            'laboratory', dt, 'string', p['disease_type'],
        ))
        obs_list.append(self._obs(
            pid, f"obs-{pid}-bone-lesions", '24646-7', 'Bone lesions',
            'imaging', dt, 'boolean', p['bone_lesions'],
        ))
        obs_list.append(self._obs(
            pid, f"obs-{pid}-meets-crab", '89599-5', 'Meets CRAB criteria',
            'laboratory', dt, 'boolean', p['meets_crab'],
        ))
        return obs_list

    def _bone_marrow_obs(self, p, diag_date):
        return self._q_obs(
            p['id'], 'bmpc', _L['bmpc'],
            'Clonal plasma cells in bone marrow (%)',
            p['plasma_cells_pct'], '%',
            diag_date.strftime('%Y-%m-%d'),
        )

    def _asct_procedure(self, p, diag_date):
        # ASCT typically happens ~6-12 months after diagnosis in 1st or 2nd line
        offset_days = random.randint(180, 480)
        proc_date = (diag_date + timedelta(days=offset_days)).strftime('%Y-%m-%d')
        return {
            'resourceType': 'Procedure',
            'id': f"proc-asct-{p['id']}",
            'status': 'completed',
            'code': {
                'coding': [
                    {'system': 'http://snomed.info/sct', 'code': '58336002',
                     'display': 'Autologous bone marrow transplantation'},
                    {'system': 'http://snomed.info/sct', 'code': '404798000',
                     'display': 'Autologous peripheral blood stem cell transplantation'},
                ],
                'text': 'Autologous Stem Cell Transplantation (ASCT)',
            },
            'subject': {'reference': f"Patient/{p['id']}"},
            'performedDateTime': proc_date,
            'note': [{'text': f"SCT status: {p['sct_history']}"}],
        }

    def _therapy_medications(self, p, diag_date):
        """
        Generate MedicationStatement resources the ctomop FHIR importer can parse.

        Structure per therapy line:
          - One regimen-level MedicationStatement (no partOf) with the regimen name
            as medicationCodeableConcept.text, effectivePeriod, therapy-line and
            therapy-outcome extensions.  The importer uses this to populate
            first_line_therapy / second_line_therapy / later_therapies.
          - One individual-drug MedicationStatement per drug (with partOf pointing
            to the regimen entry) for OMOP DrugExposure records.
        """
        meds = []
        nl = p['prior_lines']
        if nl == 0:
            return meds

        line_start = diag_date + timedelta(days=random.randint(14, 60))

        for line_num in range(1, nl + 1):
            is_last = line_num == nl

            if line_num <= 2:
                name, drugs = _reg(_EARLY_REGIMENS)
            else:
                name, drugs = _reg(_LATER_REGIMENS)

            duration_days = random.randint(84, 365)
            line_end = line_start + timedelta(days=duration_days)

            outcome_weights = _OUTCOME_WEIGHTS_LAST if is_last else _OUTCOME_WEIGHTS_PENULTIMATE
            outcome = _weighted_choice(_OUTCOMES, outcome_weights)

            regimen_id = f"med-{p['id']}-line{line_num}-regimen"

            # Look up HemOnc concept_id for this drug combination
            _drug_key = frozenset(drugs)
            _hemonc_id = MYELOMA_REGIMEN_CONCEPT_IDS.get(_drug_key)
            _regimen_coding = [{'system': 'http://ctomop.io/fhir/mm-regimen', 'code': name}]
            if _hemonc_id:
                _regimen_coding.append({
                    'system': 'http://ohdsi.org/omop/HemOnc',
                    'code': str(_hemonc_id),
                    'display': name,
                })

            # Regimen-level entry — no partOf, regimen name as text
            meds.append({
                'resourceType': 'MedicationStatement',
                'id': regimen_id,
                'status': 'completed' if not is_last else 'active',
                'medicationCodeableConcept': {
                    'coding': _regimen_coding,
                    'text': name,
                },
                'subject': {'reference': f"Patient/{p['id']}"},
                'effectivePeriod': {
                    'start': line_start.strftime('%Y-%m-%d'),
                    'end': line_end.strftime('%Y-%m-%d'),
                },
                'extension': [
                    {'url': 'http://ctomop.io/fhir/StructureDefinition/therapy-line',
                     'valueInteger': line_num},
                    {'url': 'http://ctomop.io/fhir/StructureDefinition/therapy-outcome',
                     'valueString': outcome},
                ],
                'note': [{'text': f"Line {line_num}: {name} — {outcome}"}],
            })

            # Individual drug entries with partOf
            for drug_key in drugs:
                if drug_key not in _DRUG_INFO:
                    continue
                rxcui, display = _DRUG_INFO[drug_key]
                meds.append({
                    'resourceType': 'MedicationStatement',
                    'id': f"med-{p['id']}-line{line_num}-{drug_key}",
                    'status': 'completed' if not is_last else 'active',
                    'medicationCodeableConcept': {
                        'coding': [{'system': 'http://www.nlm.nih.gov/research/umls/rxnorm',
                                    'code': rxcui, 'display': display}],
                        'text': display,
                    },
                    'subject': {'reference': f"Patient/{p['id']}"},
                    'effectivePeriod': {
                        'start': line_start.strftime('%Y-%m-%d'),
                        'end': line_end.strftime('%Y-%m-%d'),
                    },
                    'partOf': [{'reference': f"MedicationStatement/{regimen_id}"}],
                    'extension': [
                        {'url': 'http://ctomop.io/fhir/StructureDefinition/therapy-line',
                         'valueInteger': line_num},
                    ],
                })

            line_start = line_end + timedelta(days=random.randint(28, 84))

        return meds

    def _mm_specific_obs(self, p, diag_date):
        pid = p['id']
        dt = diag_date.strftime('%Y-%m-%d')
        obs_list = []
        obs_list.append(self._obs(
            pid, f"obs-{pid}-pcl", '47082-2', 'Plasma cell leukemia',
            'laboratory', dt, 'boolean', p['plasma_cell_leukemia'],
        ))
        obs_list.append(self._obs(
            pid, f"obs-{pid}-measurable-imwg", '85598-2',
            'Measurable disease per IMWG criteria',
            'laboratory', dt, 'boolean', p['measurable_disease_imwg'],
        ))
        obs_list.append(self._obs(
            pid, f"obs-{pid}-progression", '33728-8', 'Disease progression status',
            'laboratory', dt, 'string', p['progression'],
        ))
        obs_list.append(self._obs(
            pid, f"obs-{pid}-iss-stage", '21908-9', 'ISS stage',
            'laboratory', dt, 'string', f"ISS {p['iss_stage']}",
        ))
        obs_list.append(self._obs(
            pid, f"obs-{pid}-riss-stage", '21908-9-riss', 'R-ISS stage',
            'laboratory', dt, 'string', f"R-ISS {p['riss_stage']}",
        ))
        obs_list.append(self._obs(
            pid, f"obs-{pid}-prior-lines", '21861-0', 'Prior lines of therapy',
            'laboratory', dt, 'integer', p['prior_lines'],
        ))
        obs_list.append(self._obs(
            pid, f"obs-{pid}-refractory", '85330-2', 'Treatment refractory status',
            'laboratory', dt, 'string',
            ','.join(p['refractory']) if p['refractory'] else 'none',
        ))
        return obs_list

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _random_date(year_from, year_to):
        start = datetime(year_from, 1, 1)
        end = datetime(year_to, 12, 31)
        return start + timedelta(days=random.randint(0, (end - start).days))
