"""Which controlled lookup a field's options come from (#1225).

Nineteen fields on the patient tabs offer the entries of a ``VocabularyLookup``
table, and until now only the frontend knew which: ``useVocabulary('her2-status')``
beside the field. This is the server's record of it, so the choice editor can
show a curator what a field offers without a second, editable copy of it. It
only reads: the lookup tables stay managed where they are loaded.

A field is bound in one of two ways:

* **enforced**: its approved ``FieldConceptMapping`` names the lookup in
  ``value_vocabulary``. Ingest keeps only those titles and the write descriptor
  offers only them, so answers curated for the field are not offered.
* **offered**: the tab draws its dropdown from the lookup. A field with an
  approved concept mapping and curated answers offers those instead, since the
  write descriptor's options win over the tab's own.

Keep ``LOOKUP_FIELDS`` in step with the tabs: ``tests/test_option_sources.py``
fails when a ``useVocabulary`` call names a lookup no field here uses.
"""

#: PatientRecord field -> the lookup slug served by ``/api/v1/vocabularies/<slug>/``.
LOOKUP_FIELDS: dict[str, str] = {
    'estrogen_receptor_status': 'estrogen-receptor-status',
    'progesterone_receptor_status': 'progesterone-receptor-status',
    'her2_status': 'her2-status',
    'hr_status': 'hr-status',
    'hrd_status': 'hrd-status',
    'tumor_stage': 'tumor-stage',
    'nodes_stage': 'nodes-stage',
    'distant_metastasis_stage': 'distant-metastasis-stage',
    'staging_modalities': 'staging-modality',
    'histologic_type': 'histologic-type',
    'tumor_grade': 'follicular-lymphoma-grade',
    'post_transformation_outcome': 'post-transformation-outcome',
    'progression': 'disease-progression',
    'stem_cell_transplant_history': 'stem-cell-transplant',
    'sct_eligibility': 'sct-eligibility',
    'myeloma_type': 'myeloma-type',
    'ecog_performance_status': 'ecog-status',
    'karnofsky_performance_score': 'karnofsky-score',
    'ethnicity': 'ethnicity',
}
