# Integration-compatible clinical source inputs (CancerBot #5440)

This branch extends the exact PROMOP schema pinned by CancerBot
(`e1bcc42c9130205eb06029eb79c843f0c1255825`) without importing modern-dev therapy,
deletion or federation schema changes. It is a compatible source candidate, not
authorization to deploy integration history or waive immutable-pin provenance.

`clinical_input_contract.py` publishes version 1 of the four-field source contract:
two positive decimal PT/aPTT ratios to laboratory ULN, nullable swallowing ability,
and the existing SCT date. The three new columns are added by
`0280_cb_clinical_inputs.py`; the SDK has four inputs because `sct_date` already
exists in the pinned schema. Unknown is not yes. Ratios are neither INR nor seconds;
their explicit reviewed local Observation source keys use concept 0 rather than
inventing a standard clinical code. SCT retains `mm-sct-date`, matching existing
imports, and stays in the contract because the generic field applier refuses clears:
a patient clearing a transplant date must append a tombstone instead of leaving the
old fact current. The descriptor requires real concept 0 and self-report type vocabulary.

The host SDK validates all inputs, locks Person then projection in the same order
as the existing field applier, appends dated self-reported facts, and refreshes the
projection inside one transaction. Corrections and clears never overwrite or
delete imports. A clear is an explicit `PatientRecord:cleared` tombstone; latest
date/PK wins, including invalid latest data becoming unknown. A newer fact that
prevents the requested value from round-tripping rejects and rolls back the write.
Five decimal places match OMOP numeric storage; excess precision/nonfinite data
is rejected rather than silently rounded. Boolean false and null are distinct.

The descriptor-driven field applier routes these four fields through the same
SDK, including equal-value retries and clears. A caller that supplies a curated
DIRECT descriptor for `sct_date` keeps the generic projection path instead; the
contract descriptor is the default. The integration schema still does
not accept arbitrary direct clinical projection PATCHes. Computed verdicts stay
read-only. Other facts, therapy inference, lab mappings and schema retain their
existing contracts. No patient_portal application is required by the plugin SDK.

The new three fields are not exposed by CancerBot's currently reviewed old
federated bundle: its explicit privacy classification remains unread-by-widget.
CB Trial Details/source editors and matching consume them. A rebuilt widget and
changed manifest need separate review; no compiled bundle is replaced here.

Tests exercise source writes/refresh, explicit false, clears/history, vocabulary
failure, float/decimal validation, same-day imported facts and failed round-trip
rollback. CancerBot additionally runs real HTTP PATCH → fact → fresh projection →
search under both dispatch modes with the installed candidate wheel, and its full
backend suite. A reviewed upstream history promotion and a compatible immutable
repin are still mandatory; reaching `dev` by a cherry-pick alone does not make the
existing pin an ancestor of `dev`.

Local target evidence: 58 upstream source/legacy-applier tests pass, and 141
composed CB source/bridge/profile tests pass against the installed compatible
wheel. A preliminary full CB run passed 3763 tests (one skip, one expected failure);
the final frozen-source serial run with an isolated Redis database passed 3767
tests (one skip, one expected failure, 12 subtests). Fresh migrations and drift checks pass.
These are isolated local runs, not promotion approval, current production-pin CI
or an authenticated browser smoke. The preserved modern-dev research candidate
had 21 failures and is not the target of this narrow extension.
