# Concept to source code curation

The Code Mapping page supports two ways to review the same mappings. Source
Code → Concept starts with an incoming code. Concept → Source Code starts with
a current standard OMOP destination and reviews its source-code coverage.
Storage and ingestion still resolve source codes to concepts through
`SourceCodeConceptMapping` (SCCM).

## Choosing a destination

The default catalog contains distinct standard concepts referenced by
non-rejected `FieldConceptMapping` rows. Several patient fields can share one
concept without multiplying its source-code counts. Curators can instead search
all standard concepts, including drugs without a patient-field mapping.

Destinations must have `standard_concept = 'S'`, no invalid reason, current
validity dates, and one of the five supported clinical domains. The list
supports name, code, OMOP ID and patient-field search, domain filtering and
bounded pagination. Coverage counts include all approved, proposed and rejected
SCCM rows currently targeting each concept, including zero-Seen rows. These
counts describe stored mappings, not proof of clinical equivalence.

## Reviewing sources

The selected concept has three views:

- Existing mappings: SCCM rows already targeting that concept.
- Find source codes: other proposed rows with compatible or unspecified source
  domain/table, including imported proposals with nonstandard destinations.
- Suggested source codes: a saved preview retrieved from local vocabulary
  evidence. No SCCM rows change when this search runs.

Source rows default to Seen > 0 and sort by Seen descending. The checkbox above
the Seen header includes zero-Seen rows when unchecked. Seen represents the
stored occurrence counter, not a count of distinct patients. Search, pagination
and changes to the filter clear the current selection. A changed Seen filter
requires a new suggestion search to retrieve additional candidates.

Reverse retrieval uses installed UMLS same-CUI siblings, matching both code and
source vocabulary, and indexed trigram searches over SCCM descriptions using
the destination's name and up to five synonyms. Shared CUIs and similar names
are retrieval evidence, not automatic equivalence decisions. The candidate
pool is capped and places encountered sources before zero-Seen sources.
When zero-Seen sources are included, retrieval also searches current installed
OMOP source concepts, publisher vocabulary terms (including their indexed
synonyms), and UMLS siblings that do not yet have an SCCM row. OMOP sources must
share the destination's clinical domain; publisher/UMLS records cannot bypass
known OMOP retirement or domain mismatches. Existing approved, rejected,
reference and already-linked mappings are excluded. An editable existing SCCM
row retains its description and Seen count. Vocabulary-only candidates have
Seen = 0 and display as **Not yet mapped**; retrieving them creates no mapping.
For LOINC names with a bracketed property, lexical retrieval also searches the
complete component before that property. This lets short source labels such as
`Albumin (g/dL)` reach review for `Albumin [Mass/volume] in Serum or Plasma`
without requiring an installed short synonym. The full destination, including
specimen and method, remains the target for ranking and explicit approval.
Optional Anthropic/Jev ranking evaluates each source against the chosen
standard destination, with no first-candidate fallback when ranking is
unavailable. All candidates still require curator review.

`SuggestRun.direction` separates reverse previews from forward runs. The
existing Celery/inline dispatcher persists progress and preview snapshots for
polling. Each queued run accepts at most ten concepts and at most 100 ranked
candidates across those concepts. Inline runs accept one concept and at most
three ranked candidates. Each concept's retrieval has a shared SQL time budget
of 30 seconds queued or 8 seconds inline. Failed runs retain partial previews
and report failure. A completed/running reverse run is not restarted by task
redelivery.
If a worker was lost after starting a reverse run, redelivery marks that run
failed with an interrupted-search message and preserves its partial preview so
the curator can retry. A redelivery before the run started still executes it.

## Saving and approving

Curators select displayed sources and choose Propose selected or, for staff and
organization admins, Approve selected. The client processes one source per
request and reports successes and failures separately. Each request locks the
SCCM row in a transaction and requires its exact `updated_at` revision from the
preview. Concurrent edits, approved/rejected rows, Athena reference rows,
active locks held by another user and domain mismatches prevent the write.

A vocabulary-only candidate must first be proposed using its saved reverse
preview run and source identity. The server reloads the installed source and
checks its revision, current validity and domain compatibility. Changed sources
or a mapping created since the preview require a refreshed search. A successful
proposal creates one SCCM row, with the full label retained in notes if it
exceeds the description field. It does not repoint clinical rows. Approval is a
separate explicit action using the new mapping's exact revision.

The API reuses `_upsert_source_code_mapping`: proposals retain source evidence;
approvals record reviewer information, preserve suggestion-review semantics,
repoint applicable clinical rows and mirror approved concept relationships
where supported. Approval therefore has the same effects as the existing
source-first editor. Retrieval reads each deployment's installed tables;
only an explicit proposal creates a source row from a vocabulary candidate.

When a curator changes a proposal's destination, SCCM retains its prior
destination IDs in `pending_repoint_concept_ids`. Approval from either editor
then updates matching clinical rows on those destinations as well as concept 0,
and clears the pending IDs in the same transaction. A failed update rolls back
both the mapping and the clinical changes. Pending IDs stay attached to the
same source identity and table until its review is resolved. This records
changes made after deployment; it does not reconstruct past proposal edits.

## API and schema

All endpoints require an authenticated professional role:

| Endpoint under `/api/v1/` | Purpose |
| --- | --- |
| `GET concept-to-code/` | Concept catalog, fields and coverage counts |
| `GET concept-to-code/<concept_id>/` | Paginated linked or available sources |
| `POST concept-to-code/suggest/` | Queue a reverse preview; returns 202 |
| `GET concept-to-code/suggest-runs/<run_id>/` | Preview progress and candidates |
| `POST concept-to-code/<concept_id>/mappings/` | Propose one vocabulary source from a saved preview |
| `POST concept-to-code/<concept_id>/mappings/<mapping_id>/` | Propose or approve one reviewed source with a revision precondition |

Migration 0264 adds the run direction and pending destination IDs with database
defaults for older writers, and builds a GIN trigram index on uppercase source
descriptions concurrently.
It changes no mappings. The remaining visualization work is tracked in the
[concept-to-code plan](concept_to_code_plan.md).
