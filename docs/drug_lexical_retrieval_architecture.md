# Drug lexical retrieval

Drug retrieval already narrows concept names with ingredient word similarity
before scoring the full source description. That earlier optimization can drop
candidates relative to an unrestricted full-description search. It falls back
to that wider search only when ingredient narrowing returns no names.

The top-N optimization preserves the output of **that existing implementation**.
It does not claim to restore candidates that ingredient narrowing excluded.

## A selective probe with an exact fallback

For multiword Drug searches, name and synonym retrieval first try a transaction-local
`pg_trgm.similarity_threshold` of 0.6. They retain the original filters, scoring,
descending score order, and concept-ID tiebreaker. Synonyms are grouped by concept
before applying the limit, as before.

If the probe returns the requested N concepts, every excluded match has a lower
score than those N. The shortlist is therefore identical to the original query's
shortlist. The threshold boundary is inclusive, so ties at 0.6 still use the same
concept-ID ordering. If fewer than N concepts qualify, even if some qualify, the
original query runs with the original threshold. Name/synonym merging, the synonym
bonus, eligibility checks, and the final limit are unchanged.

Single-word searches and other domains use the existing queries. A session already
using a similarity threshold of 0.6 or higher also keeps its original queries.
The probe restores the previous threshold on success; a database exception rolls
back the transaction/savepoint, including its threshold change. Subsequent
retrieval and evidence enrichment must never inherit the probe threshold.

This trades an extra query on sparse multiword matches for less scoring and
sorting when a full high-similarity shortlist exists. It does not guarantee that
every search is faster. No new index, schema change, or vocabulary change is needed.

## Result-preservation checks

[Differential tests](../tests/test_drug_lexical_top_matches.py) compare complete
candidate dictionaries with a [frozen implementation](../tests/lexical_reference.py)
from commit `8798ec24`. Coverage includes limits 1/3/10/100, direct and derived
synonyms, ties, duplicate synonyms, stale terms, eligibility filters, misspellings,
combination drugs, empty results, other domains, and non-default session thresholds.
Tests also check threshold restoration after a database failure and exact input
to the ranker. Replaying a fixed provider response checks that the selected concept
and ranking output are preserved; live model sampling is not an equality oracle.

## Comparing a vocabulary snapshot

Use a local vocabulary snapshot for performance comparisons:

```bash
DATABASE_URL="postgresql://postgres@localhost:5433/promop_lexical_benchmark" DEBUG=True \
  .venv/bin/python manage.py compare_drug_lexical_retrieval \
  --text 'ASPIRIN 81 MG ORAL TABLET' \
  --text 'METFORMIN HYDROCHLORIDE 500 MG ORAL TABLET' \
  --text 'ASPIRIN' --limit 10 --repeat 5 --json
```

Without `--text`, the command samples distinct Drug descriptions from the mapping
queue, bounded by `--count`. Both paths run in one read-only, repeatable-read
transaction. It warms both paths, alternates execution order, and reports median
latency plus the full before/after candidate records. Any result difference on
any repetition causes a nonzero exit, including score or ordering differences.
No ranking API calls or mapping writes occur.

The disabled optimization mode uses the pre-probe queries, including existing
ingredient narrowing. The older `benchmark_lexical_retrieval` command instead
compares ingredient narrowing against the unrestricted search; it answers a
different question.

Synthetic timing results are useful for checking the mechanism, but deployment
speedups require a representative vocabulary and source-text distribution. Include
sparse and no-match searches in that comparison, not only exact drug products.
