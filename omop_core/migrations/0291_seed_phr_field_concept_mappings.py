"""Seed proposed FieldConceptMapping rows for PHR record design fields (#1839).

The PHR record design (ht-phr-one docs/design-verification.md, rows D03–D22)
shows details that PRomop has no place to store. This migration creates proposed
FieldConceptMapping rows so the system knows which OMOP concept each field maps
to. A curator must review and approve each before it becomes writable.

Per-event attributes use dotted field names (e.g. ``condition.note``,
``drug_exposure.role``) following the ``genetic_mutations.*`` pattern established
by the genomics catalog.

All mappings are ``status='proposed'`` with blank ``omop_table``, ``source_value``
and ``value_kind`` — the curator fills these in during approval. If the referenced
concept is not yet loaded (Athena vocabulary not imported), the mapping is
skipped with a warning and can be created by a later migration or manual seed.
"""
import logging

from django.db import migrations

logger = logging.getLogger(__name__)

# (field_name, vocabulary_id, concept_code, issue_ref, note)
_PHR_MAPPINGS = [
    # D03: Per-diagnosis clinical note
    (
        'condition.note',
        'LOINC', '34109-9',
        'D03',
        'Per-diagnosis clinical note (D03). A short note attached to a specific '
        'diagnosis, as shown on each diagnosis card in the PHR record design. '
        'The design shows e.g. a treatment summary or clinical context per condition.',
    ),
    # D03: Per-diagnosis care team
    (
        'condition.care_team',
        'SNOMED', '133932002',
        'D03',
        'Care team per diagnosis (D03). The treating team members '
        '(e.g. "Dr. Amara Chen, oncology") attached to a specific diagnosis card. '
        'SNOMED 133932002 "Caregiver" is a starting-point concept; the curator '
        'may prefer a more specific concept for care team composition.',
    ),
    # D05: Blood cancer spread (bone lesion locations for hematologic malignancies)
    (
        'hematologic_spread',
        'SNOMED', '128462008',
        'D05',
        'Bone lesion locations for blood cancers (D05). The existing bone_lesions '
        'field covers solid-tumour metastasis flags; this field captures hematologic '
        'spread such as myeloma bone involvement (e.g. "spine L2, L4 and right hip"). '
        'SNOMED 128462008 "Metastatic malignant neoplasm to bone" is a candidate; '
        'curator should verify suitability for hematologic spread specifically.',
    ),
    # D08: Medicine role within a therapy line
    (
        'drug_exposure.role',
        'SNOMED', '277132007',
        'D08',
        'Per-medication therapeutic role within a therapy line (D08). Labels each '
        'drug as "Induction", "Conditioning", "Neoadjuvant", "Maintenance", etc. '
        'Distinct from the existing therapy_intent field, which is per-patient.',
    ),
    # D18: Kind of medication
    (
        'drug_exposure.category',
        'SNOMED', '410942007',
        'D18',
        'Medication category (D18). Classifies each medication as "Myeloma treatment", '
        '"Supportive", "Supplement", etc. SNOMED 410942007 "Drug or medicament" is a '
        'broad starting concept; the curator may prefer a more specific one or define '
        'answer choices via FieldChoice rows.',
    ),
    # D20: Kind of procedure
    (
        'procedure.category',
        'SNOMED', '71388002',
        'D20',
        'Procedure category (D20). Classifies each procedure as "Surgical procedure", '
        '"Biopsy", "Diagnostic", etc. SNOMED 71388002 "Procedure" is the root concept; '
        'the curator should verify or narrow it.',
    ),
    # D22: Pathologist notes per genetic test
    (
        'genetic_test.pathologist_notes',
        'LOINC', '22637-3',
        'D22',
        'Pathologist interpretation notes per genetic test (D22). Free-text remarks '
        'from the reporting pathologist. The genomics catalog already covers specimen '
        'type (genetic_mutations.specimen_type) and method '
        '(genetic_mutations.variant_analysis_method_type); this adds the missing '
        'narrative notes field.',
    ),
]


def seed_phr_field_mappings(apps, schema_editor):
    alias = schema_editor.connection.alias
    Concept = apps.get_model('omop_core', 'Concept')
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')

    for field_name, vocabulary_id, concept_code, issue_ref, note in _PHR_MAPPINGS:
        concept = Concept.objects.using(alias).filter(
            vocabulary_id=vocabulary_id, concept_code=concept_code,
        ).first()

        if concept is None:
            logger.warning(
                'Concept %s:%s is missing; skipping %s mapping (%s). '
                'Load Athena vocabularies and re-run or seed manually.',
                vocabulary_id, concept_code, field_name, issue_ref,
            )
            continue

        FieldConceptMapping.objects.using(alias).update_or_create(
            field_name=field_name,
            defaults=dict(
                concept=concept,
                vocabulary_id=vocabulary_id,
                concept_code=concept_code,
                status='proposed',
                notes=f'PHR record design #{issue_ref} (#1839). {note}',
            ),
        )


def unseed_phr_field_mappings(apps, schema_editor):
    """Remove only the mapping rows this migration created."""
    alias = schema_editor.connection.alias
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    FieldConceptMapping.objects.using(alias).filter(
        field_name__in=[f for f, *_ in _PHR_MAPPINGS],
    ).delete()


class Migration(migrations.Migration):

    dependencies = [('omop_core', '0290_personmergecandidate')]

    operations = [
        migrations.RunPython(seed_phr_field_mappings, unseed_phr_field_mappings),
    ]
