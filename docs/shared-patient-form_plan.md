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
`omop_core/services/patient_form.py` is portable to an embedded Django host.

Native v1 clients explicitly opt in with `X-Patient-Form: 1` on both descriptor
GET and record PATCH. Legacy descriptors and serializer writes remain unchanged.
Responses carry `patient_info`, optional `patient_name`, and per-field
`write.applied/rejected/deferred_profile`. HTTP 200 does not acknowledge every
submitted field. Computed results remain read-only.

## Remaining acceptance gates

- BMI is verified end to end on synthetic local data. SLiM/CRAB and MIPI/MCL
  input/result compatibility remains blocked on clinical review and complete
  reviewed bindings. Do not change formulas to make the extraction appear done.
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
