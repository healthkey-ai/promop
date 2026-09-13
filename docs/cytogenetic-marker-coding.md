# Legacy cytogenetic marker summary coding

Interactive summary editing was retired by #1242. New discrete entry belongs to
Genomics; original legacy evidence remains available through its read-only
history view. See the [implemented architecture](genomics_architecture.md).

The per-selection standard Observation mappings added by #1208 are documented in
[cytogenetic-markers.md](cytogenetic-markers.md). The description below covers the
legacy aggregate recipe, which remains readable for existing data and curator
recipes.

`cytogenetic_markers` retains canonical selection tokens for PatientRecord and
matching. It is a local multi-marker summary, not a genetic variant status.
Its OMOP projection uses Observation concept **0** (no matching concept) and
the stable source value `mm-cytogenetic-markers`. No standard concept is claimed.
Choices without a verified equivalent remain uncoded; curator-provided mappings
are retained unless they match the specific erroneous seed associations.

The earlier seed incorrectly associated marker choices with LOINC test and
metadata identifiers. For example, [81249-5](https://loinc.org/81249-5) is a
genomic reference sequence coding system, not 1q gain/amplification.
[69548-6](https://loinc.org/69548-6) is genetic variant assessment with
present/absent/no-call/indeterminate answers, not a comma-separated marker list.
Neither changing an OMOP domain nor loading more vocabulary fixes that mismatch.

Issue #1204 reconciles the separate `genetic_mutations.*` component domains,
including the legitimate `genetic_mutations.status` use of 69548-6. This summary
does not reuse or modify those mappings. The repair of prior development data
is restricted to the summary field/choice associations and Observation rows
with source `mm-cytogenetic-markers` and the previously incorrect 69548-6 code.

The merge migrations join the cytogenetic rename branch to current dev, while
also repairing development databases that already applied the earlier seed.
Migration 0228 incorporates the landed #1204 genomics changes and creates the
NOTE ID sequence used by both storage paths.
Pending-edit field names are migrated too, preserving protection for patient
edits that have not yet been projected into OMOP.
Migration 0229 joins the subsequent genomics/provenance merge from dev.

Selections longer than the CDM's 60-character value limit are stored losslessly
in NOTE. The summary fact holds a `[note:id]` reference. Readback requires the
same patient, fact table, and fact ID; same-day edits reuse the NOTE, while
earlier dated facts retain their own text. NOTE changes and the summary fact
are saved in one transaction.
