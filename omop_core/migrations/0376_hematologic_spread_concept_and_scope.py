"""Give Hematologic Spread an OMOP concept and the blood cancers' extension.

Migration 0293 blanked the field's concept (SNOMED 128462008 is a Condition, and
the field is stored as an Observation) and registered it as a created field on
the disease tab, scoped to nobody, so it was part of every record's base: a
prostate patient's disease tab offered "bone lesion locations for blood
cancers", with an empty "Source Concept" in Edit Mapping.

Two repairs, each only where nobody has decided otherwise:

* a mapping with no concept is pointed at LOINC 33733-7, "Sites of distant
  metastasis" (an Observation, standard), which is what a free-text list of
  where the disease has spread is. The vocabulary id and code are written even
  when the concept is not loaded yet, so ``resolve_field_concept_mappings``
  finds it once the vocabulary is;
* a field with no disease scope is made part of the blood cancers' extension.

Historical models and this file's own constants only (#2440).
"""
import logging

from django.db import migrations

logger = logging.getLogger(__name__)

FIELD = 'hematologic_spread'
CONCEPT_VOCABULARY = 'LOINC'
CONCEPT_CODE = '33733-7'
# The Disease Categories of blood cancers. A category this instance lacks is skipped.
HEMATOLOGIC_SCOPE = ('CLL', 'DLBCL', 'FL', 'MCL', 'MM')
PROVENANCE = 'hematologic-spread-scope v1'


def repair(apps, schema_editor):
    alias = schema_editor.connection.alias
    Mapping = apps.get_model('omop_core', 'FieldConceptMapping')
    Concept = apps.get_model('omop_core', 'Concept')
    Membership = apps.get_model('omop_core', 'FieldDiseaseMembership')
    Category = apps.get_model('omop_core', 'DiseaseCategoryChoice')

    mapping = Mapping.objects.using(alias).filter(field_name=FIELD).first()
    if mapping is None:
        return  # an instance that never had the field
    if mapping.concept_id is None and not mapping.concept_code:
        concept = Concept.objects.using(alias).filter(
            vocabulary_id=CONCEPT_VOCABULARY, concept_code=CONCEPT_CODE,
            standard_concept='S', invalid_reason__isnull=True,
        ).first()
        mapping.vocabulary_id = CONCEPT_VOCABULARY
        mapping.concept_code = CONCEPT_CODE
        mapping.concept = concept
        mapping.save(update_fields=['vocabulary_id', 'concept_code', 'concept'])
        if concept is None:
            logger.info('%s: concept %s %s is not loaded; it is resolved with the vocabulary.',
                        FIELD, CONCEPT_VOCABULARY, CONCEPT_CODE)

    if not Membership.objects.using(alias).filter(field_name=FIELD).exists():
        known = set(Category.objects.using(alias).values_list('code', flat=True))
        Membership.objects.using(alias).bulk_create([
            Membership(field_name=FIELD, disease=code, provenance=PROVENANCE)
            for code in HEMATOLOGIC_SCOPE if code in known
        ])


class Migration(migrations.Migration):

    dependencies = [('omop_core', '0375_recordrevision_source')]

    operations = [
        migrations.RunPython(repair, migrations.RunPython.noop),
    ]
