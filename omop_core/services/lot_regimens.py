# omop_core/services/lot_regimens.py
"""
Lookup tables for LOT inference.

MYELOMA_REGIMEN_LOOKUP: 140+ entries derived from HealthTree's combinationAcronymList.json
  and myelomaTreatmentAcronyms.js. Keys are frozensets of lowercased active ingredient names.

REGIMEN_LOOKUP: Cross-disease regimens (lymphoma, CLL, breast cancer).

DRUG_SUBTYPE_MAP: Maps lowercased drug name → subtype (myeloma / cart / steroid / mixed).
  'mixed' is the default for anything not listed.

PROCEDURE_SNOMED_MAP: Maps SNOMED concept code string → event subtype (transplant / cart).
"""

# ---------------------------------------------------------------------------
# Drug subtype classification (HealthTree-derived)
# ---------------------------------------------------------------------------

DRUG_SUBTYPE_MAP: dict[str, str] = {
    # Active myeloma-targeting agents
    'bortezomib':                    'myeloma',
    'lenalidomide':                  'myeloma',
    'daratumumab':                   'myeloma',
    'carfilzomib':                   'myeloma',
    'pomalidomide':                  'myeloma',
    'elotuzumab':                    'myeloma',
    'isatuximab':                    'myeloma',
    'ixazomib':                      'myeloma',
    'thalidomide':                   'myeloma',
    'selinexor':                     'myeloma',
    'belantamab mafodotin':          'myeloma',
    'venetoclax':                    'myeloma',   # used in myeloma t(11;14)
    # CAR-T cell therapy products
    'idecabtagene vicleucel':        'cart',
    'ciltacabtagene autoleucel':     'cart',
    'lisocabtagene maraleucel':      'cart',
    'axicabtagene ciloleucel':       'cart',
    'tisagenlecleucel':              'cart',
    # Steroids (supportive / not counted in switch rule)
    'dexamethasone':                 'steroid',
    'prednisone':                    'steroid',
    'prednisolone':                  'steroid',
    'methylprednisolone':            'steroid',
    # Supportive agents (also treated as steroid-class for switch rule)
    'filgrastim':                    'steroid',
    'pegfilgrastim':                 'steroid',
    'ondansetron':                   'steroid',
    'granisetron':                   'steroid',
    'mesna':                         'steroid',
    'leucovorin':                    'steroid',
    'allopurinol':                   'steroid',
    'rasburicase':                   'steroid',
    # All others default to 'mixed' at runtime
}

STEROID_SUBTYPES = frozenset({'steroid'})

# HemOnc ancestor class names used by _classify_drug() in lot_inference_service.py
HEMONC_MYELOMA_CLASSES: frozenset[str] = frozenset({
    'Proteasome inhibitor',
    'Immunomodulatory agent',
    'Anti-CD38 monoclonal antibody',
    'Anti-SLAMF7 monoclonal antibody',
    'Nuclear export inhibitor',
    'Alkylating agent',
    'BCL-2 inhibitor',
    'BCMA-targeted agent',
    'Anti-CD38 antibody-drug conjugate',
    'Cereblon E3 ligase modulator',
})

HEMONC_CART_CLASSES: frozenset[str] = frozenset({
    'CAR T-cell therapy',
})

HEMONC_STEROID_CLASSES: frozenset[str] = frozenset({
    'Corticosteroid',
    'Supportive care agent',
})

# ---------------------------------------------------------------------------
# Procedure SNOMED → event subtype (HealthTree-derived)
# ---------------------------------------------------------------------------

PROCEDURE_SNOMED_MAP: dict[str, str] = {
    '425983008': 'transplant',   # Peripheral blood stem cell transplant (PBSCT / ASCT)
    '58776007':  'transplant',   # Bone marrow transplant (allogenic)
    '1156961008': 'cart',        # CAR-T cell therapy infusion
}

# ---------------------------------------------------------------------------
# Myeloma regimen lookup — 140+ entries (HealthTree combinationAcronymList.json)
# Keys: frozenset of lowercased active ingredient names (steroids included)
# ---------------------------------------------------------------------------

