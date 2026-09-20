# Publisher source vocabulary content

Code mapping can use publisher terminology that has no complete Athena vocabulary.
The source catalog stores these codes and their descriptive metadata separately
from OMOP destination concepts and from the patient-derived mapping queue.

## NCIt

`load_ncit_source` imports NCI's nine-column flat release into `source_vocabulary`
and `source_vocabulary_term`. It retains the preferred name, synonyms, definition,
parent codes, semantic types, publisher status, IRI, display name and subsets.
The release records its version, download URL, archive SHA-256 and load time.

The importer validates the entire archive before writing. Replacement of the
catalog and its release record is atomic. Re-running a release is safe; a failed
reload preserves the previous catalog. No source mapping, Seen count, destination,
curator note or approval is created or changed by a catalog load.

```bash
python manage.py load_ncit_source \
  --archive /path/to/Thesaurus_26.08e.FLAT.zip \
  --release-version 26.08e \
  --source-url https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/Thesaurus_26.08e.FLAT.zip \
  --dry-run
```

Remove `--dry-run` to load the catalog. Apply database migrations first. NCI's
[download directory](https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/) and
[format documentation](https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/ReadMe.txt)
identify versioned archives. Retired and obsolete concepts are retained for
exact lookup, labelled in the dialog, and excluded from default source search.

## Mapping workflow

In New Mapping or Edit Mapping, choosing NCIt as the source vocabulary makes
publisher code/name/synonym search available in the Source block. Selecting a
result fills the source code and description; it does not pick or approve a
destination. Entering an exact code displays its definition, synonyms, semantic
types, parent codes, retirement and release information.

`GET /api/v1/code-mappings/source-catalog/` requires the same mapping permissions
as the existing Code Mapping endpoints. `vocabulary_id` identifies the catalog,
`code` performs exact lookup, and `q` searches codes, names and synonyms. Searches
return at most 25 results. `include_retired=1` includes historical entries.
Search uses an indexed uppercase text expression and ranks exact code/name hits
first. Catalog terms are never offered as OMOP destinations.

NCIt is available for all five source domains. Its existing UMLS crosswalk uses
root source `NCI` when the loaded UMLS release contains the source code. Newly
published NCIt codes can still supply a source description without a UMLS CUI.
Source descriptions preserve existing Athena precedence and curator-entered text.

Additional publisher candidates are recorded in the [source vocabulary plan](source_vocabulary_plan.md).
