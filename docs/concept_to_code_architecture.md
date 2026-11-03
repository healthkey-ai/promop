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

The selected concept has two views, and opens on the second (#2010):

- Existing mappings: SCCM rows already targeting that concept, grouped by
  status. Approved mappings come first, folded, then the proposed mappings
  open for review, then rejected ones, folded. Each group is fetched and paged
  on its own; the group headers carry the counts of all three under the current
  search and Seen filter.
- Suggested source codes: a saved preview retrieved from local vocabulary
  evidence. No SCCM rows change when this search runs, with one exception: a
  candidate whose units imply several LOINC axis sets (the blue "n axis
  values") has each axis variant without a destination suggested a standard
  LOINC with the run's ranker, stored as proposed (#2012). The variants are
  listed under the code with their axes, units and destination, and an
  approver can approve each from the row; approving re-points that variant's
  stored rows. Curator review (no ranker) lists the variants and suggests
  nothing.

The API still serves `mode=available` (other proposed rows with a compatible or
unspecified source domain/table); the dialog no longer shows that list, since
Suggest reaches the same rows with the ranker's evidence attached.

Source rows default to Seen > 0 and sort by Seen descending. The checkbox above
the Seen header includes zero-Seen rows when unchecked. Seen represents the
stored occurrence counter, not a count of distinct patients. On a seeded
instance it starts as the hospital-code extract's record count, which counts
each coding of an observation; the rebuild walk (#2043, #2121) replaces it
with the sum of this instance's own per-patient rows for the code, per
vocabulary, so a code never loaded here keeps the extract's number and one
that was shows what this instance has seen. Search, pagination
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

Staff and organization admins select displayed sources and choose Approve
selected; there is no separate Propose step (#2010). Other curators open one
code at a time in Edit Mapping. The client processes one source per request and
reports successes and failures separately. Each request locks the
SCCM row in a transaction and requires its exact `updated_at` revision from the
preview. Concurrent edits, approved/rejected rows, Athena reference rows,
active locks held by another user and domain mismatches prevent the write.

A vocabulary-only candidate is first proposed using its saved reverse preview
run and source identity, then approved on the revision that proposal returned;
Approve selected makes both requests, and a proposal that fails leaves the row
as it was. The server reloads the installed source and
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
| `GET concept-to-code/<concept_id>/` | Paginated linked or available sources; `status=` filters one status and `status_counts` counts all three |
| `POST concept-to-code/suggest/` | Queue a reverse preview; returns 202 |
| `GET concept-to-code/suggest-runs/<run_id>/` | Preview progress and candidates |
| `POST concept-to-code/<concept_id>/mappings/` | Propose one vocabulary source from a saved preview |
| `POST concept-to-code/<concept_id>/mappings/<mapping_id>/` | Propose or approve one reviewed source with a revision precondition |
| `GET concept-to-code/axis-variant-walk/` | The active axis variant walk, the last one, the patients loaded since it began, and the axis evidence it counted (#2050, #2051) |
| `POST concept-to-code/axis-variant-walk/` | Ask for a rebuild walk; 202 with `created: true`, or 200 with the walk already queued or running. Approval rights required |

### A run moves on

A run retrieves the best `limit` sources for the concept and asks the ranker
about each. One the ranker declines gets no confidence and the tab hides it.
Each decline is recorded (`ReverseSuggestDecline`: concept, mapping or
vocabulary key, ranker, run), and later runs and the estimate skip declined
sources in the query itself, so the next Suggest reaches the sources the last
one did not. The estimate reports `declined` and `declined_skipped`; the run
event carries `declined`. `include_declined` on the suggest and estimate
requests ("Retry declined" on the tab) searches them again. Curator review
(no ranker) declines nothing, and a decision on the mapping takes the source
out of the pool anyway (#2034).

Migration 0264 adds the run direction and pending destination IDs with database
defaults for older writers, and builds a GIN trigram index on uppercase source
descriptions concurrently.
It changes no mappings. The remaining visualization work is tracked in the
[concept-to-code plan](concept_to_code_plan.md).

### The source-code walk panel

Unit counts and axis variants are what the last walk left (#2043): nothing
re-runs it when patients are loaded. The panel beside the Source-code coverage
heading shows when the last walk began, how many patients have source codes
first seen since then, and a button that records a rebuild request
(`axis_variant_jobs.request_refresh(rebuild_evidence=True)`) for the worker.
One walk at a time: while one is queued or running the panel shows its
progress instead and polls every ten seconds. Starting a walk needs approval
rights, because it costs the worker hours on a large instance; every
professional role sees the panel.

A rebuild also counts, over the codes it walks, how much evidence each LOINC
axis has (`omop_core/mapping/axis_evidence.py`, stored on the request as
`axis_evidence` with every batch): codes with the axis settled and from what
(curator entry, LOINC source code, LOINC coding, unit, quantitative value,
specimen), codes with only a hint for it, and codes whose patients disagree on
it (units implying different properties, codings naming different tests,
specimens naming different systems, different method texts). The panel shows
the last completed walk's table. Units settle property and scale; if no other
axis is ever settled or varies, the data holds no evidence for more than one
LOINC per unit variant, which is what decides whether the multi-axis
capability is worth extending (#2051).