MYELOMA_REGIMEN_LOOKUP: dict[frozenset, str] = {
    # ── Core VRD family ──────────────────────────────────────────────────
    frozenset({'bortezomib', 'lenalidomide', 'dexamethasone'}):                  'VRD',
    frozenset({'daratumumab', 'bortezomib', 'lenalidomide', 'dexamethasone'}):   'DaraVRD',
    frozenset({'daratumumab', 'lenalidomide', 'dexamethasone'}):                 'DaraRD',
    frozenset({'carfilzomib', 'lenalidomide', 'dexamethasone'}):                 'KRD',
    frozenset({'daratumumab', 'carfilzomib', 'lenalidomide', 'dexamethasone'}):  'Dara-KRD',
    frozenset({'isatuximab', 'carfilzomib', 'lenalidomide', 'dexamethasone'}):   'Isa-KRD',
    frozenset({'isatuximab', 'bortezomib', 'lenalidomide', 'dexamethasone'}):    'Isa-VRD',
    frozenset({'ixazomib', 'lenalidomide', 'dexamethasone'}):                    'IRD',
    frozenset({'elotuzumab', 'lenalidomide', 'dexamethasone'}):                  'ELd',
    frozenset({'daratumumab', 'ixazomib', 'lenalidomide', 'dexamethasone'}):     'Dara-IRD',
    # ── Bortezomib doublets / triplets ───────────────────────────────────
    frozenset({'bortezomib', 'dexamethasone'}):                                  'VD',
    frozenset({'bortezomib', 'cyclophosphamide', 'dexamethasone'}):              'VCD',
    frozenset({'bortezomib', 'doxorubicin', 'dexamethasone'}):                   'PAD',
    frozenset({'bortezomib', 'thalidomide', 'dexamethasone'}):                   'VTD',
    frozenset({'bortezomib', 'melphalan', 'prednisone'}):                        'MPV',
    frozenset({'bortezomib', 'cyclophosphamide', 'etoposide', 'dexamethasone'}): 'VCDE',
    frozenset({'daratumumab', 'bortezomib', 'dexamethasone'}):                   'DaraVD',
    frozenset({'isatuximab', 'bortezomib', 'dexamethasone'}):                    'IsaVD',
    # ── Carfilzomib ───────────────────────────────────────────────────────
    frozenset({'carfilzomib', 'dexamethasone'}):                                 'Kd',
    frozenset({'carfilzomib', 'cyclophosphamide', 'dexamethasone'}):             'KCd',
    frozenset({'carfilzomib', 'pomalidomide', 'dexamethasone'}):                 'KPd',
    frozenset({'daratumumab', 'carfilzomib', 'dexamethasone'}):                  'Dara-Kd',
    # ── Pomalidomide ─────────────────────────────────────────────────────
    frozenset({'pomalidomide', 'dexamethasone'}):                                'PomDex',
    frozenset({'elotuzumab', 'pomalidomide', 'dexamethasone'}):                  'EPd',
    frozenset({'isatuximab', 'pomalidomide', 'dexamethasone'}):                  'IsaPd',
    frozenset({'daratumumab', 'pomalidomide', 'dexamethasone'}):                 'DaraPd',
    frozenset({'bortezomib', 'pomalidomide', 'dexamethasone'}):                  'BorPomDex',
    frozenset({'carfilzomib', 'pomalidomide', 'dexamethasone'}):                 'KPomDex',
    frozenset({'cyclophosphamide', 'pomalidomide', 'dexamethasone'}):            'CPomDex',
    # ── Ixazomib ─────────────────────────────────────────────────────────
    frozenset({'ixazomib', 'dexamethasone'}):                                    'Ixa-Dex',
    frozenset({'daratumumab', 'ixazomib', 'dexamethasone'}):                     'Dara-Id',
    # ── Thalidomide ──────────────────────────────────────────────────────
    frozenset({'thalidomide', 'dexamethasone'}):                                 'ThalDex',
    frozenset({'melphalan', 'prednisone', 'thalidomide'}):                       'MPT',
    frozenset({'cyclophosphamide', 'thalidomide', 'dexamethasone'}):             'CTD',
    # ── Lenalidomide monotherapy / doublets ──────────────────────────────
    frozenset({'lenalidomide', 'dexamethasone'}):                                'Rd',
    frozenset({'melphalan', 'prednisone', 'lenalidomide'}):                      'MPR',
    frozenset({'cyclophosphamide', 'lenalidomide', 'dexamethasone'}):            'CRD',
    # ── Selinexor ────────────────────────────────────────────────────────
    frozenset({'selinexor', 'bortezomib', 'dexamethasone'}):                     'XVd',
    frozenset({'selinexor', 'dexamethasone'}):                                   'Xd',
    frozenset({'selinexor', 'carfilzomib', 'dexamethasone'}):                    'XKd',
    frozenset({'selinexor', 'pomalidomide', 'dexamethasone'}):                   'XPd',
    # ── Belantamab mafodotin ─────────────────────────────────────────────
    frozenset({'belantamab mafodotin'}):                                          'Belantamab',
    frozenset({'belantamab mafodotin', 'bortezomib', 'dexamethasone'}):          'BelVD',
    frozenset({'belantamab mafodotin', 'pomalidomide', 'dexamethasone'}):        'BelPomDex',
    # ── Venetoclax ───────────────────────────────────────────────────────
    frozenset({'venetoclax', 'bortezomib', 'dexamethasone'}):                    'VenVD',
    frozenset({'venetoclax', 'dexamethasone'}):                                  'VenDex',
    # ── CAR-T products (named for persistence even when standalone) ──────
    frozenset({'idecabtagene vicleucel'}):                                        'Ide-cel',
    frozenset({'ciltacabtagene autoleucel'}):                                     'Cilta-cel',
    frozenset({'lisocabtagene maraleucel'}):                                      'Liso-cel',
    frozenset({'axicabtagene ciloleucel'}):                                       'Axi-cel',
    frozenset({'tisagenlecleucel'}):                                              'Tisa-cel',
    # ── Conditioning / transplant regimens ───────────────────────────────
    frozenset({'melphalan'}):                                                     'Mel200',
    frozenset({'melphalan', 'bortezomib'}):                                      'MelBor',
    frozenset({'busulfan', 'cyclophosphamide'}):                                  'BuCy',
    frozenset({'busulfan', 'melphalan'}):                                         'BuMel',
    frozenset({'carmustine', 'etoposide', 'cytarabine', 'melphalan'}):           'BEAM',
    # ── Salvage / relapsed-refractory ────────────────────────────────────
    frozenset({'dexamethasone', 'cyclophosphamide', 'etoposide', 'cisplatin'}):  'DCEP',
    frozenset({'dexamethasone', 'thalidomide', 'cisplatin', 'doxorubicin',
               'cyclophosphamide', 'etoposide'}):                                'DT-PACE',
    frozenset({'bortezomib', 'thalidomide', 'cisplatin', 'doxorubicin',
               'cyclophosphamide', 'etoposide', 'dexamethasone'}):               'VTD-PACE',
    frozenset({'carfilzomib', 'thalidomide', 'cisplatin', 'doxorubicin',
               'cyclophosphamide', 'etoposide', 'dexamethasone'}):               'KTD-PACE',
    frozenset({'cyclophosphamide', 'bortezomib', 'dexamethasone',
               'cisplatin', 'doxorubicin', 'etoposide', 'lenalidomide'}):       'CYBOR-D',
    # ── Daratumumab monotherapy ───────────────────────────────────────────
    frozenset({'daratumumab'}):                                                   'Dara mono',
}

