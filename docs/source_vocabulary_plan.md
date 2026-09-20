# Source vocabulary expansion plan

Issue: [#1470](https://github.com/healthkey-ai/promop/issues/1470).

The first delivery imports NCIt source content directly from NCI, exposing its
metadata for source mapping. See the [architecture](source_vocabulary_architecture.md).

## Next candidates

The first importer is NCIt. These additional sources have been identified, but
are not imported by this command:

- [MeSH XML, including Supplementary Concept Records](https://www.nlm.nih.gov/databases/download/mesh.html):
  richer substance descriptions, synonyms, registry identifiers and pharmacological
  actions than the existing MRCONSO-only import retains.
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
