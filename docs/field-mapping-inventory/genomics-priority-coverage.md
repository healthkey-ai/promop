# Priority gene/marker storage coverage (#1311)

**42 effective fields across 51 disease memberships.**

Generated from the reference-only inventory (`manifest.json` → `priority_field_coverage`); it records storage dispositions, not clinical approval of catalog values or gene/variant answer concepts.

## Dispositions

| Disposition | Fields | Meaning |
| --- | ---: | --- |
| source_only_parent | 42 | supported source-only parent (concept 0 retained) |
| standard_parent_candidate_requires_review | 0 | standard Measurement parent candidate requires review |
| storage_recipe_incomplete | 0 | missing, unapproved or contract-incompatible storage recipe |

**Gate:** PASS — missing, unapproved and incompatible recipes fail; supported source-only parents and standard parent candidates pass.

## Per disease

### BC — 6 memberships (source_only_parent=6)

| Field | Gene | State | Problems | Parent recipe | Portable code | Concept target |
| --- | --- | --- | --- | --- | --- | --- |
| `genomics_brca1` | BRCA1 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_brca2` | BRCA2 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_pik3ca` | PIK3CA | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_tp53` | TP53 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_esr1` | ESR1 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_palb2` | PALB2 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |

### CLL — 8 memberships (source_only_parent=8)

| Field | Gene | State | Problems | Parent recipe | Portable code | Concept target |
| --- | --- | --- | --- | --- | --- | --- |
| `genomics_tp53` | TP53 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_notch1` | NOTCH1 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_sf3b1` | SF3B1 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_atm` | ATM | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_del17p` | TP53 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_del11q` | 11q | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_del13q` | 13q | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_trisomy12` | 12 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |

### FL — 5 memberships (source_only_parent=5)

| Field | Gene | State | Problems | Parent recipe | Portable code | Concept target |
| --- | --- | --- | --- | --- | --- | --- |
| `genomics_bcl2` | BCL2 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_ezh2` | EZH2 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_kmt2d` | KMT2D | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_crebbp` | CREBBP | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_bcl6` | BCL6 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |

### MCL — 16 memberships (source_only_parent=16)

| Field | Gene | State | Problems | Parent recipe | Portable code | Concept target |
| --- | --- | --- | --- | --- | --- | --- |
| `genomics_tp53` | TP53 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_myc` | MYC | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_kmt2d` | KMT2D | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_notch1` | NOTCH1 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_notch2` | NOTCH2 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_nsd2` | NSD2 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_cdkn2a` | CDKN2A | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_smarca4` | SMARCA4 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_ccnd1` | CCND1 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_del17p` | TP53 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_t1114` | CCND1/IGH | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_bcl2_amplification` | BCL2 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_complex_karyotype` | Chromosomal | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_complex_karyotype_excl_t1114` | Chromosomal | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_atm_atr` | ATM/ATR | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_notch1_notch2` | NOTCH1/NOTCH2 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |

### MM — 16 memberships (source_only_parent=16)

| Field | Gene | State | Problems | Parent recipe | Portable code | Concept target |
| --- | --- | --- | --- | --- | --- | --- |
| `genomics_tp53` | TP53 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_kras` | KRAS | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_nras` | NRAS | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_braf` | BRAF | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_myc` | MYC | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_fam46c` | FAM46C | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_dis3` | DIS3 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_xbp1` | XBP1 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_del17p` | TP53 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_t414` | FGFR3/NSD2/IGH | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_t1114` | CCND1/IGH | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_t1416` | IGH/MAF | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_gain1q` | 1q21 | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_hyperdiploidy` | Chromosomal | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_chromothripsis` | Chromosomal | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |
| `genomics_igh` | IGH | source_only_parent | — | measurement/json/multiple=True | 81252-9 | 21495010 (Observation) |

## Regenerate

```sh
python manage.py export_field_mapping_inventory --output docs/field-mapping-inventory/manifest.json --report docs/field-mapping-inventory/coverage.md
python manage.py export_genomics_priority_coverage --write
python manage.py export_genomics_priority_coverage --check
```

Source-only parents remain supported by the Genomics Measurement/event contract. An Observation-domain concept cannot certify a standard Measurement parent. Field question coverage does not approve gene/variant answer concepts.