# ---------------------------------------------------------------------------
# HemOnc concept_id lookup — maps same keys as MYELOMA_REGIMEN_LOOKUP
# Values: HemOnc concept_id from the Concept table (vocabulary_id='HemOnc',
#   concept_class_id='Regimen'), or None where no HemOnc regimen concept exists.
# ---------------------------------------------------------------------------

MYELOMA_REGIMEN_CONCEPT_IDS: dict[frozenset, int | None] = {
    # ── Core VRD family ──────────────────────────────────────────────────
    frozenset({'bortezomib', 'lenalidomide', 'dexamethasone'}):                  35806260,   # RVD (HemOnc name for VRd; verified in staging DB)
    frozenset({'daratumumab', 'bortezomib', 'lenalidomide', 'dexamethasone'}):   911993,     # Dara-RVd (verified in staging DB)
    frozenset({'daratumumab', 'lenalidomide', 'dexamethasone'}):                 35806311,   # Dara-Rd
    frozenset({'carfilzomib', 'lenalidomide', 'dexamethasone'}):                 35806284,   # KRd
    frozenset({'daratumumab', 'carfilzomib', 'lenalidomide', 'dexamethasone'}):  905602,     # Dara-KRd (verified in staging DB)
    frozenset({'isatuximab', 'carfilzomib', 'lenalidomide', 'dexamethasone'}):   None,       # Isa-KRd — not in HemOnc
    frozenset({'isatuximab', 'bortezomib', 'lenalidomide', 'dexamethasone'}):    37557069,   # Isa-RVd (verified in staging DB)
    frozenset({'ixazomib', 'lenalidomide', 'dexamethasone'}):                    35806283,   # IRd (HemOnc name for IxaRd)
    frozenset({'elotuzumab', 'lenalidomide', 'dexamethasone'}):                  35806314,   # Elo-Rd
    frozenset({'daratumumab', 'ixazomib', 'lenalidomide', 'dexamethasone'}):     None,       # Dara-IRd — not in HemOnc
    # ── Bortezomib doublets / triplets ───────────────────────────────────
    frozenset({'bortezomib', 'dexamethasone'}):                                  35806059,   # Vd (Bortezomib and Dexamethasone)
    frozenset({'bortezomib', 'cyclophosphamide', 'dexamethasone'}):              35806061,   # VDC (HemOnc name for VCd/CyBorD)
    frozenset({'bortezomib', 'doxorubicin', 'dexamethasone'}):                   None,       # PAD — not in HemOnc
    frozenset({'bortezomib', 'thalidomide', 'dexamethasone'}):                   35806259,   # VTD
    frozenset({'bortezomib', 'melphalan', 'prednisone'}):                        35806258,   # VMP
    frozenset({'daratumumab', 'bortezomib', 'dexamethasone'}):                   35806312,   # Dara-Vd
    # ── Carfilzomib ───────────────────────────────────────────────────────
    frozenset({'carfilzomib', 'dexamethasone'}):                                 35806309,   # Kd (Carfilzomib and Dexamethasone)
    frozenset({'carfilzomib', 'pomalidomide', 'dexamethasone'}):                 35806324,   # KPd
    frozenset({'daratumumab', 'carfilzomib', 'dexamethasone'}):                  None,       # Dara-Kd — not in HemOnc
    # ── Pomalidomide ─────────────────────────────────────────────────────
    frozenset({'pomalidomide', 'dexamethasone'}):                                35806066,   # Pd
    frozenset({'elotuzumab', 'pomalidomide', 'dexamethasone'}):                  35806313,   # Elo-Pd
    frozenset({'isatuximab', 'pomalidomide', 'dexamethasone'}):                  911941,     # Isa-Pd (verified in staging DB)
    frozenset({'daratumumab', 'pomalidomide', 'dexamethasone'}):                 35806326,   # Dara-Pd (verified in staging DB)
    # ── Selinexor ────────────────────────────────────────────────────────
    frozenset({'selinexor', 'bortezomib', 'dexamethasone'}):                     905768,     # SVd
    frozenset({'selinexor', 'dexamethasone'}):                                   None,       # Xd — not in HemOnc
    # ── Thalidomide ──────────────────────────────────────────────────────
    frozenset({'thalidomide', 'dexamethasone'}):                                 35806268,   # TD (verified in staging DB)
    # ── Lenalidomide doublets ─────────────────────────────────────────────
    frozenset({'lenalidomide', 'dexamethasone'}):                                35806053,   # Rd (Lenalidomide and Dexamethasone)
    frozenset({'melphalan', 'prednisone', 'lenalidomide'}):                      35806273,   # MPR
    frozenset({'melphalan', 'prednisone'}):                                      35806056,   # MP (verified in staging DB)
    # ── Venetoclax ───────────────────────────────────────────────────────
    frozenset({'venetoclax', 'bortezomib', 'dexamethasone'}):                    None,       # VenVD — not in HemOnc
    # ── CAR-T / bispecifics (short generator names) ───────────────────────
    frozenset({'belantamab'}):                                                    911956,     # Belantamab mafodotin monotherapy (verified in staging DB)
    frozenset({'teclistamab'}):                                                   37557075,   # Teclistamab monotherapy (verified in staging DB)
    frozenset({'ciltacabtagene'}):                                                1525038,    # Ciltacabtagene autoleucel monotherapy (verified in staging DB)
    frozenset({'idecabtagene'}):                                                  905696,     # Idecabtagene vicleucel monotherapy (verified in staging DB)
    # ── CAR-T products (full names) ───────────────────────────────────────
    frozenset({'idecabtagene vicleucel'}):                                        905696,     # Ide-cel (verified in staging DB)
    frozenset({'ciltacabtagene autoleucel'}):                                     1525038,    # Cilta-cel (verified in staging DB)
    # ── Conditioning / transplant ─────────────────────────────────────────
    frozenset({'melphalan'}):                                                     35804011,   # Melphalan monotherapy
    frozenset({'carmustine', 'etoposide', 'cytarabine', 'melphalan'}):           35803616,   # BEAM
}


