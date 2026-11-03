# Concept to source code plan

Tracking issue: [#1341](https://github.com/healthkey-ai/promop/issues/1341).

The table-based workflow is described in the
[implemented architecture](concept_to_code_architecture.md). Its delivery
includes standard-concept browsing, existing source-code coverage, local
reverse retrieval, optional ranking, explicit selection-based proposal and
approval, and default-on Seen > 0 filtering. Preview-only retrieval replaces
the original plan's automatic writes of proposed mappings, so searching cannot
replace an imported proposal or a curator decision. Ranking reuses the existing
source-to-destination checks for each candidate.

## Remaining: graph visualization

Add an optional graph view for the selected concept without changing curation
semantics:

- Return bounded ancestor relationships and linked source codes from a graph
  endpoint, with explicit truncation/pagination metadata.
- Lazy-load a graph component, distinguish approved/proposed/rejected sources,
  and preserve concept selection when switching between table and graph.
- Reuse the same source preview, revision checks and approval API.
- Test graph navigation, statuses, accessibility and large source-code sets.

The table workflow is the core curator workflow; the graph remains a separate
follow-up under #1341.
