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

## Categories

Every concept on the list is filed under a category, shown as the first
column, so that a long list can be worked a category at a time (#2690). The
category is computed in the list query from data the instance already holds;
nothing is stored for it.

| List | A concept's category |
|---|---|
| Wearables domain | the wearable metric category most of its wearable mappings carry |
| any other, LOINC concept | its LOINC class, by display name |
| any other, any other concept | its OMOP concept class |

- **Wearables.** The non-rejected `SourceCodeConceptMapping` rows in the
  wearable vocabularies that point at the concept vote with their `category`;
  blanks do not vote, the most frequent wins, and a tie goes to the category
  that comes first in `WEARABLE_METRIC_CATEGORIES`. A concept none of whose
  mappings has a category has none.
- **LOINC.** `LoincCodeClass` gives the code's class and `LoincClass` its
  display name ("Chemistry - non-challenge"). A class whose display name is
  blank shows its code; a LOINC code with no class row falls back to the
  concept class.
- **Everything else.** `concept_class_id` ("Disorder", "Ingredient",
  "Procedure").

`omop_core/services/concept_to_code.py::concept_category(params)` is the one
definition: `browse_concepts` annotates it as `category`, and the order, each
row's value, the counts and the filter read that annotation.

### Order, grouping and the limit

`GET /api/v1/concept-to-code/` takes three parameters for this and returns four
keys beside each row's `category`:

| Parameter | Values | Effect |
|---|---|---|
| `order` | `category` (default), `priority` | `category` sorts by category, then by curator priority, Done last, then name. `priority` is the global ranked list. Anything else is a 400. |
| `category` | a category, or `__none__` | narrows to that category under the same definition; combines with every other filter; an unknown value matches nothing. This is how the page asks for the rows of one opened category, paged by `page` and `page_size` within it |
| `view` | `categories`, or absent | `categories` answers the headers alone (#2699): the whole envelope (`grouped`, `categories`, `order`, `group_limit`, `total`, `prioritized`, `domains`) with `results: []`, and no row, field, lock or note is read, so it costs 4 queries whatever the list holds (8 with rows). Every filter applies to the counts. A list that is not grouped has no headers, so it returns its page of rows as without `view`. Absent or empty is the list as before; any other value is a 400 |

| Response key | Meaning |
|---|---|
| `order` | the order the rows are in |
| `grouped` | true when the rows are in category order |
| `categories` | when grouped, `[{category, count}]` for the whole filtered list in display order; otherwise null |
| `group_limit` | the largest list that is grouped |

Categories are ordered by `WEARABLE_METRIC_CATEGORIES` under Wearables and
alphabetically, without regard to case, elsewhere; concepts with no category
come last.

Sorting and counting by a computed value reads the whole filtered list, and
"All standard concepts" with no domain is millions of concepts. So a list is
grouped only while its total is at most `CONCEPT_CATEGORY_GROUP_LIMIT` (5,000).
A longer list, whatever `order` asked for, comes back in priority order with
`grouped: false` and `categories: null`, and each row still carries its
category. The total is counted once and decides both the paging and the order;
the count itself never computes a category unless `category=` filters on it.
The per-category summary is one GROUP BY, so the endpoint reads the same number
of queries however many concepts it lists.

### On the page

A grouped list is **one list of headers, never paged**: one row per category,
spanning the table, a button with the category ("Uncategorized" for none) and
the number of concepts in it. The page asks for it with `view=categories`
(together with the filters) and fetches no concept row until a category is
opened, so with every category folded there is one request however many
concepts the list holds, and the page never fills with headers.

Every category starts folded, and a line above them says "Categories are
folded: open one to see its concepts, or Expand all." while all of them are.
Clicking a header opens that category: the page asks the same endpoint for
`category=<name>` with the current filters, `page` and the page-size selector
(50, 100, 250, 500), and draws that category's rows under its header. Each
open category pages on its own: a "Pages of <category>" control sits under its
rows and only when the category holds more than one page ("unless the screen is
full"), and a filter or a page-size change starts every open category over at
its first page. Open categories are independent requests, with no more than six
in flight at once (`MAX_CATEGORY_REQUESTS`); the rest wait their turn. Folding a
category discards its rows and its page. A write (priority, Done, Lock, a note,
a saved mapping) asks again for the headers and for the open categories only,
and a saved note shows on its row before that reload.

Collapse all folds every category of the list. Expand all opens them all, one
request each, so it is offered only for a list of twelve categories or fewer
(`EXPAND_ALL_LIMIT`) and is disabled, with a title that says why, above that. The
categories remembered as open are fetched when the page loads, after the
headers, and no others. Drag-to-reorder needs visible rows, so it works among
the categories that are open. Under the Wearables domain neither button is
shown and the folded line ends at "its concepts." (#2795): the row holds From,
To and the files there, and each category opens and folds from its own header.

Priority remains one list across every category. Typing a number, the arrows
and a drop onto a row in another category all set the global position, and the
list is read again in the server's order.

A list too long to group shows no headers and a line saying so, with how to
narrow it: a domain, a disease or a search.

The order and the opened categories are remembered with the chosen domain, per
account in the browser (`promop-concept-to-code:<user id>`), as
`{"domain", "order"?, "wearables_order"?, "expanded"?}`: `order` when it is
`priority`; `wearables_order` likewise, the order chosen under the Wearables
domain, which is kept apart so that Wearables opens by category whatever the
clinical domains use (#2795); and `expanded`, the categories opened under each
domain (`""` is Uncategorized).
A category that is not listed is folded, so a new account, a new domain and a
category never seen start folded. A stored part of the wrong shape is ignored
without discarding the rest, and an older `collapsed` list, which recorded what
was shut, is not read.

## Approval needs a second person (#2726)

Approval is the one mapping transition that rewrites stored patient data, so
the curator who **proposed** a destination may not **approve** it. Someone with
approver access (`_can_approve_mappings`: staff and org admins) who did not
propose it signs it off.

**Who is the proposer.** `SourceCodeConceptMapping.proposed_by` and
`SourceCodeAxisVariant.proposed_by` name the person behind the destination now
on the row. A curator's save stamps it when it

- creates the row (the dialog, a file upload, Concept to Code propose),
- moves the destination (the new destination is theirs), or
- re-opens a rejected row as proposed.

A note fix by anyone, an un-approval and an approval leave it as it is, so the
person who tidies a typo does not take the proposal over and the person who
proposed is not freed by someone else's edit. A **machine** proposal records no proposer: Suggest (single, group and
Replace), reverse Suggest's auto-propose, a Concept to Code proposal or apply
whose candidate a ranker **chose** (verdict `supported`, not declined; a candidate that was only retrieved, e.g. with the `none` ranker, is the curator's own pick) (`machine_proposal=True` on the writer), an
importer, Athena, and a service credential (an `urn:service` identity is not a
person). A machine that moves a destination takes the name off (Suggest only when the destination actually changes, so a re-run that re-confirms a curator's own pick leaves them as proposer), except that a
re-point started by the person who proposed the row keeps their name, so a
re-point cannot launder it. Any approver may sign off a machine proposal,
including the one who ran the Suggest; a curator who replaces its destination
by hand becomes the proposer of the replacement. A CSV row with no destination
proposes nothing. Rows proposed before this change have no proposer (no
backfill: `created_by` and `updated_by` say who last touched a row, not whose
destination it is), so the rule starts with the next proposal.

**What is refused.** An approval whose proposer is the approver, on the writer
every human save goes through (`_upsert_source_code_mapping`), before anything
is saved: a mapping dialog or list save, the Concept to Code apply, a patient
page save. Proposing and approving in one request is the same person on both
sides and is refused too: creating a mapping already approved, or changing the
destination and approving in one save. The way through is two steps by two
people, or a save as proposed followed by another approver's. The answer is a
403 `{"detail": "You proposed this mapping, so another approver must approve
it. …", "code": "self_approval"}`. Elsewhere:

| Path | Behaviour |
|---|---|
| group Approve (`group_mapping_jobs`) | the actor's own proposals, and members the group's destination would move (which would make the actor their proposer), are skipped and stay proposed; the run's `done` counts only the codes decided and its activity carries `{action: 'skipped', count}`, which the banner reports |
| axis variants (`axis_variants.set_destination`) | the same rule per variant; Suggest passes `machine=True` and records no proposer |
| code mapping file upload | a file that approves a row the uploader proposes (a new row, or a moved destination) is refused whole, one error per row; approving someone else's proposal with its destination unchanged is allowed |

**What the page does.** Row payloads carry `proposed_by_id`, null when the rule
is off or for a machine reader (a service token), and `code-mappings/reference/`
carries `separate_approver`. The list's Approve button and the Edit Mapping
dialog's Approved option are disabled for the proposer, with the reason
(`isOwnProposal`); with the rule on the dialog also disables Approved when the
save would create the row or move its destination ("save as proposed first"),
and the inline picker's "Save & approve" is hidden, since each would be refused
in the same request. Concept to Code's batch Approve is not predicted: a source
taken from a ranker preview approves, a hand-picked one shows the server's
message.

**The switch.** `MAPPING_SEPARATE_APPROVER` (`promop/settings.py`). Unset, it is
on wherever `DEBUG` is off and off under `DEBUG`, which a one-person local
instance and the test runs use. Set it to `true` or `false` to override either
way. **With it on, one administrator cannot approve anything they proposed**,
including creating a mapping as approved: an instance run by a single
administrator sets `MAPPING_SEPARATE_APPROVER=false`.

**Not covered.** Field mappings and custom fields (`FieldConceptMapping`,
`POST /api/v1/custom-patient-fields/`): adding a field creates an approved
mapping by design (#721) and would need its own decision. `proposed_by` is a
user id, so `copy_reference_data` does not carry it, as with `reviewer`.

Tests: `tests/test_mapping_separate_approver.py`,
`EditMappingDialog.ownProposal.test.tsx`.
