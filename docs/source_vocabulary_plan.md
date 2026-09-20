# Source vocabulary expansion plan

Issue: [#1470](https://github.com/healthkey-ai/promop/issues/1470).

The current delivery packages full NCIt and MeSH publisher content in immutable
data migrations and exposes their metadata for source mapping. See the
[architecture](source_vocabulary_architecture.md). Release counts and provenance
are recorded in the [snapshot manifest](../omop_core/data/source_catalog_20260920/manifest.json).

## Scope and evidence

NCIt was already partially present: the inspected Render staging database had
2,426 Athena NCIt rows and 102,215 distinct NCI codes in its UMLS import. The
linked August 2026 Athena ZIP contained no NCIt vocabulary; its CDISC subset
overlapped 14,430 NCIt identifiers. The full publisher snapshot adds coverage
and definitions, not a previously absent vocabulary. Additional identifiers
are not necessarily clinically distinct concepts or new OMOP destinations.

MeSH was chosen for its substance descriptions and aliases. Exact name/synonym
matching against the observed uncoded Drug inventory found 27 source names,
accounting for 185,778 Seen occurrences. Those are overlapping patient-derived
occurrence counts, not distinct patients or destination counts. Their existing
RxNorm/UMLS matches remain useful; the addition supplies richer metadata.

No automatic approval, source recoding or rewriting of curator descriptions is
part of this load. Short regimen abbreviations remain ambiguous and need
contextual mapping review. Merely adding a dictionary does not validate an
existing proposed destination.

## Next candidates

These further sources are deferred until their additional content addresses a
demonstrated gap:

- [SEER staging data](https://api.seer.cancer.gov/docs): schema-specific code
  meanings, definitions and registrar notes. Schema and edition must accompany
  a staging value; identical short values are not globally interchangeable.
- [HemOncKB](https://hemonc.org/wiki/Ontology): compare the publisher's relationship
  and ancillary content with the existing Athena export. The publisher distinguishes
  a CC BY subset from its fuller noncommercial/commercial-licence offering.

The current UMLS loaders ingest MRCONSO code/name atoms, not MRDEF definitions or
MRSTY semantic types. Presence in those tables alone is not evidence that the
publisher's full code set or descriptive metadata is available to curators.

Each new importer must retain publisher identifiers and release provenance,
show source metadata in the existing lookup, and preserve mapping decisions
and HealthTree Seen counts. Compare publisher content with the loaded Athena
export before adding another copy of the same reference data.

## Delivery checks

- Apply all migrations on a fresh database with the packaged payloads; verify
  manifest counts, representative definitions, search and preservation of mappings.
- Fetch LFS payloads before the HealthTree image build, then use the instance's
  existing migration job. No separate vocabulary-loading command is required.
- Verify migrations 0250/0251 and NCIt/MeSH lookup on HealthTree after its
  deployment. Local or Render verification alone does not establish GCP delivery.
- Keep missing destination-search trigram indexes (#1474) separate from source
  catalog loading and embedding-index retry repair (#1476).
