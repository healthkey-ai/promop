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

## LOINC

PRomop uses LOINC content in its clinical vocabulary and imports the LOINC
distribution's `LoincClass.csv` and `Loinc.csv` into the `LoincClass` and
`LoincCodeClass` tables. LOINC concepts may also be loaded from an Athena
vocabulary export. The following notice is required by Section 10 of the
[LOINC license](https://loinc.org/kb/license):

> This material contains content from LOINC (http://loinc.org/). LOINC is
> copyright © Regenstrief Institute, Inc. and the Logical Observation
> Identifiers Names and Codes (LOINC) Committee and is available at no cost
> under the license at http://loinc.org/license. LOINC® is a registered United
> States trademark of Regenstrief Institute, Inc.

LOINC content is licensed separately from PRomop. Use and redistribution of
that content remain subject to the LOINC license, including any notices for
third-party content identified in the LOINC distribution.
