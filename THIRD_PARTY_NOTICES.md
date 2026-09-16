# Third-party acknowledgments

## Lettuce

PROMOP's optional **Semantic retrieval** strategy adapts the filtered pgvector
cosine-neighbour retrieval approach from [Lettuce](https://github.com/Health-Informatics-UoN/lettuce),
developed by University of Nottingham Health Informatics.

Reference revision: `7e8796ace2cbd86490bb077b3300da003c334e50`, specifically
`lettuce/omop/omop_queries.py` (`query_vector`) and
`lettuce/components/embeddings.py`, `lettuce/components/prompt_templates.py`,
and `lettuce/routers/search_routes.py`. The retrieval adaptation is implemented in
`omop_core/mapping/suggestions.py` (`semantic_candidates`) using Django and
PROMOP's existing embeddings. `omop_core/mapping/search_expansion.py` also draws
on Lettuce's formal-name generation followed by vocabulary search. PROMOP bounds
this to one retry and requires final selection against original source evidence.
It does not require a Lettuce server or accept a generated name solely because
it exactly matches a vocabulary name.

Copyright (c) 2024 University of Nottingham Health Informatics.

Lettuce is licensed under the MIT License. Its complete copyright, permission,
and warranty notice is reproduced in [licenses/lettuce-MIT.txt](licenses/lettuce-MIT.txt).
Retain that notice when redistributing this adaptation. PROMOP's own license
remains in [LICENSE](LICENSE).
