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

`GET /api/v1/concept-to-code/` takes two parameters for this and returns four
keys beside each row's `category`:

| Parameter | Values | Effect |
|---|---|---|
| `order` | `category` (default), `priority` | `category` sorts by category, then by curator priority, Done last, then name. `priority` is the global ranked list. Anything else is a 400. |
| `category` | a category, or `__none__` | narrows to that category under the same definition; combines with every other filter; an unknown value matches nothing |

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

A grouped list has one header row per category, spanning the table: a button
with the category ("Uncategorized" for none) and the number of concepts in it
across every page, with the number on this page when that is fewer. A category
that began on an earlier page says "(continued)"; the page works that out from
the sizes of the categories before it.

Every category starts folded: only the headers are drawn, and a line above them
says "Categories are folded: open one to see its concepts, or Expand all."
while all of them are. Clicking a header opens that category's rows, and
Expand all and Collapse all act on every category of the filtered list. Opening
is a matter of what is drawn: the rows of a folded category are still part of
the page, so a folded category frees no room for more concepts. Drag-to-reorder
needs visible rows, so it works among the categories that are open.

Priority remains one list across every category. Typing a number, the arrows
and a drop onto a row in another category all set the global position, and the
list is read again in the server's order.

A list too long to group shows no headers and a line saying so, with how to
narrow it: a domain, a disease or a search.

The order and the opened categories are remembered with the chosen domain, per
account in the browser (`promop-concept-to-code:<user id>`), as
`{"domain", "order"?, "expanded"?}`: `order` when it is `priority`, and
`expanded`, the categories opened under each domain (`""` is Uncategorized).
A category that is not listed is folded, so a new account, a new domain and a
category never seen start folded. A stored part of the wrong shape is ignored
without discarding the rest, and an older `collapsed` list, which recorded what
was shut, is not read.
