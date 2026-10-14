"""Seed approved FieldConceptMapping rows for PHR record design fields (#1839).

The PHR record design (ht-phr-one docs/design-verification.md, rows D03–D22)
shows details that PRomop has no place to store. This migration creates approved
FieldConceptMapping rows so the write descriptor can persist edits immediately.

Per-event attributes use dotted field names (e.g. ``condition.note``,
``drug_exposure.role``) following the ``genetic_mutations.*`` pattern established
by the genomics catalog. All are stored as observations with type_concept_id
32817 (EHR).

If the referenced concept is not yet loaded (Athena vocabulary not imported),
the mapping is skipped with a warning and can be created by a later migration
or manual seed.
"""
import logging

from django.db import migrations

logger = logging.getLogger(__name__)

# Type concept for a curated, human-entered clinical assertion.
_EHR_TYPE_CONCEPT_ID = 32817

# (field_name, vocabulary_id, concept_code, source_value, value_kind, issue_ref, note)
_PHR_MAPPINGS = [
    # D03: Per-diagnosis clinical note
    (
        'condition.note',
        'LOINC', '34109-9',
        'phr:condition-note', 'string',
        'D03',
        'Per-diagnosis clinical note (D03). A short note attached to a specific '
        'diagnosis, as shown on each diagnosis card in the PHR record design.',
    ),
    # D03: Per-diagnosis care team
    (
        'condition.care_team',
        'SNOMED', '133932002',
        'phr:condition-care-team', 'string',
        'D03',
        'Care team per diagnosis (D03). The treating team members '
        '(e.g. "Dr. Amara Chen, oncology") attached to a specific diagnosis card.',
    ),
    # D05: Blood cancer spread (bone lesion locations for hematologic malignancies)
    (
        'hematologic_spread',
        'SNOMED', '128462008',
        'phr:hematologic-spread', 'string',
        'D05',
        'Bone lesion locations for blood cancers (D05). The existing bone_lesions '
        'field covers solid-tumour metastasis flags; this captures hematologic '
        'spread such as myeloma bone involvement (e.g. "spine L2, L4 and right hip").',
    ),
    # D08: Medicine role within a therapy line
    (
        'drug_exposure.role',
        'SNOMED', '277132007',
        'phr:drug-exposure-role', 'string',
        'D08',
        'Per-medication therapeutic role within a therapy line (D08). Labels each '
        'drug as "Induction", "Conditioning", "Neoadjuvant", "Maintenance", etc. '
        'Distinct from the existing therapy_intent field, which is per-patient.',
    ),
    # D18: Kind of medication
    (
        'drug_exposure.category',
        'SNOMED', '410942007',
        'phr:drug-exposure-category', 'string',
        'D18',
        'Medication category (D18). Classifies each medication as '
        '"Myeloma treatment", "Supportive", "Supplement", etc.',
    ),
    # D20: Kind of procedure
    (
        'procedure.category',
        'SNOMED', '71388002',
        'phr:procedure-category', 'string',
        'D20',
        'Procedure category (D20). Classifies each procedure as '
        '"Surgical procedure", "Biopsy", "Diagnostic", etc.',
    ),
    # D22: Pathologist notes per genetic test
    (
        'genetic_test.pathologist_notes',
        'LOINC', '22637-3',
        'phr:genetic-test-pathologist-notes', 'string',
        'D22',
        'Pathologist interpretation notes per genetic test (D22). Free-text '
        'remarks from the reporting pathologist.',
    ),
]


def seed_phr_field_mappings(apps, schema_editor):
    alias = schema_editor.connection.alias
    Concept = apps.get_model('omop_core', 'Concept')
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')

    for field_name, vocabulary_id, concept_code, source_value, value_kind, issue_ref, note in _PHR_MAPPINGS:
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
                omop_table='observation',
                source_value=source_value,
                value_kind=value_kind,
                type_concept_id=_EHR_TYPE_CONCEPT_ID,
                status='approved',
                provenance='system_generated',
                notes=f'PHR record design {issue_ref} (#1839). {note}',
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
