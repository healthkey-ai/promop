# Clinical source-input dev forward-port plan

## Scope and preserved work

Prepare a separate PR against PROMOP `dev`, based on `550dc132`, preserving
published [#1987](https://github.com/healthkey-ai/promop/pull/1987) on `cb-v2`
unchanged. This port selects the clinical SDK, projection, descriptor, provenance
and API changes; it excludes therapy-attestation, its migration chain, unrelated
DRF/bulk-error changes and frontend history.

The shared-form composition and its unpublished HTTP400 adapter remain separate.
No CB dependency SHA, clinical mapping approval, merge, deployment or backfill is
authorized by this PR. No frontend controls or FHIR coding rules are invented
for these fields. See [implemented behavior](cb_clinical_inputs_architecture.md)
and `clinical_input_contract.RECIPES` for the authoritative contract.

## Acceptance gates

- Run targeted SDK, native/API permission, descriptor, provenance, pending and
  mixed-batch rollback tests on a dedicated local PostgreSQL database.
- Retain explicit False/null, append-only corrections/tombstones, selected-fact
  readback and mapping-lock concurrency tests.
- Verify current dev IMWG/FLC, unknown/reported therapy history, packaged therapy
  authoring and 21-query refresh budget. Ratios must not change INR/PT/aPTT seconds.
- Check one migration leaf, no model drift, the new migration's upgrade from the
  dev predecessor, and fresh/package migration graphs without `patient_portal`.
- Complete two independent whole-PR reviews against the final SHA before push
  and PR creation. Required application/security CI gates remain necessary.

## Rollout remains gated

SDK availability does not imply per-field approval. The deployed mapping table
must contain compatible individually approved bindings before a field is writable.
Legacy SCT availability is not SDK permission.

Derivation version 19 makes older projections eligible for re-derivation but
does not run a backfill. Full refresh includes already-current upstream clinical
rules; before any separately authorized backfill, review scoped projection impact,
including IMWG/FLC changes, against source evidence. Test fixtures with synthetic
approvals are not clinical approval or deployment evidence.

CB must separately validate the real candidate round trip and coordinated
dependency composition. Existing CB EXACT mapping parity, security analysis and
other acceptance blockers do not become green merely because this port exists.