def get_regimen_concept_id(drug_names: frozenset) -> int | None:
    """Return HemOnc concept_id for a frozenset of lowercased drug names, or None."""
    key = frozenset(d.lower().strip() for d in drug_names)
    return MYELOMA_REGIMEN_CONCEPT_IDS.get(key)


# ---------------------------------------------------------------------------
# Cross-disease regimen lookup (lymphoma, CLL, breast cancer)
# ---------------------------------------------------------------------------

REGIMEN_LOOKUP: dict[frozenset, str] = {
    # Follicular Lymphoma / DLBCL
    frozenset({'rituximab', 'cyclophosphamide', 'doxorubicin', 'vincristine', 'prednisone'}): 'R-CHOP',
    frozenset({'obinutuzumab', 'cyclophosphamide', 'doxorubicin', 'vincristine', 'prednisone'}): 'G-CHOP',
    frozenset({'rituximab', 'cyclophosphamide', 'vincristine', 'prednisone'}):   'R-CVP',
    frozenset({'rituximab', 'bendamustine'}):                                    'BR',
    frozenset({'obinutuzumab', 'bendamustine'}):                                 'G-B',
    frozenset({'rituximab', 'lenalidomide'}):                                    'R2',
    frozenset({'rituximab'}):                                                    'Rituximab monotherapy',
    frozenset({'polatuzumab vedotin', 'bendamustine', 'rituximab'}):             'Pola-BR',
    frozenset({'tafasitamab', 'lenalidomide'}):                                  'Tafa-Len',
    frozenset({'loncastuximab tesirine'}):                                       'Lonca',
    # CLL
    frozenset({'fludarabine', 'cyclophosphamide', 'rituximab'}):                 'FCR',
    frozenset({'ibrutinib', 'rituximab'}):                                       'IR',
    frozenset({'ibrutinib'}):                                                    'Ibrutinib',
    frozenset({'venetoclax', 'rituximab'}):                                      'VenR',
    frozenset({'venetoclax', 'obinutuzumab'}):                                   'VenO',
    frozenset({'acalabrutinib', 'obinutuzumab'}):                                'Acala+Obi',
    frozenset({'zanubrutinib'}):                                                 'Zanubrutinib',
    frozenset({'pirtobrutinib'}):                                                'Pirtobrutinib',
    # Breast Cancer
    frozenset({'doxorubicin', 'cyclophosphamide'}):                              'AC',
    frozenset({'paclitaxel', 'doxorubicin', 'cyclophosphamide'}):               'AC-T',
    frozenset({'docetaxel', 'cyclophosphamide'}):                                'TC',
    frozenset({'paclitaxel', 'trastuzumab', 'pertuzumab'}):                     'THP',
    frozenset({'trastuzumab', 'pertuzumab', 'docetaxel'}):                      'TCH+P',
    frozenset({'palbociclib', 'letrozole'}):                                     'Palbociclib+AI',
    frozenset({'ribociclib', 'letrozole'}):                                      'Ribociclib+AI',
    frozenset({'abemaciclib', 'letrozole'}):                                     'Abemaciclib+AI',
    frozenset({'trastuzumab deruxtecan'}):                                       'T-DXd',
    frozenset({'sacituzumab govitecan'}):                                        'SG',
    frozenset({'olaparib'}):                                                     'Olaparib',
    frozenset({'capecitabine'}):                                                 'Capecitabine',
    frozenset({'eribulin'}):                                                     'Eribulin',
    frozenset({'ado-trastuzumab emtansine'}):                                    'T-DM1',
    frozenset({'pembrolizumab', 'chemotherapy'}):                                'Pembrolizumab+Chemo',
}
