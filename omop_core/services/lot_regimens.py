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
