# Writable-field descriptor

`GET /api/v1/patient-records/writable-fields/` is the contract a client reads
before it draws a patient-side editor. This page is the contract's own
documentation; the code is `omop_core/services/write_descriptor.py`.

## Envelope

The response is a mapping of field name to entry, plus one reserved key:

```json
{
  "__descriptor__": {
    "schema_version": 1,
    "documentation": "docs/writable-field-descriptor.md"
  },
  "hemoglobin_g_dl": { "kind": "computed", "...": "..." }
}
```

`__descriptor__` is not a field and has no `kind`. Clients look fields up by
name, so it never collides with one; any loop over the mapping must skip keys
starting with `__`. The form enrichment (`patient_form_descriptors`) does.

The envelope is **opt-in** on the HTTP endpoint: it is sent with the
shared-form header `X-Patient-Form: 1` or with `?envelope=1`. Without either,
the response is the field-only mapping older clients iterate. A client that
wants the version gate must ask for it.

`schema_version` bumps only when a **client-visible meaning** changes: a new
kind, a renamed capability key, different `writable`/`answerable` semantics, or
a new write/readback path. Adding a field, an option or a binding does not bump
it. A client that does not know the version must not guess — keep the field
read-only and say why.

## Scoping

The descriptor is **per caller and per person**:

- without `?person_id=N` it describes the deployment: which fields could be
  written by someone permitted to write them;
- with `person_id`, every entry is the truth for that caller and that patient.
  A caller without write access gets the same entries with `writable: false`
  (and `answerable: false` for attestations), `read_only_for_caller: true` and
  a reason.

A mapped field is not a permission. `personId` is not a permission either.

## Capability model

Four independent dimensions. Do not collapse them into one `kind`.

### Source — `kind`

| kind | canonical value | editor |
| --- | --- | --- |
| `direct` | PatientRecord column | scalar / select / multiselect / date |
| `editable` | a mapped OMOP fact | scalar / select / multiselect / date; the structured `genomics_*` recipes use the findings editor |
| `selectable` | a value carried with its source (a unit) | choice beside `qualifies`; never written alone |
| `profile` | Person attribute (gender, race, ethnicity, address) | select / address |
| `alias` | mirrors `canonical` | the canonical's editor |
| `computed` | derived by code, `inputs` name the sources | subform over the inputs |
| `negated` | opposite of the canonical boolean | positive / negative / unknown dialog |
| `explicit-answer` | attestation with a snapshot | dialog driven by `actions` |
| `authored` | Episode/EpisodeEvent, genomics, therapy lines | the resource's own editor |
| `unmapped` | none | read-only, with a reason and owner |

`computed` and `unmapped` entries may still carry `target`/`projection`
metadata describing where a value comes from. Those keys are provenance, not
permission: only `writable` (and `answerable`, for an attestation) permits a
write.

### Operation — `writable` vs `answerable`

- `writable: true` permits a write and always names `target`. The route is
  the target, not a guess:

  | `target` | write path |
  | --- | --- |
  | `patient_record` | `PATCH /api/v1/patient-records/<person_id>/` |
  | `genomics` | the findings endpoints (list diff), never a field PATCH; `genetic_mutations` and the `genomics_*` recipes are the entries that use it |

  `projection` is optional even for `patient_record`: it is present when the
  write projects into a clinical table, and absent for a direct column with no
  approved mapping yet. A client must not treat its absence as read-only.
- `answerable: true` permits the explicit-answer actions **and nothing else**.
  Such an entry is `writable: false`; its write is its own `endpoint`. A
  client must never PATCH an `answerable` field.
- `writable` and `answerable` are never both true.

An `explicit-answer` entry carries `endpoint`, `actions` and one
`action_labels` string per action. An action without a label is an incomplete
contract: a client that refuses to invent clinical wording drops it, so the
producer must label every action it offers.

### Inputs

`inputs` lists what a computed value is derived from. An input is either:

- another descriptor key (a field the client can offer), or
- a **resource**: `Episode`, `EpisodeEvent`, `PersonLanguageSkill`, or a
  wearable metric the device pipeline owns (`steps`, `spo2`, `resting_hr`, …,
  see `DESCRIPTOR_RESOURCE_INPUTS`).

Naming an input is navigation metadata, not permission to write it. The client
decides whether to offer the subform; it should not when no input is writable
(the dialog would contain only demographics or nothing at all).

### Recomputed values

A `computed` entry is never authored directly: writing it would be replaced by
the next derivation. `overridable: true` marks a value the matcher infers but
the patient may replace; the client offers an editor with the reason “inferred
by default; enter a value to override”. `normalised_read_model: true` marks a
value derived from a source pair (a lab value and its unit): edit the pair.

## Canonical mapping

- `alias` entries are one-way and never point at another alias
  (`__descriptor__` tests enforce it). `canonical` is the only key a write may
  touch.
- The trial catalog's legacy spelling `cytogenic_markers` (no second “t”) is
  an alias of `cytogenetic_markers`. The canonical is the read-only summary;
  individual findings are recorded in Genomics.
- `refractory_status` is an alias of `treatment_refractory_status`.
- `ethnicity` is **not** an alias of `race`, and race is not aliased to it.
  They are separate OMOP concepts. The matcher maps `race` to the legacy ULN
  vocabulary at its own surface; see cancerbot-org/cancerbot#5777.

## Readback

Every write path has one authoritative read. Hosts re-read that source and
re-normalise it; `store.load()` alone is not a host notification.

| write | authoritative read | host action |
| --- | --- | --- |
| `PATCH /api/v1/patient-records/<person_id>/` | `GET {base}/patient-info/me/` for the signed-in patient, or `GET /api/v1/patient-records/<person_id>/` when the host edits someone else | re-read, normalize, invalidate the trial list/detail |
| explicit answer endpoint (`preexisting-conditions-answer`, `negative-answer`) | the answer's own GET plus the record read above | re-read, invalidate |
| therapy line / episode endpoints | the record projection and the episode list | re-read, invalidate |
| genomics findings endpoints | the findings list | re-read, invalidate |

`{base}` is where the host mounts the API (`/api`, or the host's federation
prefix). The shared-form client sends `X-Patient-Form: 1` and asks
`{base}/patient-info/me/`; `me` is the signed-in patient and is not a
substitute for reading a different `<person_id>`.

A successful PATCH response proves the batch was accepted, not that every
field was applied: `write.applied` and `write.rejected` are per field, and a
computed or deferred field can be present in `patient_info` with an unchanged
value.

## Producer tests

`tests/test_descriptor_contract.py` pins the envelope, alias shape, options,
explicit-answer labels and input resolution. `tests/test_writable_field_descriptor.py`
pins the exact field inventory. Change one of those expectations deliberately,
with the schema version if the meaning changed.
