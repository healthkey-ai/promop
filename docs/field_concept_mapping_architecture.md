# Field concept mapping architecture

Implemented behavior reviewed against `dev` at `10c5644` on 2026-09-17.
This document explains how UI fields, PatientRecord and OMOP mappings interact.
Future schema, mapping review, repairs and delivery gates belong to the
[field concept mapping plan](field_concept_mapping_plan.md).

## Reference ownership

| Reference | What it owns |
|---|---|
| This architecture | Implemented mapping models, runtime resolution and field ownership |
| [PatientRecord-first writes](patient-record-first-writes.md) | Detailed PATCH, projection, clear and refresh contracts |
| [Generated field/value inventory](field-mapping-inventory/README.md) | Reproducible source rows, mappings, candidates, context and coverage evidence |
| [Mapping plan](field_concept_mapping_plan.md) | Unresolved meanings, proposed capabilities, dependencies and acceptance |

The generated inventory is the field-by-field evidence source
([PR #1284](https://github.com/healthkey-ai/promop/pull/1284), merged). Its
[manifest](field-mapping-inventory/manifest.json) includes source revisions,
field and option identities, existing recipes, candidate evidence and validation
flags; its [coverage report](field-mapping-inventory/coverage.md) states the
export date and omissions. It is incomplete and does not clinically approve
candidates. Deployment-specific recipes must be checked against that deployment's
current mapping and vocabulary tables. Do not copy snapshot concept IDs into a
second manually maintained UI-to-concept table.

## Implemented data model

| Model | Current responsibility |
|---|---|
| `FieldConceptMapping` | Field-level recipe: concept, destination, source key, value kind, units, type, review state and provenance |
| `FieldChoice` | An allowed value with a stable identity `(field_name, context_key, code)`, an editable label, a typed canonical `value`, ordering, a retired mark and creator metadata (#1224) |
| `FieldChoiceAlias` | Another spelling of a choice: an earlier label or a source's wording; one spelling names one choice within a field and context |
| `FieldChoiceDisease` | The Disease Categories that offer a choice; no rows means every disease |
| `FieldValueConceptMapping` / `FieldValueConceptMappingRevision` | The standard concept an answer means in a role, or the reviewed statement that it has none; one decision per row, with a transition log (#1224) |
| `FieldChoiceCode` | Vocabulary/code aliases for a choice, with a primary flag; this does not independently approve an answer mapping |
| `CustomPatientField` / `FieldFormula` | Dynamic field definitions and computed values using the existing mapping/formula services |
| Therapy reference models | Regimen, component, class and disease/round relationships, resolved by their dedicated authoring paths |

The models are in [omop_core/models.py](../omop_core/models.py). A choice has
stable, scoped identity and a reviewed binding to a standard concept (both
below). Nothing that writes patient data reads the binding yet.

### Scoped choices: `FieldChoice` identity (#1224)

A choice used to be known by its label, which a curator can edit, and offered
to every patient. Its identity is now `(field_name, context_key, code)`;
`omop_core/services/field_choices.py` holds the rules and writers use it
rather than the models.

- **`code`** is made once from the label (`stage_iv`, then `stage_iv_2` for a
  label that slugs alike) and never changes: the API refuses a different code
  on PATCH, and a label edit goes through `rename`. Migration `0365` gave every
  existing row its code, in sort order, before the unique constraint was
  added, so it does not depend on row order and a replay changes nothing.
- **`context_key`** separates answers of one field that mean different things
  (stage `I` in the ISS system and in Ann Arbor); blank when there is one
  meaning. The label is unique within a field and context, not within the
  field, so both can exist.
- **`value` / `value_type`** are the canonical storage value and how it is read
  (`text`, `integer`, `decimal`, `boolean`); blank `value` means the label,
  which is what every seeded row stored. A value the type cannot hold is a 400.
  **A save always stores the value** (`stored_value`, the value or the label while
  there is none), never the label as it reads now: `rename` freezes the label
  being replaced into a blank `value` at the first rename (lazily, so an answer
  nobody renames is untouched and no migration rewrites rows), and a later rename
  leaves it. The write descriptor's options therefore carry `value` (what is
  stored) and, only when it differs, `label` (what a person reads); the patient
  select shows the label and saves the value, derivation reads a concept-only
  fact back as the value, and the projection already matched the value, the
  label and every alias. The editor shows the value read only ("Saved as"); a
  `value` sent through the API on purpose still wins. A curation transfer copies
  the source's `value`, so a source that has frozen one gives the target the same.
- **Aliases.** A rename keeps the old label as an alias (`source='label'`), so
  stored data that still reads it resolves; `set_aliases` replaces the curator's
  own spellings and keeps those. A spelling another choice of the field and
  context answers to, as its label, code or alias, is refused by name, and the
  database holds the alias rule (`uq_field_choice_alias`).
- **Scope.** `FieldChoiceDisease` rows name the Disease Categories that offer a
  choice, validated against the categories (`Other` refused, its profile is the
  base record). `offered(field, disease)` returns the unscoped choices plus the
  disease's own, retired ones left out; `GET /api/v1/field-choices/?disease=MM`
  is the same read. `resolve(field, text, disease)` finds the one choice a
  spelling names for that disease and raises `AmbiguousChoice` rather than
  guess when two are in scope. Scope hides nothing already stored.
- **Retired** (`retired_at`) stops a choice being offered; it still resolves.
- **Shapes.** The API and the field descriptors keep their old keys and gain
  `code`, `context_key`, `value`, `value_type`, `retired`, `aliases` and
  `diseases`. The write descriptor still lists an answer once however many
  contexts hold it; which meaning a record means is scope's business, and
  enforcing it on write is #2333. It also still offers the label, not `value`,
  and keeps retired choices, because a retired value already stored must still
  save when an edit echoes it back; honouring `value` on write and hiding
  retired values from new entries belong with that enforcement.
- **Transfer.** The curation payload carries the new keys. A choice is matched
  by code, so a rename on the source renames it here instead of adding a
  second choice; a payload from before this matches by label as it did, and so
  does a coded row whose code this instance does not have, when the local
  choice's own code is not one the payload names (two instances that numbered a
  label that slugs alike differently); it then takes the payload's code.
  Labels about to change are moved aside first, so a rename that hands its old
  label to another choice, or a swap, does not trip the label rule. A choice
  already retired keeps the date it was retired.
  Aliases and scope are replaced from the source, and a disease this instance
  has no category for is dropped.
- **Not here.** The editor shows none of this (#1225), and the disease tab's
  `stage` options are still the hand-written ones (#2332).

### Answer-to-concept mappings: `FieldValueConceptMapping` (#1224)

`omop_core/services/field_value_mappings.py` holds the rules; writers use it
and never the model.

- **One row is one decision** about one choice in one **role**:
  `value_as_concept` (the answer is the value of the field's question),
  `main_concept` (the answer is itself the fact) or `context_recipe` (the
  answer only means something with its own `question_concept`). The
  `outcome` is `mapped` (a `target_concept`) or `no_equivalent`, a reviewed
  statement with a reason and no target; the value stays saveable either way.
- **Review.** `proposed` / `approved` / `rejected`, plus `superseded` for an
  approval a later one replaced. The database holds *one approved and one open
  proposal per choice and role* and that an approved `mapped` row has a target
  (and that only a recipe has a question), so a change is proposed beside the
  decision in force and replaces it only when approved; the replaced row stays,
  superseded. Withdrawing an approval is a rejection. Decisions on one choice
  take turns (the choice row is locked), and a lost race is a 409.
- **A target is checked on the server**, by id or by exact vocabulary and
  code, never trusted from the client: it must be a loaded, current
  (`invalid_reason` blank), standard concept of a vocabulary that is not
  withdrawn, outside the domains that describe the record rather than a
  finding (Unit, Type Concept, Metadata, ...), and for `main_concept` in the
  domain of the table the field's mapping stores to. It is checked again at
  approval, since a vocabulary reload can retire it between the two.
  `target_state` on an approved row reports `ok`, `retired`, `non_standard`
  or `invalid` as the vocabulary stands now.
- **Release and revisions.** The vocabulary version the target was checked
  against is stored with the decision; every transition (proposed, edited,
  approved, superseded, rejected, transferred, migrated) is an append-only
  `FieldValueConceptMappingRevision` that holds the concept as ids and codes,
  with no foreign key, so a vocabulary reload cannot block it.
- **Migration `0368`** carries each choice's first primary `FieldChoiceCode`
  over as a `value_as_concept` **proposal** (never an approval), recording
  whether it resolved, was not loaded here (`unresolved`), is retired
  (`invalid`) or non-standard. The codes stay for source codings. A replay
  changes nothing. Existing choices without a concept remain usable. On a fresh
  instance the migration runs before any vocabulary, so every proposal starts
  unresolved; `load_athena_vocabularies` re-resolves them after every load
  (best effort, like the field mappings) and `manage.py
  resolve_field_value_mappings` does it by hand: a current standard concept
  becomes the target, a retired or non-standard one is recorded as such, one
  not loaded stays. Approved rows are never touched.
- **API**, on `/api/v1/` only: `GET`/`POST field-choices/<id>/value-mappings/`
  (read: anyone who reads the mapping pages; propose: mapping curators),
  `POST .../<mapping>/approve|reject/` (org admins and staff, as for field
  mappings), `GET .../<mapping>/revisions/`, and `GET
  field-choices/answer-coverage/` (`?field_name=`). A choice's own payload
  carries its current `value_mappings` for readers of the mapping pages only; a
  patient's dropdown never sees who reviewed what. Field descriptors gain
  `answer_coverage` (`choices`, `mapped`, `no_equivalent`, `proposed`,
  `unmapped`, over the choices still offered), kept apart from whether the
  field's question is mapped.
  The Field Mappings list shows it as an **Answers** column ("3 of 5 decided":
  a concept or a reviewed no-equivalent counts as decided; amber until all are,
  the tooltip gives the four counts, a dash for a field with no curated
  answers). It is the server's figure from the list's own response, so it is
  not the editor's stricter client-side count; for a reader who can write
  mappings it opens the field's answers.
- **Target search for the editor** (#1225): `GET
  field-choices/<id>/value-mappings/targets/?q=&role=&question=&vocabulary_id=`
  (readers of the mapping pages). `q` is three characters or more and matches a
  name, an exact vocabulary code or an OMOP id, as the curator's concept search
  does (`omop_core.api.concepts.concept_text_search`, shared). The server's
  rules decide what is listed (`field_value_mappings.search_targets`, the
  query form of `target_problem`): current, standard, outside withdrawn
  vocabularies and record-keeping domains, and for `main_concept` in the domain
  of the field's table; `question=1` (a recipe only) searches the question
  concept with no domain rule. So the editor offers nothing a proposal would
  refuse. The answer is the concept search's page plus `rules` (`required_domain`,
  `excluded_domains`, `excluded_vocabularies`) so the editor can say why a
  concept it knows is missing. Unlike the general search it never lists
  non-standard concepts authored on this instance: those are not valid targets.
- **In the choice editor** (#1225, `AnswerConceptPanel.tsx`): each answer shows
  where its concept stands, as one of five states (`answerState`): no concept
  decided, proposed (and when the proposed concept is not loaded here), approved,
  approved as "no standard equivalent", or approved but unusable (retired,
  non-standard or no longer fitting the field, which the panel says writes the
  value without a concept). A mapping curator searches (the target search
  above) and proposes, or records no equivalent with a reason; an org admin or
  staff approves, rejects or withdraws. Only `value_as_concept` is offered, since
  nothing projects the other roles. The panel shows the server's own refusal
  text (and reads the list again when a decision is refused, in case another
  curator decided first), keeps its place on a save rather than reloading the
  list, and the editor passes `canApprove` from the page.
  Beside it, `AnswerDetailsPanel.tsx` shows the answer's stable code and other
  spellings and lets a mapping curator change the label and the spellings (one
  per line) or retire and restore it, all through `PATCH field-choices/<id>/`:
  only what changed is sent, a new label keeps the old one as a spelling (the
  server's `rename`), the code never changes, and the server's refusal is shown
  as written (`refusalText` reads serializer field errors as well as `detail`).
  Retiring asks first; a retired answer is dimmed with a "Retired" mark and is
  still projected and read back. `AnswerHistory.tsx` lists every proposal,
  approval and rejection of the answer's concept, newest first, for readers of
  the mapping pages: one request for the decisions and one per decision for its
  revisions, made only when History is opened. Deleting an answer a non-approver
  cannot delete shows the server's reason, and the confirmation points to
  retiring.
  **Disease scope** is edited in the same form (#1225, #2332): the line above it
  says "Offered to every patient" or "Offered to: MM, FL" and names the answer's
  context when it has one, and the form reuses `FieldScopeControl` with its own
  wording ("Every patient" / "Only some Disease Categories"). `diseases` is sent
  only when the ticked set changed from what the form opened on; offering the
  answer to every patient sends `[]`; narrowing to no category is refused before
  asking the server; the categories come from the page (Other has no extension,
  so it is not offered) and without them the scope is shown, not edited. A
  category the list lacks but the answer already names stays ticked. Scope
  changes which answers a record of that category is offered; a value already
  saved is neither changed nor hidden.
  **Answer coverage** heads the editor for a reader of the mapping pages
  (`AnswerCoverage.tsx`, `answerCoverage` in `answerConcept.ts`), computed from
  the list the editor already holds so it costs no request. It is the stricter
  count: only the `value_as_concept` role is read, and an approved concept the
  server can no longer use ("needs attention") is not counted as mapped, which
  the server's `answer-coverage` totals do count as mapped. Each offered answer
  is in one of four groups: decided (a concept, or a reviewed no standard
  equivalent), needs attention, nothing approved or proposed, or retired
  (not counted). "Nothing approved or proposed" is not "writes no concept": an
  answer with a legacy code may still write that code's concept. **Waiting for
  approval** is separate and overlaps the groups: every answer with an open
  proposal, including a replacement for an approved concept that cannot be
  used, so an approver filtering on it finds the whole queue. The summary says
  it counts the answers only, the field's own question concept being mapped on
  the field. The chips list one group; a filtered view cannot reorder, the rows
  it listed when it was chosen stay listed (with their new state) until the
  filter changes, so a decision does not take its row away, and adding an
  answer returns to the whole list. The summary sticks to the top of the
  scrolling editor. The stored value is shown read only in the
  editor, frozen at the first rename (see `value` above); typing one is not
  offered. Still not here: concept mapping for the options of a therapy catalog.
  **Lookup options** (#1225, `LookupOptions.tsx`, `GET field-choices/lookup/
  ?field_name=`, `omop_core/services/option_sources.py`): nineteen fields draw
  their dropdown from a `VocabularyLookup` table, which until now only the
  frontend knew (`useVocabulary('her2-status')` beside the field). The editor
  now lists that lookup, read only and never copied into choices (so there is
  no second list to drift), with its source and a Show entries control. The
  binding is **enforced** when the field's approved mapping names the lookup in
  `value_vocabulary` and is complete enough to write (a concept and a table; five
  fields today): the write descriptor then offers only those titles, so answers
  added in the editor are not offered (the two transplant fields also have
  serializer and upload validators that keep only the lookup's titles),
  otherwise **offered**: the tab draws its dropdown from the lookup,
  and a field with an approved concept mapping and answers offers those
  instead, which the section says. `LOOKUP_FIELDS` must match the tabs;
  `tests/test_option_sources.py` fails when a `useVocabulary` call names a
  lookup no field uses, or an enforced seed names one it does not map.
  **What an entry means as a concept** is decided here too (#1225). Each entry
  row has a Concept control that opens the same panel an answer has: search,
  propose, approve, reject, withdraw, history. The decision is held by a
  `FieldChoice` made when a curator first decides something for the entry
  (`POST field-choices/lookup/answer/` `{field_name, code}`, mapping curators,
  `option_sources.ensure_answer`): its context is `lookup:<slug>`, its label the
  entry's title (which is also how the entry is found again) and its value what
  the tab stores, the entry's code for `CODE_VALUED` fields (ECOG, Karnofsky)
  and its title for the rest. Because it is an ordinary choice, propose,
  approve, revisions, the projection (`answer_concept_ids`: the title and the
  code both project), the readback (`answer_values_by_concept` returns the
  stored value for such a row), the curation transfer and the two-role rules
  all apply unchanged, and no migration was needed. It is **not an answer**:
  `lookup:` rows are left out of `GET field-choices/`, the descriptors'
  `choices`, the write descriptor's options, `answer_coverage` and
  `field_choices.offered`; they cannot be created, renamed, retired, scoped or
  given codes through the answer endpoints (400), and a `context_key` starting
  `lookup:` cannot be sent when adding an answer. `GET field-choices/lookup/`
  returns each entry's `choice_id` and `value_mappings` (null and empty for an
  entry nobody has decided anything for, so reading makes nothing) and
  `orphaned`: decisions about a title the lookup no longer has (a renamed
  entry starts afresh and its old decision stays, listed there). A row holding
  decisions cannot be deleted either (400, even for an approver): a decision is
  withdrawn or rejected, so its history stays. **A curated answer speaks over
  a lookup entry**: in both `answer_concept_ids` and `answer_values_by_concept`
  a lookup row fills only the spellings (and concepts) no curated answer of the
  field uses, so a decision about an entry can neither void nor override an
  answer the field has of its own. The response says whether the field's
  approved mapping stores a concept (`writes_concepts`): where it stores text
  (`value_kind` other than `concept`, as `her2_status` does today) a decision is
  recorded and reviewed but not written, and the section says so. The tab says
  how many entries have a concept decided. Not here: therapy catalogs, which
  are lookups of the same kind (`therapy-regimen`, `-component`, `-class`) but
  carry their own mappings, and reconciling a renamed entry with its old
  decision.
- **Transfer** carries the decision in force and the open proposal as
  vocabulary and code, re-resolved here. A concept this instance has not
  loaded, or that is not a valid target here, cannot be approved here: the
  decision already in force here is left as it is, and the source's answer
  arrives as an unresolved proposal with a warning (unless the source also has
  an open proposal for the role, which then takes the place). A decision the
  source no longer has in force is superseded (approved) or rejected
  (proposed), not deleted; a malformed row is skipped with a warning. Reviewers,
  review dates and the revision log are not copied.
- **Deleting an answer** removes its mappings and revisions with it, so an
  answer with an approved mapping can be deleted only by org admins and staff;
  retire it to stop offering it.
- **The projection reads the decision.** A concept-valued field's write recipe
  carries `value_concept_map`, `{spelling: concept_id}`, built by
  `field_value_mappings.answer_concept_ids` in a fixed number of queries
  whatever the number of choices. A spelling is the label, the stored `value`
  or any alias, so a value saved under an earlier label still projects, and a
  retired choice still projects. Per choice, in the `value_as_concept` role:
  - **approved, mapped** writes the target, provided it is still a current
    standard concept; a target a reload has since retired or made non-standard
    writes none (the value saves as text);
  - **approved, no equivalent** writes none, even where the choice has a legacy
    code;
  - **no decision, or only an open proposal** keeps the concept of its primary
    `FieldChoiceCode`, as before. The proposal migration 0368 made from that
    code is not a decision; holding projection until each is approved would stop
    writing concepts that are written today;
  - **a decision once made and gone** (an approval withdrawn or superseded), or
    a **rejection of the primary code's own concept**, does not fall back to that
    code: a reviewer answered for it. Rejecting some other proposal for the
    choice leaves the code in place.

  A spelling that two choices of one field (the same label in two contexts)
  resolve to different concepts, or one to a concept and one to an explicit "no
  concept" decision, writes none; a choice with no code and no decision says
  nothing, as before. Where a live choice and a retired one share a spelling, the
  live one decides.
- **Derivation reads the decision back.** A fact that carries the answer as a
  concept and no text (an ETL's `value_as_concept_id`) is read into a
  concept-valued field as the answer that concept stands for
  (`answer_values_by_concept`, called from `curated_values_from_snapshot`). It
  is the inverse of the projection above and takes its decisions from the same
  function, so an approved answer reads back as what it writes and a retired
  target, a no-equivalent decision or a withdrawn approval reads as nothing,
  and a choice nobody has decided keeps its primary code's concept, as in the
  projection. The value is the choice's label, which the editor offers and the
  projection stores. A fact's own text or number always
  wins; the concept is the fallback. A concept that two answers of one field
  share (or one live and one retired answer) is not guessed: the live answer
  speaks over a retired one, and two live ones read as nothing. The lookup is
  made once per refresh, and only when such a fact is met, so a refresh with
  none costs no query more. Other readers (the dedicated extractors) are
  unchanged.
- **Not here.** `main_concept` and `context_recipe` decisions are recorded and
  reviewed but nothing projects or reads them; the two-person rule is not
  applied (as for field mappings), there is no scoped question recipe beyond a
  recipe's own question concept, and no UI (#1225).

`FieldConceptMapping.provenance` distinguishes System Generated and Curator
recipes. Approval state is separate from origin: approving a recipe does not
change who supplied it. Migration 0239 backfilled `provenance = 'system_generated'`
on all 314 legacy rows that had blank provenance (created by seed migrations
0156–0227, before the provenance field existed). No blank-provenance rows remain
on staging. The mapping API and curation transfer preserve this distinction. A
recipe's OMOP `type_concept_id` is fact provenance, not this curation-origin label.

## Runtime field ownership

The backend [writable descriptor](../omop_core/services/write_descriptor.py) resolves
field ownership, allowed values and available projections from code and installed
mappings. Frontends consume that contract instead of selecting an OMOP table or
hardcoding a vocabulary ID. Patient authorization still constrains each operation;
a mapping cannot grant permission to edit a record.

| Field category / example | Implemented save or derivation owner |
|---|---|
| Direct scalar clinical field, such as `hemoglobin` | PatientRecord PATCH; a supported descriptor projection can also write OMOP |
| Direct scalar with no usable projection | Saved to PatientRecord with pending-edit protection; no fabricated concept or fact |
| Profile fields, including birth, contact and address values | PatientRecord PATCH and the backend Person/Location projection; `patient_name` is a virtual input |
| Alias, such as `absolute_neutrophile_count` → `anc_thousand_per_ul` | Canonical field is the editor; the alias has no independent write recipe |
| Lab field derived from Measurement, such as `hemoglobin_g_dl` | Routed by its approved `FieldConceptMapping` and that concept's approved `SourceCodeConceptMapping` LOINC aliases (`resolve_lab_code_fields`); `_LOINC_LAB_FIELDS` seeds those rows and decides only for a field with no concept-resolved mapping |
| Computed values, such as BMI and wearable aggregates | Derived from inputs or source events; no independent scalar assertion |
| Entered treatment courses and supportive therapy | Dedicated clinical resource writers; Episode/Event and drug facts supply summaries |
| `genetic_mutations` and priority Genomics findings | Canonical Genomics writer and approved parent/component mappings |
| `cytogenetic_markers` | Read-only legacy summary; discrete interactive findings belong to Genomics |
| Language skills, allergies, surveys and the OMOP browser | Dedicated resources with their own contracts |
| Dynamic fields | `PatientRecord.custom_fields` and their definitions/recipes; computed custom fields use formulas |

The descriptor includes structured Genomics cases as well as ordinary direct
fields. Do not infer that every writable field uses the generic scalar writer,
or that the presence of an enum supplies a coded-answer projection.

### Where a field renders: `FieldConceptMapping.tab`

A field's placement is one value, the patient tab it renders on
(`FieldConceptMapping.tab`, #2328; `FIELD_TAB_CHOICES` is the one list the
mapping row, `CustomPatientField` and the Field Mappings page share). It is
not a layout: a placed field renders last on its tab, on its own row.
`field_descriptor.resolve_tab` reads the mapping's tab, then a custom field's
own, then the tab the code gives a built-in field (`_classify_tab`), and the
descriptor emits the result as `tab`. A field nothing places is `omop`,
unplaced: it keeps its mapping and derivation, no tab renders a control for
it, and it is listed on the mapping page's OMOP (unplaced) tab so it is seen
rather than lost. The column travels with the mapping through
`copy_reference_data` and the curation fixture, and a payload written before
it existed reads as blank. Rendering placed built-in fields on the patient
tabs and the tab control for an existing field are #2330 and #2329.

### Adding a field from a patient tab

Staff and org admins, outside patient mode, see an **Add Field** link at the
top and bottom of each patient tab that holds PatientRecord fields
(`AddFieldLink.tsx`, shown under the same context as the fields' globes;
General, the disease tab, Treatment, Labs, Genomics, Behavior and Wearables).
It is a link, not a dialog on the patient page (#721):

```
/field-mappings?new=1&tab=<tab>[&disease=<Disease Category code>]
```

The disease tab adds the record's Disease Category
(`PatientDetail.tsx::DISEASE_CATEGORY_CODES`, the codes
`disease_profiles.SEED_DISEASE_FIELDS` uses). The URL names a tab and a
category and nothing about the patient; the page and tab to come back to
travel as router state, followed only to a `/patient/<id>` path.

The Field Mappings page opens `ConceptAssignDialog` in **create mode** on
that tab and scope: display name, field name, value type, editable or
computed (with its formula), tab, the concept search, OMOP table and unit
the dialog already has, the scope control, and the PatientRecord
confirmation. Every default can be changed; disease-specific with no
category ticked is refused. Saving is one `POST
/api/v1/custom-patient-fields/`, which writes the `CustomPatientField`, its
**approved** mapping, the tab and the membership rows in one transaction.
Because the field arrives approved, creating one is gated by
`_can_approve_mappings`: an analyst or a doctor gets 403, and neither the
link nor the page's own Add field button is offered to them. Saving or
cancelling a dialog the link opened returns to the patient tab.

A scope change takes effect at once and has no proposal stage, so it is an
approval too (#1225): `PUT field-mappings/<name>/diseases/` and a `diseases`
sent with a field mapping are 403 for an analyst, and the mapping dialog shows
the scope read-only to anyone who cannot approve. The same holds for an
existing answer's label, spellings, scope, retired flag and codes
(`PATCH field-choices/<id>/` with anything but `sort_order`, and
`field-choices/<id>/codes/`): a curator may add an answer (with no scope,
spellings, codes or retirement), reorder answers and propose a concept, and the
choice editor shows the rest to a non-approver read-only, with no code controls.
Deleting an answer is approvers only too: it takes the answer's codes,
spellings, scope and decisions with it, and an answer deleted and added again
plain would get around the rest. The editor shows no Delete to anyone else.

`CustomPatientFields.tsx` renders created fields last on their tab, for
every reader of the record, in the app's patient page and in the federated
editor (which offers no Add Field: the host has no Field Mappings page):
those placed on the tab (a field placed on Blood shows on Labs, where the
editor has blood counts) that are base or name the record's Disease
Category. The definitions are read once per page
(`useCustomPatientFields`). They are reference data, not an organization's:
a field one instance's admin adds is part of every record it is scoped to.

The record's Disease Category is what Disease Category management maps its
disease to (`useAllDiseaseMappings`), with the editor's own disease-type
match as the fallback for a name that mapping does not hold
(`fieldScope.recordDiseaseCategory`); a disease mapped to `Other` has the
base record only. That category, not the disease name, picks the disease tab
(#2332): a record shows the hand-authored section of its category (BC, FL, MM,
CLL and MCL have one; `patientVisibleFields.json`'s `disease_categories` names
them), and a category without one shows its created fields. The server's
`disease_profiles.record_category` is the same rule, so the attestation counts
the fields of the section on screen. The tab appears for a record when its
category has a section or a field placed on its extension, and for whoever can
add a field (staff and org admins) for any category, because the tab's Add
Field link is how the first field of an empty extension is made.

A record with more than one cancer (`PatientRecord.disease_slugs`, #2182) has the
extension of each, not only of the cancer `disease` names. The record read adds
`disease_categories` (primary first; `disease_profiles.record_categories`) when
there is more than one cancer, found through Disease Category management's names
(each made into its slug) with the keyword rules as the fallback for a slug it
does not hold. The disease tab is then one tab named "Diseases" with a section
per cancer under its own heading (a category with no hand-authored section says
its fields are under Additional fields), created fields say whose they are, and
the tab offers one Add Field link per cancer. The attestation and the therapy
tabs still read the primary cancer alone.

Inside the tab a control reads its field's membership
(`DiseaseProfile.tsx`, `ClinicalField`): a field of another disease's
extension that holds a value renders read-only with "Not part of the <disease>
record", is never cleared, and renders nothing when it is empty. Controls
that do not go through `ClinicalField` (GELF, FLIPI) are not held to
membership; they belong to the lymphoma section. Two limits remain with
#2332: a record with several cancers is read by its primary disease, and the
disease-scoped choice lists (#1224) are not done. The keyword match survives
only as the fallback for a disease name Disease Category management does not
hold, so a record whose name is not mapped keeps the tab it had.

A created field takes a value on the tab (#2669), along the
[PatientRecord-first path](patient-record-first-writes.md#created-fields):
the record PATCH carries it under `custom_fields`, it lands in
`PatientRecord.custom_fields`, and it is projected into OMOP through the
field's own mapping. The section renders an input for each field the
writable-fields descriptor lists as writable for this caller and this
patient, and a read-only box otherwise, with the server's reason when it
gives one (a field the descriptor does not list, as on a host served by an
older instance, is simply read-only); a computed field shows its formula's
result. A value already in a patient's OMOP data for the
field's concept (`_get_custom_patient_field_data`) appears when the record
is next derived, so a field added just now is empty on a record until then;
`manage.py backfill_patient_records --all` derives every record.

With provisional data on (#2857), the same reader runs over rows from proposed
mappings (`custom_field_answers`, `provisional_record.custom_proposals`): a proposal
fills a created field the record leaves empty, marked with its confidence in
`patient_record_confidence.custom_fields` (`{name: percent}`) and in the record read as
`provisional_fields["custom_fields.<name>"]`. A computed field is not proposed. The
[plan](provisional_data_plan.md#created-fields-as-built-2857) has the rules.

### Created dropdown fields

A created field can be a **dropdown**: `CustomPatientField.field_type = 'choice'`
(`FIELD_TYPE_CHOICES`, migration `0378`; the dialog calls it "Dropdown"). It reuses
the answer machinery of the built-in coded fields and adds no second answer system.

- **The answers are `FieldChoice` rows under the field's name** (`field_name` is a
  string, not a column), so the identity, code, stored value, aliases, retirement and
  `value_as_concept` decisions of [#1224](#scoped-choices-fieldchoice-identity-1224)
  apply unchanged. `POST /api/v1/custom-patient-fields/` takes `answers`
  (`[{label, value?, concept?}]`, in order) and creates, in the field's transaction, the
  field, its approved mapping (`value_kind='concept'`), its scope and the answers. An
  answer's concept becomes an **approved** `value_as_concept` decision
  (`field_value_mappings.propose` then `approve`, so the same target checks apply: a
  current standard concept outside the non-answer domains). The creator is an approver
  (`_can_approve_mappings`: staff and org admins; an analyst or a doctor gets 403).
  `FieldChoiceSerializer` accepts a created dropdown's name, so the Field Mappings answer
  editor adds, renames, retires, scopes and maps answers afterwards; a created field of
  another type takes none.
- **What a dropdown needs.** At least one answer; distinct labels and distinct stored
  values (a value left blank stores the label, and a rename freezes that, so a renamed
  answer keeps saving its old stored value, #2901); one answer per concept (two answers
  sharing a concept could not be read back); `mode='editable'` (a person chooses it);
  and `omop_table` Measurement or Observation, the tables that carry a coded answer. A
  refused answer is a per-index 400 (`answers: [{label?, value?, concept?}]`) and nothing
  is created. At most 200 answers, and a name that still has `FieldChoice` rows (left by a
  field since removed, since answers are keyed by name, not by the field) is refused rather
  than inheriting them.
- **Reads.** The definition list (`GET custom-patient-fields/`) and the writable-fields
  descriptor carry `options: [{value, code, label?}]` as a built-in select's do: `value`
  is what a save stores, `code` the code of the answer's concept (its approved decision,
  else its primary code) or null, `label` only where it differs from the value. Retired
  answers are not offered. The record read carries only the stored value under
  `custom_fields`; the descriptor entry adds `multiple: false` and the projection's
  `value_concept_map`.
- **Writes.** The PATCH value must be the stored value of a live answer, else a 400 that
  names the field and lists the answers (`custom_field_values.changed_values`); `null` or
  `""` clears; a number is accepted for a numeric stored value (`2` is `"2"`). A value the
  record already holds is not an edit and is not checked again, so a client echoing a
  retired answer already stored is not refused. It lands on `custom_fields`, is pending as
  `custom_fields.<name>`, and projects as for any created field.
- **Projection.** The recipe is the `concept` value kind of `project_single_value` with
  `value_concept_map` from `answer_concept_ids`: the row carries `value_as_string` = the
  stored value always, and `value_as_concept_id` as well where the answer has a usable
  approved concept (none, and plain text, for an answer with no concept, a
  no-equivalent decision, or a target a reload retired). A retired decision is not
  written; the value is still saved. The row is under the field's one concept and its
  source key `custom:<name>`, in Observation or Measurement.
- **Derive-back** (`custom_field_values.AnswerIndex`, used by
  `patient_record_service.custom_field_answers`). A fact at the field's concept, entered
  or imported, reads as the stored value of the answer its `value_as_string` stands for
  (label, stored value or alias, case and spacing aside), else the answer its
  `value_as_concept_id` stands for (`answer_values_by_concept`, the inverse of the
  projection from the same decisions). A text or concept that two live answers share, or
  that stands for no answer, reads as nothing and is **not written**: such a fact is
  skipped before the latest fact is chosen, so it cannot hide an older fact that does
  match. A retired answer still reads (stored data keeps its meaning); a live answer
  speaks over a retired one. A value the editor wrote (under `custom:<name>`) whose answer
  was **deleted** since still reads as the text it stored, so deleting an answer does not
  blank the record; an imported fact of a deleted answer is not read. A *proposal* whose
  answer is retired is not shown, since a proposal is approved by saving its value and only
  a live answer can be saved.
- **Provisional data.** A proposal-based row fills a dropdown through the same reader, so
  only with one of its answers; the mark, the rejection by clearing and the approval by
  PATCH are the scalar rules of [created fields](patient-record-first-writes.md#created-fields).
- **Limits.** The field has **one concept**, as every created field does, and facts are
  matched on it: an import that carries the answer as a *different* observation concept
  (one concept per answer, the common FHIR pattern) is not read into the dropdown, which
  needs a mapping to the field's one question concept with the answer as value. An answer
  concept is read back only from `value_as_concept_id` of a fact at that question concept.
  An existing dropdown's mapping (`PATCH field-mappings/<id>/`) cannot be moved to a table
  that holds no answer or given another value kind (400), and the edit dialog lists only
  Measurement and Observation for it. Changing an answer's stored value through the editor
  keeps the old value as an alias, so facts saved under it still name the answer, and a
  stored value another answer of the field answers to is refused; a saved value is also
  matched exactly before any folded spelling. Each refresh builds the answer index (about
  nine queries) once for the approved pass and again for the provisional pass when there
  are provisional rows; it is not yet shared between them.
  Dropdowns are single-choice (no multi-select) and text-valued: no integer, decimal or
  boolean answer typing, and a dropdown cannot be changed to or from another type, nor
  computed. Answer scope by disease (`FieldChoiceDisease`) is stored but, as for built-in
  fields, does not yet narrow what the patient tab offers. A condition, drug or procedure
  table cannot hold a coded answer, so a dropdown cannot be stored there.

### Removing a created field: retire, restore, delete

A created field is **global**: one `CustomPatientField`, one approved mapping, its
answers and scope, shared by every organization on the instance. Removing it removes
it for all of them, so it is an approval like creating one: `DELETE
/api/v1/custom-patient-fields/<field_name>/` and `POST .../restore/` are for staff and
org admins (`_can_approve_mappings`); an analyst, doctor or patient gets 403 on both,
before the field is looked up. v1 only. Rules in `omop_core/services/custom_field_removal.py`.

| Call | What it does |
|---|---|
| `GET .../<name>/` | the field with `records_with_value`, `has_facts`, `answers`, `can_delete`, `retired`: what a confirmation dialog shows |
| `DELETE .../<name>/` | **retire** (the default, reversible) |
| `DELETE .../<name>/?permanent=true` | **delete for good**, only when `can_delete`; else 409 `has_data` with the counts |
| `POST .../<name>/restore/` | restore a retired field; 409 `not_retired`, or `mapping_conflict` when another approved mapping took its table and source value |
| `GET /custom-patient-fields/?state=retired` | the removed fields (approvers only), with their scope |
| `DELETE field-mappings/<id>/` of a created field's mapping | 409 `created_field` naming the action above (it was a 500: `CustomPatientField.mapping` is `PROTECT`); so is a `PATCH` that un-approves or rejects it (the field would vanish with no way back) |
| `PUT field-mappings/<name>/diseases/` of a retired field | 409 `retired_field`: its scope is kept for the restore |
| any of the above on a field another request just deleted | 404 `gone` |

- **Retire** sets `CustomPatientField.retired_at` (migration `0379`) and flips the
  field's mapping to `rejected`, with `update()` so that neither an approval's OMOP
  re-projection nor any record changes. Every reader of created fields asks for
  `CustomPatientField.objects.active()` or for approved mappings, so the field leaves
  the patient tabs (`GET custom-patient-fields/`), the writable-fields descriptor, the
  Field Mappings lists and CSV (`created_field: true` marks a created field's
  descriptor), the patient-visible and attestation rules, the derive and provisional
  passes, the formulas, and the scope readers (`disease_profiles.memberships()`,
  `extension_fields()` and `field_names()` skip a retired field; `include_retired`
  reads them). An edit of it is ignored as a key with no definition always was, and
  the answer editor refuses its answers. Idempotent.
- **Nothing a record holds is touched.** `custom_fields.<name>` and the OMOP facts under
  the field's source key stay. `refresh_patient_record` rebuilds `custom_fields` from the
  active fields' facts, so it carries a retired field's stored value over unchanged
  (`retired_values`); a key no field defines, retired or not, is still dropped. The
  definition, mapping, answers (`FieldChoice` rows, aliases, codes and concept decisions),
  formula and scope rows are all kept, so a restore returns the same field.
  `user_edited_fields` pins (`custom_fields.<name>`) stay too, through derivations
  (`retired_pins`): only a permanent delete clears them.
- **The name stays taken.** Creating a field under a retired name is a 400 that points to
  Restore, so stored values are never adopted by a different definition.
- **Restore** approves the mapping again (with `update()`, so no re-projection) and clears
  `retired_at`; the field, its scope, answers and every stored value are back at once.
- **Delete for good** only when nothing depends on the field: no record holds a value
  under the key (one `has_key` query; a JSON null is no value) and no OMOP fact is
  written under the field's own source key (one existence query). Rows that only sit at
  the field's concept are the patients' own data and do not block (#2930): deleting the
  field never touches them, and a field created again over that concept reads them again.
  Otherwise it is refused and retire is the way. It then deletes, in one transaction, the definition,
  mapping, formula, scope, synonyms, answers (with aliases, codes, disease scope and
  concept decisions), and what records keep *about* the field with no value in it:
  `user_edited_fields` pins (`custom_fields.<name>`, a pending clear), proposal rejections
  and the key in the provisional picture. A retired field can be deleted later by the same
  rule, from the Removed fields list. A permanent delete **retires first** and re-checks
  after that commits: a retired field takes no new value from the record editor, so a write
  that resolved the field while it was active cannot land between the check and the delete.
  A refusal at that point leaves the field retired. Records are matched by the key as data
  (`KeyTransform`), never by building a lookup from the name, since `psa__level` or
  `x__isnull` are valid names. The scans are unindexed JSON lookups over `patient_record`
  and the two provisional tables, run when the dialog opens and once more per removal.
- **Copying between instances.** The curation payload (`dump_field_curation`,
  `copy_reference_data`) carries `retired` on each created field, with the rejected
  mapping; a payload without the key leaves the target's state alone.
- **The page.** On a created field's row, staff and org admins get a Remove field action
  (`RemoveFieldDialog.tsx`): it reads the usage, states how many records hold a value and
  says that removing keeps them and is restorable, offers Delete permanently only when
  `can_delete`, and reports what the server answered. The global Remove-mapping X is not
  shown for a created field. **Removed fields** (`RemovedFieldsPanel.tsx`, approvers only)
  lists them with Restore and Delete.
- **Not done.**
  - A retired field's `custom_fields` value is still in the raw JSON of the record read on
    `/api/v1/patient-records/` and the legacy path, to every reader of the record (nothing renders
    it; a client that echoes the object back sends a no-op). Retiring hides a field, it does not
    withdraw its data from the API; a sensitive field's values would need a data fix. The dialog says so.
  - Restore does not re-derive records. While a field was retired its value was frozen, so facts
    the ETL wrote meanwhile, and the provisional marks dropped at retire, come back at each
    record's next derivation (`manage.py backfill_patient_records --all` does every record).
  - Permission is the same as creation: an admin of *any* organization may remove a global field,
    and `GET .../<name>/` counts records instance-wide. A narrower rule (staff only, or the
    creating organization) is a product decision not made here.

### Whose record a field is part of: `FieldDiseaseMembership`

A patient's record is a **profile**: the base record every patient has, plus
the **extension** of each disease the patient has. A field's **scope** is its
`FieldDiseaseMembership` rows (#2331), one per disease whose extension it is
in, keyed by field name so a column and a `CustomPatientField` are scoped the
same way and a field needs no mapping row to have a scope. No rows means the
field is base. It is membership on the one `PatientRecord` table, never a
per-disease table: adding or removing a row moves no column and changes no
stored value.

The diseases are Disease Category management's, and there is no second list.
A membership names a `DiseaseCategoryChoice` code;
[`disease_profiles`](../omop_core/services/disease_profiles.py) is the one
writer and refuses a code that is not a category, and `Other`, whose profile
is the base record. Which category a record's disease name falls in is
`DiseaseCategoryMapping` through `disease_code`, and
`disease_relevance.patient_diseases` returns a record's categories, so a
disease name a curator maps, or a category a curator adds, gets a profile
with no deploy. A category that still has member fields cannot be deleted.

Migration 0347 seeds MM, BC, FL, MCL and CLL with the fields
`DiseaseTab.tsx` renders for each; every other field starts as base. The
migration carries its own frozen copy of the rows (`FIELD_DISEASE_SEED`), so
what a fresh install seeds does not depend on the code it is installed from.
`disease_profiles.SEED_DISEASE_FIELDS` is the same list for a database built
without migrations, and two tests hold the three together: one against the
tab, one against the union of what the migrations write. A new disease-tab
field therefore needs the constant and a data migration that declares its
own `FIELD_DISEASE_SEED`. The genomics catalog's per-disease markers are not seeded: a priority
is not a scope, and relevance still reads them from the catalog.

| Reader | What it gets |
|---|---|
| Field descriptors (`GET /api/v1/field-mappings/`) | `scope` (`base` or `disease`) and `diseases` on every field; `?disease=<code>&set=extension\|profile` narrows the list to a disease's extension or its full record |
| `GET /api/v1/field-mappings/export/?disease=<code>&set=profile\|extension` | The same set as a CSV for offline review, one row per field with its concept or blank concept columns; mapping read access, no patient value, no reviewer name ([`disease_field_sets`](../omop_core/services/disease_field_sets.py)) |
| `GET /api/v1/patient-records/writable-fields/` | `scope` and `diseases` on every entry; `?disease=` (repeatable, a code or a name) returns that profile |
| `GET /api/v1/custom-patient-fields/` | `scope` and `diseases` on each created field |
| `disease_relevance` | A disease's `disease` tier is its extension; `common` is the base clinical fields |

Writers, following mapping write access except the last, which creates an
approved mapping and so follows approval (staff and org admins, #721):

- `PUT /api/v1/field-mappings/<field_name>/diseases/` sets a field's scope by
  name (`GET` reads it). It needs no mapping row, so a field with no concept
  yet is scoped without creating an empty proposed mapping.
- `PATCH`/`POST /api/v1/field-mappings/` take `diseases` with the rest of a
  mapping save: omitted leaves the scope alone, `[]` makes the field base. A
  save that carries only a scope does not re-save the mapping, so it does not
  re-run the field's OMOP projection.
- `POST /api/v1/custom-patient-fields/` takes it for a new field, which
  starts from exactly the scope sent.

Only a field the descriptors list can be given a scope
(`disease_profiles.field_names`): a clinical column, a genomics component or
a created field, not an internal column and not `disease` itself, which
decides the profile. Any name can be made base, which is how rows left under
the name of a removed field are cleared; such rows do not hold a category in
place and are deleted with it. A scope edit is not part of the recipe, so it
never signs a mapping off. Edits to one field are serialised, and each change
is logged to `omop.ops` once it commits, with the scope before and after and
who made it, since a removed row leaves nothing behind.

Rows travel as the `memberships` table of the curation payload, and a
field's scope is **replaced**, not merged, as far as the source can vouch
for it. Adding rows alone could never carry a narrowed or cleared scope,
because both instances start from the same seed. The payload's
`membership_source` names the fields and the categories the source knows,
and a row on the target is removed only when the source knows both its field
and its disease and does not have it. So:

| On the target | After a copy |
|---|---|
| A field both instances have | Takes the source's scope, base included |
| A field created only on the target | Keeps its scope |
| A field in a category only the target has | Keeps that membership |
| A field whose source row names a category the target lacks | Row skipped with a warning; the field's scope is left exactly as it is |
| Any field, from a payload without `membership_source` (hand-written) | Rows are added, none removed |
| Any field, from a payload without `memberships` (the committed fixture) | Unchanged, with or without `--prune` |

Both instances must be on the same migrations, as the rest of the copy
already requires: a source that has not run a later seed migration reads as
"base" for the fields that migration scoped. Pruning a created field removes
its memberships with it.

The Field Mappings page is where a curator reads and edits scope (#2332):

- **Scope column.** Every row shows `Base` or the field's Disease Category
  codes.
- **Disease filter.** All / Base record / one category (never `Other`, whose
  record is the base record), each with its count, and for a category a
  **Disease-specific** / **Full record** toggle. It is applied in the browser
  to the descriptors the page already holds, by the rule the list endpoint's
  `?disease=<code>&set=extension|profile` applies (`inDiseaseView` in
  `frontend/src/utils/fieldScope.ts`), so choosing a disease is no further
  request. The tab bar and the counts follow the view. The page opens on a
  view from its own `?disease=&set=` query, read once: a code or a category's
  label in any case, `Other` for the base record, and a value that names no
  category lists every field and says so. With no `set` the page opens on
  the disease-specific fields, where the endpoint's default is the full
  record.
- **Two CSV downloads** for the chosen category, **Full record (CSV)** and
  **Disease-specific (CSV)**, from the export endpoint under the filename the
  server sends; with no category chosen, the base record alone. They follow
  mapping read access, so a doctor may download.
- **Scope control** in the Field Concept Mapping dialog
  (`FieldScopeControl.tsx`), for a caller with mapping write access: base
  record, or disease-specific with one or more categories. A save that
  changes the scope and nothing else goes through the by-name `PUT`, whether
  or not the field has a mapping row, so no mapping rule can refuse a
  membership edit and no row is re-saved. A save that also changes the
  mapping carries `diseases` with it, and one that leaves the scope alone
  sends no `diseases`. A refusal under `diseases` is shown under the
  control. `disease`, `disease_slug` and `disease_slugs` get no control,
  since they decide the profile. The dialog opens only for a mappable field,
  so a computed field's scope is read on the page and set through the API.

Location fields and legacy alias fields have nothing to map, so the page has
never listed them, and neither do the per-disease list
(`?disease=&set=`) or the CSV (`disease_field_sets.NOT_MAPPABLE`, the same
rule as the page's `isSupportedMapperField`): for any category and either
set, the page, the list and the download name the same fields. An alias a
category's rows name, as `absolute_lymphocyte_count` is for CLL, is still
that category's field for the patient tab and for relevance; it is only
left out of these lists. The unfiltered list endpoint returns every
descriptor, as before.

No patient tab and no patient save consults the rows yet: what the disease
tab renders (the rest of #2332) and what a PATCH accepts (#2333) are
unchanged.

### Lab derivation reads the mapping tables

Every reader that turns a LOINC code into a lab field at derivation time (the
lab extractor, the FLC pairing, the SLiM scan, the ANC/ALC/AMC/platelet
selection, the provenance registry and the demo-profile recovery) takes its
routing from `resolve_lab_code_fields` in the
[PatientRecord service](../omop_core/services/patient_record_service.py),
resolved once per refresh. An approved `FieldConceptMapping` with a loaded
concept names each lab field's canonical code; approved global
`SourceCodeConceptMapping` rows whose target is that concept add alias codes,
so a curator's alias selects, converts and attributes a result exactly as the
canonical code does. `_LOINC_LAB_FIELDS` in the same module is the checked-in
inventory of the lab fields and their codes: migration 0240 seeds the mapping
rows from `field_curation_v1.json`, 0285 approves the alias codes and 0327
approves what the seed left proposed. The table decides a field's codes only
while that field has no approved, concept-resolved mapping -- on a fresh
install before `load_athena_vocabularies`, a seeded row whose concept is not
yet loaded carries none -- and a test database built without migrations; `tests/conftest.py`'s
`lab_field_mappings` fixture seeds the rows for a test that must derive through
the tables.

A seeded row resolves its concept when the vocabulary arrives. On a fresh
instance the migrations run before the full vocabulary is loaded, so 0240
saves each row whose concept is not loaded yet with `concept = NULL` and only
its `(vocabulary_id, concept_code)` text (`FieldConceptMapping.concept` is a
db-constrained FK, so nothing else is possible). A bootstrapped production
instance already holds the bootstrap's LOINC subset when 0240 runs, so those
rows resolve at seed time; the SNOMED, NAACCR and similar rows, and any LOINC
code outside the subset, stay NULL until the load. `resolve_lab_code_fields` requires a resolved concept, so the
instance would stay on the extractor table, the alias join would find
nothing, and the editor and `copy_reference_data` would see every seeded row
unresolved. `load_athena_vocabularies` therefore ends every successful
non-dry-run load, `--concepts-only` included, by running
`resolve_field_concept_mappings` in the
[field concept resolution service](../omop_core/services/field_concept_resolution.py):
every mapping with no concept is pointed at the Concept its pair names, in
batches, and the load log reports how many rows resolved and which are still
unresolved (a code outside the load scope, or a HealthKey concept this
instance has not minted). The pair is the row's own text, or, for a row
whose text is blank, the pair the fixture resolved that field to (the three
SCT rows are keyed by `source_value` and carry no code). Each row is saved
as the editor would save it, so the post-save projection backfills a user
edit that was pending on the field; only the FK changes, and text, status,
provenance and `updated_at` are untouched. A row that already points at a
concept is never a candidate, concept 0 included (the "No matching concept"
placeholder the genomics seeds park a field on), so "0 still unresolved"
means every candidate's code was found, not that every field has a real
concept; a second run changes nothing. Each row is locked, re-checked (still
no concept, still the text its pair came from) and saved in an `atomic` block
of its own, in pk order. How long the lock lasts depends on the caller. In
`manage.py resolve_field_concept_mappings`, which holds no transaction, every
block commits and releases its lock before the next, so a curator's concurrent
save waits for one row and a failure keeps the rows already done. In the
loader hook (and under `sync_athena_vocabulary`, the sync's whole load) the
blocks are savepoints: the locks are held until the hook's or the sync's
transaction ends, a curator's save of a row already processed waits for the
rest of the run, and a failure discards the whole pass. The step is best effort: it runs in a savepoint and a failure is
logged with the manual fallback and does not fail the load, because under
`sync_athena_vocabulary` the whole load is one transaction and the hook runs
inside it. The migration bootstrap (`prepare_production_database`) loads at
the 0200 schema baseline, before the curation tables are seeded, and does
not run it. A scheduled sync whose archive was already applied does not
run the loader at all, and one whose archive is new but adds no rows runs it
and rolls it back with the rest, so resolve by hand after such a sync. An
instance that loaded its vocabulary before this hook existed, or seeded or
copied mappings after the load, runs `manage.py resolve_field_concept_mappings`
once (`--dry-run` reports and writes nothing; `--fail-on-unresolved` exits 1
while any mapping that names a concept is still unresolved, so `--dry-run
--fail-on-unresolved` is the check a deploy or cron can run). A failed hook
leaves only a log line, so that check is how an operator finds it. A row whose pair names a concept Athena has since retired
still resolves to it, as `apply_payload` would, and the log names it for
re-curation. `SourceCodeConceptMapping` needs no such pass: its concept FKs
are unconstrained, so the seed stores the id before the Concept exists.

Resolving the FK switches a mapping on. While `concept` is NULL an approved
row is inert: the editor does not offer the field, the curated reader keys on
`(concept_id, source_value)` and never matches, and the post-save projection
returns early. After the pass the roughly 160 approved rows take part in
editing, derivation and the pending-edit projection from the next refresh of
each record. On a fresh instance that changes nothing visible, there being no
patients. On a populated one, records derived earlier were derived without
them; `DERIVATION_VERSION` does not move, because no derivation logic changed,
so `backfill_patient_records` would skip them as current. The command and the
loader log therefore tell the operator to run `backfill_patient_records
--all` whenever a pass resolved anything.

## Scalar save, projection and refresh

Clinical and profile editors PATCH
`/api/v1/patient-records/{person_id}/` (the provider UI also uses the
`/api/patient-info/{person_id}/` alias). The
[serializer](../patient_portal/api/serializers.py) saves validated values and records
pending user edits. Direct saves recompute dependent values from PatientRecord;
they do not run a full OMOP refresh.

Supported built-in or approved curated recipes provide the descriptor's
projection. [project_single_value](../omop_core/services/omop_projection.py) matches
a non-erroneous fact by patient, concept, source key and **current local date**.
Repeated same-day saves reuse that row, including a matching imported row while
retaining its provenance. Earlier facts remain history. A date picker does not
automatically override this event date; structured writers own their dated events.

For scalar Measurement/Observation projection, numbers and booleans use
`value_as_number`; strings and dates use this application's `value_as_string`.
The generic writer clears `value_as_concept_id`. Field-level concept mapping
therefore does not establish coded-answer mapping. Units and fact type come from
the projection recipe; a unit or date companion is not itself a clinical question.

A clear nulls answer columns and records `PatientRecord:cleared` in the source
answer. Readback suppresses the cleared event and preceding facts with the same
concept/source key without deleting history. Occurrence tables cannot represent
negative/null answers this way; unsupported edits remain pending rather than
creating an affirmative diagnosis, procedure or drug administration.

Successful scalar projections acknowledge pending edits. Failed, unmapped and
unsupported projections preserve them. Full OMOP refresh reads supported curated
scalar values and restores pending user values when necessary. Mapping approval
backfills pending edits for that field through the same projection service; it
does not rewrite every historical derived value. See
[signals](../omop_core/signals.py), the
[projection service](../omop_core/services/omop_projection.py) and the
[PatientRecord service](../omop_core/services/patient_record_service.py).

## Structured and vocabulary boundaries

The [therapy architecture](therapy-reference-tables-architecture.md) owns
regimen/component/class catalogs and disease/round scope. Episode-linked intent,
outcome and discontinuation values retain their event context; a planned therapy
must not be treated as administered merely because its label maps to a drug.
[ADR 0002](adr/0002-omop-therapy-types.md) defines class matching and the
limitations of aggregate later-line projections.

The [Genomics architecture](genomics_architecture.md) owns Measurement finding
parents, linked Measurement/Observation components, source-preserving history,
state validation and the frozen catalog. It is implemented, not an unmerged
exception to the field-mapping architecture. Approved recipes govern supported
parents/components; the generic answer-mapping proposal does not replace this
structured writer.

[ADR 0001](adr/0001-vocabulary-source-of-truth.md) separates vocabulary release,
reviewed mapping revision and source catalog/recipe version. Curation uses the
installed concept/domain and preserves portable vocabulary/code plus source
identity. A proposed or even previously approved row in an inventory export is
not a fresh clinical validation of its meaning.

## Curation transfer and fixture seeding

Curation data (mappings, disease memberships, choices, formulas, synonyms, custom fields) can reach a
new instance through three paths:

1. **`copy_curation`** — live database-to-database transfer from a running source
   instance. See [CLAUDE.md](../CLAUDE.md) for usage. Requires `SOURCE_DATABASE_URL`.

2. **`dump_field_curation` / `load_field_curation`** — file-based export and import.
   `dump_field_curation` serialises the curation tables to JSON with a metadata
   envelope (`schema_version`, `exported_at`, `source`). `load_field_curation`
   reads the JSON and applies it via `apply_payload`. Both reuse the proven
   `read_payload`/`apply_payload` from
   [`field_curation_transfer.py`](../omop_core/services/field_curation_transfer.py).
   Supports `--dry-run`, `--prune` and `--tables` (same interface as `copy_curation`).
   Default tables: mappings, custom_fields, memberships, choices, formulas,
   synonyms (no code_mappings).

3. **Checked-in fixture** — [`omop_core/data/field_curation_v1.json`](../omop_core/data/field_curation_v1.json)
   contains 314 mappings, 262 choices, 12 formulas and 1 synonym exported from
   staging after the provenance backfill. Migration 0240 loads this fixture
   automatically on `migrate`, so a fresh deployment gets the full curated
   inventory without manual steps. The migration is additive: it filters out rows
   that already exist by natural key, so curator-approved mappings are never
   overwritten. It requires Athena vocabularies to be loaded first for concept
   re-resolution; on a database without vocabularies, mappings land with
   `concept = None` and a warning.

All three paths use natural-key matching, re-resolve concept FKs by
`(vocabulary_id, concept_code)`, and clear user FKs (reviewer, created_by) since
user IDs are instance-specific. Row IDs are never copied.

## Verification and historical evidence

Use the [inventory generator](../omop_core/management/commands/export_field_mapping_inventory.py)
and its documented read-only export to inspect field/value coverage. Runtime
sources above establish behavior; the [plan](field_concept_mapping_plan.md)
records what remains unresolved. These references do not certify a deployment's
vocabulary or migration state.

The former September 11 manual audit, duplicated issue tables and staging receipt
remain in [repository history](https://github.com/healthkey-ai/promop/blob/4302c975dfd0098209c2b0a53e7b06548041cd5b/field_to_concept_mapping.md).
The [initial approval artifact](patient_field_mapping_initial_approvals.json)
retains its original audit notes and filename references as historical evidence;
it is not rewritten by documentation renames or used as current approval status.
