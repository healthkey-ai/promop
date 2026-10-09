# HealthTree destination review

HealthTree resolver data can supply several destinations for one source code.
`MappingDestinationCandidate` stores each distinct source mapping / destination
vocabulary / destination code relationship, its contributing origins, and an
optional link to the loaded OMOP concept. Missing and retired destinations remain
visible as unavailable choices. The existing mapping's `target_concept` and review
status hold the curator's chosen destination; imported alternatives remain after
selection. Only approved mappings continue to affect clinical resolution.

Reload from HealthTree One with:

```sh
python manage.py import_healthtree_crossmaps --one-root /path/to/one --dry-run
python manage.py import_healthtree_crossmaps --one-root /path/to/one --skip-suggest-embeddings
```

Set `DATABASE_URL` to the intended database before running the commands. For staging,
load it from `STAGING_DATABASE_URL` in the local `.env`; do not use GCP connection
settings. Apply migrations first. `--skip-suggest-embeddings` defers the separate
embedding maintenance job during the reload.

A JSON artifact with `candidates` can also be supplied with `--artifact`. Markdown
exports containing multiple targets are rejected because they omit the alternative
codes. Regenerate the JSON artifact or use `--one-root` to retain the complete data.

New ambiguous sources are proposed with no destination selected. Reloads add missing
alternatives and resolve newly loaded concept links without overwriting existing
selections, review status, occurrence counts, or notes. The command reports source
and destination totals and unavailable targets. It does not delete historical
alternatives that disappear from a subsequent source snapshot.

Destination Count includes the distinct imported vocabulary/code pairs and any
additional selected destination. It counts unavailable imported targets too, so
missing vocabulary content cannot hide source ambiguity. A source with no choices
has count zero. Searching the list does not change a source's count.

Edit Mapping highlights multiple destinations and offers the source-data choices
with name, vocabulary, code, OMOP ID, and provenance. Unavailable choices are disabled.
Choosing an option fills its destination metadata and domain/table; saving persists
the choice through the existing mapping review flow. Imported alternatives are
fetched when opening the dialog, rather than embedded in every list row. A mapping
with imported alternatives cannot be reassigned to a different source identity;
create a new mapping for that source instead.
