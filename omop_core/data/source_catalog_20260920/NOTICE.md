# Publisher source terminology snapshots

These immutable migration artifacts contain normalized publisher terminology,
not patient records. `manifest.json` identifies the releases, upstream URLs,
upstream SHA-256 digests, normalized payload digests, and record counts.

**NCI Thesaurus:** produced by Enterprise Vocabulary Services, National Cancer
Institute, and distributed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
Source: [NCI Thesaurus 26.08e flat release](https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/Thesaurus_26.08e.FLAT.zip).
[Publisher terms](https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/ThesaurusTermsofUse.htm).
The JSON representation is a HealthKey transformation of publisher content,
not an NCI publication. It separates preferred names from synonyms and derives
a retired flag from the supplied concept status; it does not alter definitions.

**MeSH:** Courtesy of the U.S. National Library of Medicine.
[NLM terms](https://www.nlm.nih.gov/databases/download/terms_and_conditions.html).
This is a pinned snapshot of the 2026 descriptor and supplementary concept files
retrieved on 20 September 2026. It may not reflect the latest or most accurate
data available from NLM. NLM has not endorsed this application.
Normalization retains preferred names, entry terms, preferred scope notes/SCR
notes, registry identifiers, pharmacological actions, mapped headings and tree
numbers. Descriptor parent identifiers are derived from the publisher tree
numbers. Mapped SCR headings remain explicitly separate from parents.

Reproduction command (run from the repository root with the original archives):

```sh
python scripts/build_source_vocabulary_snapshots.py \
  --ncit /path/to/Thesaurus_26.08e.FLAT.zip \
  --mesh-descriptors /path/to/desc2026.gz \
  --mesh-supplementary /path/to/supp2026.gz \
  --output /tmp/source_catalog_20260920
```

Check the input digests against the manifest before comparing generated output.
The publisher's MeSH URLs are mutable; matching the year alone is insufficient.
Do not overwrite shipped payloads. Future releases need a new directory and a
new data migration, preserving the ability to replay historical migrations.
