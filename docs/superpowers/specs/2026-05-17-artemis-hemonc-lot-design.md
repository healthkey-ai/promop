# ARTEMIS HemOnc LOT Inference Design

**Date:** 2026-05-17
**Status:** As-built design
**Related design:** `2026-05-17-athena-vocabulary-artemis-design.md`

## Goal

LOT inference classifies therapy drugs through the loaded OMOP vocabulary graph before
falling back to legacy source-value matching. This lets brand names, RxNorm concepts, and
novel agents use HemOnc class evidence where available.

## Architecture

`omop_core/services/lot_inference_service.py` classifies each drug exposure through a
two-step lookup:

1. Resolve the exposure's RxNorm concept to a HemOnc concept through
   `ConceptRelationship(relationship='Maps to')`.
2. Walk `ConceptAncestor` from the HemOnc concept to ancestor class concepts.

Class names are compared against the HemOnc class sets in
`omop_core/services/lot_regimens.py`:

- `HEMONC_MYELOMA_CLASSES`
- `HEMONC_CART_CLASSES`
- `HEMONC_STEROID_CLASSES`

If a concept id is missing, or the vocabulary graph has no usable class evidence,
classification falls back to `DRUG_SUBTYPE_MAP`.

## Runtime Flow

```text
DrugExposure
  |
  v
_classify_drug(drug_concept_id, drug_source_value)
  |
  +-- RxNorm -> HemOnc via ConceptRelationship
  +-- HemOnc -> class names via ConceptAncestor
  +-- fallback source-value map
  |
  v
Line-of-therapy segmentation and regimen naming
```

The rest of LOT inference remains the ARTEMIS-lite and HealthTree phase-aware pipeline:
drug eras, combination windows, transplant/CAR-T boundaries, phase labels, regimen
naming, and `Episode`/`EpisodeEvent` persistence.

## Verification

Tests cover HemOnc-backed drug classification, fallback behavior, brand-name resolution,
and end-to-end inference for classified drugs.
