"""Concept suggestions chosen for meaning, not spelling.

``suggest_field_concept_mappings`` retrieves candidates by trigram similarity,
because narrowing several million concepts needs an index. It cannot pick between
them: similarity compares spelling, and the failure that matters is not a low
score but a high one on the wrong concept. ``clonal_plasma_cells`` matches
"Polyclonal plasma cells" at 0.75 while meaning its opposite.

So the retrieval is lexical and the choice recorded here is not. Three kinds of
thing this catches that the top lexical match does not:

* **The right answer ranked third.** ``hiv_status`` retrieves "HIV 1 IgG" first —
  a specific serology assay — while "HIV status" sits below it. Likewise
  ``hepatitis_b_status``, whose top match is "FH: Hepatitis", a family history
  note about somebody else.
* **The right answer not retrieved at all.** ``temperature`` should be LOINC
  8310-5 "Body temperature". Lexical search returns bare "Temperature" concepts,
  which score higher against a one-word field name and mean less.
* **The wrong domain.** Several of these fields are conditions rather than
  observations — see the notes at the bottom — and no amount of name matching
  makes an observation write correct for them.

Everything here is still seeded as **proposed**. A suggestion chosen for meaning
by something that has not examined the patient is still a suggestion; a clinician
approves it. What this buys is a reviewer reading plausible candidates instead of
wrong ones.
"""

# field -> the concept it means, why, and where the fact lives.
#
# `omop_table` and `value_kind` shape the write. `source_value` is deliberately
# absent: derivation matches on it, and guessing it is how a mapping becomes a
# write into a void. The reviewer sets it against whatever the extractor reads.
REVIEWED_SUGGESTIONS = {
    'white_blood_cell_count': {
        'vocabulary_id': 'SNOMED', 'concept_code': '767002',
        'omop_table': 'measurement', 'value_kind': 'number',
        'rationale': (
            '"White blood cell count" is the same quantity under the same name; '
            'the narrower "in blood" and "in CSF" variants name a specimen this '
            'field does not carry.'
        ),
    },
    'temperature': {
        'vocabulary_id': 'LOINC', 'concept_code': '8310-5',
        'omop_table': 'measurement', 'value_kind': 'number',
        'rationale': (
            'Body temperature, which is what a vital sign called "temperature" '
            'means. Lexical search never offered this: bare "Temperature" '
            'concepts score higher against a one-word field name while meaning '
            'less, and one of them is about specimen storage.'
        ),
    },
    'diagnosis_date': {
        'vocabulary_id': 'LOINC', 'concept_code': '63931-0',
        'omop_table': 'observation', 'value_kind': 'date',
        'rationale': '"Date of diagnosis" — the same fact, phrased the other way round.',
    },
    'death_date': {
        'vocabulary_id': 'LOINC', 'concept_code': '81954-0',
        'omop_table': 'observation', 'value_kind': 'date',
        'rationale': (
            '"Date of death [Date]" rather than the plain "Date of death", '
            'because this field carries a date and that variant says so.'
        ),
    },
    'condition_clinical_status': {
        'vocabulary_id': 'LOINC', 'concept_code': '99493-9',
        'omop_table': 'observation', 'value_kind': 'string',
        'rationale': 'Exact name match, and the only candidate that is the whole phrase.',
    },
    'hepatitis_b_status': {
        'vocabulary_id': 'SNOMED', 'concept_code': '278969009',
        'omop_table': 'observation', 'value_kind': 'string',
        'rationale': (
            '"Hepatitis B status". The top lexical match was "FH: Hepatitis" — a '
            'family history note about a relative, not this patient — and the '
            'second was the virus itself rather than the patient\'s status.'
        ),
    },
    'hepatitis_c_status': {
        'vocabulary_id': 'SNOMED', 'concept_code': '278973007',
        'omop_table': 'observation', 'value_kind': 'string',
        'rationale': '"Hepatitis C status", the counterpart of the hepatitis B choice above.',
    },
    'hiv_status': {
        'vocabulary_id': 'SNOMED', 'concept_code': '278977008',
        'omop_table': 'observation', 'value_kind': 'string',
        'rationale': (
            '"HIV status". Lexical search ranked "HIV 1 IgG" and "HIV 2 IgG" '
            'first: those are specific serology assays, not the status this '
            'field records.'
        ),
    },
    'peripheral_neuropathy_grade': {
        'vocabulary_id': 'LOINC', 'concept_code': '75691-6',
        'omop_table': 'observation', 'value_kind': 'number',
        'rationale': (
            '"Peripheral sensory neuropathy grade NCICTC" — the CTCAE grading '
            'this field holds, which is what "grade" means in an oncology record. '
            'Generic "Grade" concepts carry no scale.'
        ),
    },
    'clonal_bone_marrow_b_lymphocytes': {
        'vocabulary_id': 'LOINC', 'concept_code': '42759-1',
        'omop_table': 'measurement', 'value_kind': 'number',
        'rationale': (
            '"B lymphocytes [#/volume] in Bone marrow" matches both the analyte '
            'and the specimen. It does not carry "clonal", so a reviewer should '
            'confirm the distinction does not matter here.'
        ),
    },
}


# Deliberately not suggested, and why. Recorded so the next person does not
# rediscover each one.
#
#   lymphadenopathy, splenomegaly, hepatomegaly
#       SNOMED has exact matches (30746006, 16294009) but they are Condition
#       domain. A condition is not an observation, and the editor has no
#       condition_occurrence write path — mapping them to an observation would
#       store a fact in the wrong table to make a box typeable.
#
#   no_tobacco_use_status, tobacco_use_details
#       LOINC 72166-2 "Tobacco smoking status" is right, and derivation already
#       reads it — but it takes the answer from value_as_concept, while the
#       editor writes value_as_string. A mapping alone would write the wrong
#       column of the right concept.
#
#   binet_stage, flipi_score, oncotype_dx_score, mrd_status, toxicity_grade,
#   gelf_criteria_status, bcl2_inhibitor_refractory, btk_inhibitor_refractory
#       Named clinical instruments and drug-class refractoriness. Retrieval
#       returned generic "Stage", "Score", "Grade" concepts, or nonsense —
#       "Rectoanal inhibitory reflex" for a BCL2 inhibitor. These need someone
#       who knows the instrument, not a better string match.
#
#   disease, disease_slug, disease_activity
#       "Disease" as a bare concept says nothing, and "DAS - Disease Activity
#       Score" is a rheumatology instrument that does not apply here.
#
#   pregnancy_test_date
#       Derived as the event date of the pregnancy result fact, never written on
#       its own. A mapping would invite writing it independently of the result it
#       is supposed to date.
