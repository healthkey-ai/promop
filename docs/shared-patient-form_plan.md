# Shared patient form candidate (CB #5482)

## Implemented boundary

`frontend/src/federation/PatientFieldEditor.tsx` owns controls and recursive
computed-input dialogs. `patientFormStore.ts` owns descriptors, optimistic
drafts, 500 ms batching, serialized acknowledged PATCHes and rollback.
`patientFormRuntime.ts` captures API/credential/impersonation/session identity;
an old queue cannot be retargeted to a new context. SPA unmount drains pending
writes and retains the original host invalidation callback until idle.
Browser-close delivery is not guaranteed.

`widget.patient-info.tsx` exposes `mount` for the profile and `mountFields` for
host-provided empty field slots, using a single remote React root with portals.
The host retains authentication, authorization and eligibility/cache policy.
`mountTherapyLines` and `mountGenomics` expose one host-owned editor each (the
therapy-line list; the genomic-findings list and form), for a host that has a
row and a modal but not the patient form. The host supplies credentials and an
empty element, PROMOP owns the dialog, the fields and the write, and the host
refreshes its own queries from `onPatientUpdated`.
`omop_core/services/patient_form.py` is portable to an embedded Django host.

### React-tree hosts

A host that shares this React tree does not use the standalone widget. It mounts
`federation/TherapyLinesEditor.tsx`, `federation/GenomicsEditor.tsx` or
`federation/PatientFieldEditorSlot.tsx` through the Module Federation remote
(`vite.remote.config.ts`) and hands over its own authenticated axios client
plus a base path instead of a DRF token. That keeps the host's interceptor
(Firebase Bearer in ht-phr), its origin and its connection-scoped identity:
minting a client from a token would either send the wrong auth scheme or split
one browser session into two identities.

These editors never register the module-level clinical transport. The therapy
panel and its dialog take the connection explicitly, the genomics dialog takes
the client and URL explicitly, and the field slot writes through the store — so
two editors on one page cannot inherit each other's connection. The store is
keyed by client + base + optional `connectionKey`. A host whose single axios
instance survives a sign-in or impersonation change must provide a new client
(or at least a new key): a new key starts a new queue immediately, while a
batch already queued still drains through the client it captured and therefore
through whatever credentials that client now sends. `PatientFieldEditorSlot.onChanged` fires after the server
acknowledges a save; `TherapyLinesEditor.onChanged` and
`GenomicsEditor.onChanged` fire after their own mutations, so the host can
invalidate its trial queries.

`usePromopFieldBindings` answers which trial-row names resolve (canonical keys
and declared `bindings`), so the host can return `null` for a row PROMOP cannot
draw and EXACT keeps its own editor.

Native v1 clients explicitly opt in with `X-Patient-Form: 1` on both descriptor
GET and record PATCH. Legacy descriptors and serializer writes remain unchanged.
Responses carry `patient_info`, optional `patient_name`, and per-field
`write.applied/rejected/deferred_profile`. HTTP 200 does not acknowledge every
submitted field. Computed results remain read-only.

## Remaining acceptance gates

The CB integration lane targets `cb-v2`, not `dev`. This branch is based on
`cb-v2` at `12606c9` and ports only the shared form plus its profile-parity
regressions; it does not bring the unrelated `dev`-side medical changes along.
The CI trigger includes `cb-v2` so the normal application/security jobs run on
the integration PR. CB records any pre-merge pin as an exact-SHA, expiring
exception naming the actual published feature branch. Integration does not wait
for promotion to `dev`, and no ancestry or security check is disabled.

- BMI is verified end to end on synthetic local data. The complex rows keep a
  **result-only** gate: `meets_crab`, `meets_slim`, `mipi_risk`, `mipi_c_risk`
  and `high_risk_mcl_criteria` are computed and cannot be authored, but their
  source facts (`hemoglobin_g_dl`, `serum_calcium_mg_dl`,
  `creatinine_clearance_ml_min`, `serum_creatinine_mg_dl`, `bone_lesions`,
  `clonal_plasma_cells`, `kappa_flc`, `lambda_flc`, `ldh_*`, `wbc_*`,
  `ki67_*`, `date_of_birth`, `ecog_performance_status`) stay editable through
  the same recursive subform. A gate is reported as `compatibility_scope:
  'result'`.
- Editing a CRAB/SLiM source fact re-derives that verdict from the record's own
  canonical inputs in the same request (`derive_meets_crab`,
  `derive_meets_slim`), so the value read back is the current one rather than a
  stale result from the last OMOP refresh. The existing thresholds and the
  involved-chain burden guard are unchanged; unknown inputs stay unknown instead
  of asserting "not met".
- Full parity with Trial Details remains a clinical decision, not a coding gap:
  CB's SLiM adds the MRI-lesion criterion, and CB derives CRAB/SLiM with its own
  three-valued rule. PROMOP does not invent `bone_lesions` MRI semantics from a
  shared presence flag, and MIPI/MIPI-c still have no reviewed PROMOP result
  column. Do not change formulas to make the extraction appear done.
- The existing patient-self-report scalar applier does not support every native
  projection. Shared descriptors reject condition/procedure/drug projections,
  structured fields and special treatment/death assertions rather than writing
  the wrong fact or changing provenance. Noncanonical height/weight values are
  read-only until reviewed conversion exists. Strings exceeding the scalar
  60-character limit are rejected, never silently truncated.
- Direct PatientRecord edits retain `user_edited_fields`; a successful mapped
  transition removes the old direct marker. Planned Therapies remains direct
  storage, without an invented OMOP mapping.
- Publish an immutable candidate commit, then update CB's package pin and both
  widget bundles/manifests together. No candidate is release-ready solely because
  source-backed tests pass. Track the clinical gates separately in both PRs.

## Regression evidence

Targeted tests: `tests/test_patient_form.py`, `tests/test_patient_form_api.py`,
`frontend/src/federation/patientFormStore.test.ts`,
`frontend/src/federation/PatientFieldEditor.test.tsx`, and
`frontend/src/federation/PatientInfo.save.test.tsx`.
CB adds authorized/impersonated API and real Trial Details BMI matching tests,
plus host callback-generation and widget integrity tests. The browser fixture
verified BMI 20 to 25, changed eligibility, saved during immediate SPA navigation,
and persisted the result after reload. That fixture does not establish full
production UI or clinical parity.
