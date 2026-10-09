# ADR 0002 — OMOP-native therapy types (drug-class matching)

**Status:** Accepted 2026-08-03; clarified 2026-09-14 against the implemented
PRomop model. Deployment and consumer rollout are tracked separately.
**Extends:** [ADR 0001](0001-vocabulary-source-of-truth.md).
**Supersedes:** the earlier EXACT/CancerBot decision that therapy types must
remain CB-only because patient records cannot carry class concept IDs.
**Deciders:** PRomop, EXACT/CancerBot and SoC maintainers.

Delivery status, cross-repository tickets, coverage verification and rollout
gates live in [the field/value mapping plan](../field_concept_mapping_plan.md#therapy-type-consumer-delivery).
The [therapy architecture](../therapy-reference-tables-architecture.md) owns
curated reference tables and authoring relationships.

## Context

Therapy criteria distinguish regimen identity, drug components and drug-class
“types.” PRomop can derive class concepts from a line's components and publish
them with the patient projection. That removes the original rationale for
requiring every type criterion to match through a CB category lookup.

A class concept is not a regimen identity or an administered-drug fact. Some
source categories represent broad umbrellas or non-drug modalities without a
lossless drug-class mapping. Those criteria still need their source semantics.

## Decision

1. Use reviewed HemOnc/ATC class concept IDs for criteria with a lossless mapping
   and a validated patient-side derivation. Keep unsupported or lossy categories
   on the legacy path, or return an explicit unresolved result.
2. For drug-class criteria, **required means any-overlap (OR)** and **excluded
   means any-hit**. Do not substitute regimen-component superset matching.
   Regimen identity can depend on evidence beyond a shared component set.
   Where a consumer uses regimen expansions, keep each regimen's component group
   as an OR-alternative and require a complete patient line to contain that group.
   Do not union components across lines or expand excluded regimens; a component
   match does not turn an inferred regimen identity into a source assertion.
3. PRomop pre-expands patient classes. The implemented graph walk follows
   `Component --Is a--> Component Class` transitively. Consumers read the
   published class IDs; they do not independently derive the patient's classes
   from their vocabulary mirror. A vocabulary mirror remains valid for other
   consumer work under ADR 0001.
4. Validate mapping roles and source/target semantics. Do not substitute coarse
   regimen-to-component role relationships for class concepts, or invent an
   external-vocabulary class when coverage is missing.
5. Unknown, incomplete or incompatible evidence must not erase a required
   criterion or weaken an exclusion. Preserve unmapped categories explicitly.
   Verify patient, trial and mirror release consistency before trusting overlap.
6. Roll out the lossless subset as a hybrid. Agreement with every legacy category
   lookup is not the definition of correctness: divergences require semantic
   review. A weakened exclusion is a hard stop until resolved.

## Patient projection contract and limits

PRomop exposes `first_line_therapy_type_ids`, `second_line_therapy_type_ids`,
`later_therapy_type_ids` and aggregate `therapy_type_ids`. The structured
`lines_of_therapy` serializer also exposes `type_ids`, regimen identity/source
and release provenance.

First and second lines have separate class/component sets. Later-line sets are
still unions across 3L+ and are identified by `later_aggregate` in the structured
payload. A later entry may contain a sibling line's classes. Do not interpret
those sets as evidence for a particular later line or a complete regimen.

`therapy_release_id` certifies the aggregate class set only when every contributing
line has the same nonempty release identity and a resolved regimen, and all
aggregate classes are covered by those lines. Otherwise it is null. This is an
implemented fail-closed serializer rule, not proof that every consumer enforces
the corresponding gate. Source-asserted and inferred regimen identities remain
distinguishable through `therapy_ids_provenance`.

See the [projection implementation](../../omop_core/services/patient_record_service.py)
and [serializer](../../patient_portal/api/serializers.py) for the executable
contract. Publication and historical-data limitations from ADR 0001 still apply.

## Evidence and coverage boundary

The [original feasibility output](0002-phase0-coverage.txt) demonstrated member
drugs for 18 HemOnc target classes through transitive `Is a` edges in one local
export. The two ATC targets lacked usable member derivations there, and the
export lacked the HemOnc-to-RxNorm bridge needed for RxNorm-only input. This is
historical feasibility evidence, not certification of today's deployed corpus
or every source category. A generic ancestor traversal is not a substitute for
validating the intended class-membership relationship.

## Relationship to field/value mapping and Genomics

Reuse `TherapyRegimen`, `TherapyComponent`, `TherapyClass`, their links and
disease/treatment-round scope. The common mapping interface exposes that catalog
through a provider adapter. Its reviewed class mappings and graph-derived patient
classes have different responsibilities; the adapter must reconcile them without
claiming that merely curating a catalog link changes patient derivation.

Coded intent, outcome and discontinuation values belong to their episode and
clinical date. They are separate answer-mapping work, not therapy “type” IDs.
Genomic finding status, origin, interpretation and variant identity likewise
remain under the [Genomics architecture](../genomics_architecture.md); this
therapy-specific overlap decision supplies no generic genomic matching rule.
