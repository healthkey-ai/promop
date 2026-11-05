"""Retire the invented and mis-identified standard codes the demo generators emit.

An audit of every LOINC, SNOMED and RxNorm code emitted by the myeloma,
follicular-lymphoma and breast-cancer generators, ``concept_fixtures`` and the
bulk importer's ``MEASUREMENT_LOINC`` against the loaded LOINC release and
Athena vocabularies found two kinds of defect:

- **invented codes**: strings shaped like a LOINC code that no release
  contains (``24646-7`` "Bone lesions", ``47082-2`` "Plasma cell leukemia",
  ``32730-5`` urine M-spike, ``11286-7`` drinks per week, ``77243-3`` annual
  income, ``21908-9-riss`` ...). Five of them had Concept rows minted under
  ``vocabulary_id='LOINC'`` (ids 9001003, 2029606278, 1872307076, 2100000012,
  2100000014), so an invented code sat in the standard range and resolved;
- **mis-identified codes**: real LOINC codes used for something else
  (``89599-5`` "Meets CRAB criteria" is a Toxoplasma CSF culture, ``57905-2``
  "Myeloma immunoglobulin type" is a stool haemoglobin test, ``21861-0``
  "Prior lines of therapy" is "Dx confirmed by Cancer", the cytogenetic
  marker codes are HL7 genomics reference-sequence metadata).

Where the release has the real test it is used; where no vocabulary says the
thing (a CRAB boolean, an R-ISS stage, the drug classes a myeloma is
refractory to, capacity to consent, menopausal status) a HealthKey concept
is minted in the ``HK-Observation`` vocabulary in OHDSI's local range.

For each retired code this migration:

- mints the HK-Observation concepts the generators and derivation now use
  (``_CONCEPTS``; ``concept_fixtures`` duplicates them for test databases);
- rewrites ``*_source_value`` on Measurement and Observation rows from the
  retired code to its replacement, repointing ``*_concept`` and
  ``*_source_concept`` to the replacement Concept when it is loaded. A row
  under a mis-identified *real* code is recoded only when it is attached to
  concept 0 or to the mis-identified concept **and** its value has the shape
  the generator wrote (a 0/1 boolean, a myeloma isotype label, a small line
  count), so a genuine result stored under the real code is left alone;
- moves the FieldConceptMapping rows that still point at a retired concept
  or code to the replacement, and clears ``bone_imaging_result`` (its only
  concept was invented and no vocabulary has a bone-lesion imaging result);
- deletes the SourceCodeConceptMapping rows for the invented codes (the
  0285 alias for ``32730-5`` and the queue rows ingest created for them);
- deletes the invented LOINC-vocabulary Concepts, embeddings first, once
  nothing references them (a still-referenced one is left, protected).

Out of scope here, and left as they are: the HealthKey-range concepts tagged
``SNOMED`` or ``LOINC`` that ``field_curation_v1.json`` seeds (ids 2100000001
to 2100000043, such as SNOMED 711259004 for ``measurable_disease_iwcll`` or
LOINC 98847-0 for ``mrd_status``). They are a curation pass of their own; the
one the generators touched (``measurable_disease_imwg``) moves here.

Reverse is a no-op: recoded rows are not moved back and the deleted
concepts are not recreated. Idempotent: a second run finds nothing under
the retired codes. Test databases are not skipped — the mints are wanted
there and the rest finds no rows.
"""
from datetime import date

import logging

from django.db import migrations, transaction
from django.db.models import Q
from django.db.models.deletion import ProtectedError

logger = logging.getLogger(__name__)


HK_VOCABULARY = 'HK-Observation'

# HealthKey concepts minted here: (concept_id, concept_code, concept_name).
# Frozen; concept_fixtures duplicates the rows and omop_core.tests asserts the
# two copies agree. The ids continue the 0180 block (2_100_007_850..852);
# 2_100_007_853..864 are taken by 0187 and curator mints on staging.
_CONCEPTS = (
    (2_100_007_870, 'hko:bone-lesions', 'Lytic bone lesions present (myeloma CRAB criterion)'),
    (2_100_007_871, 'hko:meets-crab-criteria', 'Meets CRAB criteria (myeloma-defining event)'),
    (2_100_007_872, 'hko:measurable-disease-imwg', 'Measurable disease by IMWG criteria'),
    (2_100_007_873, 'hko:refractory-drug-classes', 'Drug classes the disease is refractory to'),
    (2_100_007_874, 'hko:r-iss-stage', 'Revised International Staging System (R-ISS) stage'),
    (2_100_007_875, 'hko:prior-lines-of-therapy', 'Number of prior lines of therapy'),
    (2_100_007_876, 'hko:consent-capability', 'Capacity to consent to treatment'),
    (2_100_007_877, 'hko:menopausal-status', 'Menopausal status'),
)

LOCAL_CONCEPT_ID_MIN = 2_000_000_000

# Value-shape guards for rows stored under a mis-identified *real* code.
BOOLEAN = 'boolean'          # value_as_number in (0, 1), no string
SMALL_INTEGER = 'integer'    # value_as_number 0..20, no string
PERCENT = 'percent'          # value_as_number 0..100, no string
ISOTYPE = 'isotype'          # the generator's myeloma isotype labels
PREFIX = 'prefix:'           # value_as_string starts with the text after the colon
VALUES = 'values:'           # value_as_string is one of the '|'-separated texts after the colon

MYELOMA_ISOTYPES = ('IgG', 'IgA', 'IgM', 'IgD', 'IgE', 'light_chain_only', 'non_secretory')
# The closed vocabularies the breast-cancer generator wrote; a genuine result
# under the mis-identified code (a sexual orientation, a fungus) is none of them.
THERAPY_INTENTS = ('DEFINITIVE_LOCAL', 'ADJUVANT', 'NEOADJUVANT', 'INDUCTION', 'CONSOLIDATION',
                   'MAINTENANCE', 'METASTATIC_DISEASE_CONTROL', 'PALLIATIVE_SYMPTOM', 'SALVAGE')
MENOPAUSAL_STATES = ('Premenopausal', 'Perimenopausal', 'Postmenopausal')

# retired code -> (replacement vocabulary, replacement code, guard or None).
# A guard of None means the old code is invented: every row under it is the
# generator's, and all of them move.
RECODE = {
    # Myeloma labs and disease burden
    '32730-5': ('LOINC', '42482-0', None),                      # urine M-spike, 24h
    '24646-7': (HK_VOCABULARY, 'hko:bone-lesions', None),
    '47082-2': ('SNOMED', '95210003', None),                    # Plasma cell leukemia
    '33728-8': ('SNOMED', '246450006', None),                   # Progression
    '73643-8': ('LOINC', '75691-6', None),                      # Peripheral sensory neuropathy grade
    '85598-2': (HK_VOCABULARY, 'hko:measurable-disease-imwg', None),
    '85330-2': (HK_VOCABULARY, 'hko:refractory-drug-classes', None),
    '21908-9-riss': (HK_VOCABULARY, 'hko:r-iss-stage', None),
    '89599-5': (HK_VOCABULARY, 'hko:meets-crab-criteria', BOOLEAN),   # was Toxoplasma CSF culture
    '57905-2': ('SNOMED', '415109007', ISOTYPE),                      # was stool haemoglobin
    '21861-0': (HK_VOCABULARY, 'hko:prior-lines-of-therapy', SMALL_INTEGER),  # was Dx confirmed by
    # Cytogenetic markers (per-marker booleans)
    '72838-3': ('LOINC', '81746-0', None),                      # del(17p)
    '72842-5': ('LOINC', '72726-3', None),                      # t(4;14)
    '72840-9': ('LOINC', '81749-4', None),                      # del(13q)
    '81250-3': ('LOINC', '77033-9', BOOLEAN),                   # t(14;16); was a variant panel
    '81249-5': ('LOINC', '81752-8', BOOLEAN),                   # 1q21 gain; was a reference-sequence type
    '81248-7': ('LOINC', '30914-6', BOOLEAN),                   # hyperdiploidy; was a reference-sequence type
    # Social history (invented spellings)
    '11286-7': ('LOINC', '44940-5', None),                      # Alcoholic drinks per week
    '77243-3': ('LOINC', '63586-2', None),                      # Total family income, last year
    # Eligibility assertions (invented spellings)
    '8659-8': ('SNOMED', '13197004', None),                     # Uses contraception
    '75985-6': (HK_VOCABULARY, 'hko:consent-capability', None),
    '75618-3': ('LOINC', '45420-7', None),                      # Mental health history
    '74204-0': ('LOINC', '74204-9', None),                      # Drug use [NTDS]
    # Lymphoma
    '85319-5': ('LOINC', '32525-8', None),                      # CD19 cells/cells in Bone marrow
    # Therapy lines
    '91379-3': ('LOINC', '64882-4', None),                      # Reason stopped
    '42804-5': ('LOINC', '104803-2', VALUES + '|'.join(THERAPY_INTENTS)),  # Therapeutic intent; was Fungus identified
    # Breast cancer genomics and biomarkers
    '47423-8': ('LOINC', '103680-5', None),                     # TP53
    '88039-1': ('LOINC', '81845-0', None),                      # PALB2
    '21637-4': ('LOINC', '79208-5', PREFIX + 'BRCA2'),          # was BRCA1 c.185delAG
    '21667-1': ('LOINC', '103680-5', PREFIX + 'TP53'),          # was F5 gene mutations
    '62318-1': ('LOINC', '60034-6', PREFIX + 'PIK3CA'),         # was Niemann-Pick newborn screen
    '76690-7': (HK_VOCABULARY, 'hko:menopausal-status', VALUES + '|'.join(MENOPAUSAL_STATES)),  # was Sexual orientation
    '85336-6': ('LOINC', '105305-7', PERCENT),                  # PD-L1 immune cells; was DCIS extension
    '96893-3': ('LOINC', '105303-2', PERCENT),                  # PD-L1 CPS; was ERBB2 duplication
}

# Invented codes that had Concept rows minted under vocabulary_id='LOINC'.
INVENTED_LOINC_CONCEPT_CODES = ('32730-5', '11286-7', '77243-3', '24646-7', '47082-2')

# Codes whose SourceCodeConceptMapping rows (the 0285 alias and the queue rows
# ingest created) go: only the invented spellings, never a real code.
RETIRED_SOURCE_CODES = tuple(code for code, (_v, _c, guard) in RECODE.items() if guard is None) + (
    '48346-3', '33944-8', '33945-5',
)

# FieldConceptMapping rows to move: field -> (old vocabulary, old code,
# new vocabulary, new code, note). A row that no longer carries the old
# code was re-curated and is left alone.
FCM_REPOINT = {
    'annual_household_income': ('LOINC', '77243-3', 'LOINC', '63586-2',
                                'LOINC 63586-2 "total income of all family members ... in last year [PhenX]" '
                                '(Athena 40766311). 77243-3 is not a LOINC code; migration 0329.'),
    'drinks_per_week': ('LOINC', '11286-7', 'LOINC', '44940-5',
                        'LOINC 44940-5 "Alcoholic drinks per week - Reported" (Athena 3043872). '
                        '11286-7 is not a LOINC code; migration 0329.'),
    'bone_lesions': ('LOINC', '24646-7', HK_VOCABULARY, 'hko:bone-lesions',
                     'HealthKey hko:bone-lesions: LOINC has no lytic-bone-lesion observable and '
                     '24646-7 is not a LOINC code; migration 0329.'),
    'measurable_disease_imwg': ('SNOMED', '711259004', HK_VOCABULARY, 'hko:measurable-disease-imwg',
                                'HealthKey hko:measurable-disease-imwg: SNOMED 711259004 is absent from '
                                'Athena and the generator code 85598-2 was not LOINC; migration 0329.'),
    'consent_capability': ('SNOMED', '405193005', HK_VOCABULARY, 'hko:consent-capability',
                           'HealthKey hko:consent-capability: SNOMED 405193005 is "Caregiver wellbeing '
                           'status" and 75985-6 is not a LOINC code; migration 0329.'),
    'clonal_bone_marrow_b_lymphocytes': ('LOINC', '42759-1', 'LOINC', '32525-8',
                                         'LOINC 32525-8 "CD19 cells/cells in Bone marrow" (Athena 3035081): '
                                         'the field is a percentage, 42759-1 is a count; migration 0329.'),
}


def _mint_concepts(apps, db):
    Vocabulary = apps.get_model('omop_core', 'Vocabulary')
    Domain = apps.get_model('omop_core', 'Domain')
    ConceptClass = apps.get_model('omop_core', 'ConceptClass')
    Concept = apps.get_model('omop_core', 'Concept')

    Vocabulary.objects.using(db).get_or_create(
        vocabulary_id=HK_VOCABULARY,
        defaults={
            'vocabulary_name': 'HealthKey local observation source concepts',
            'vocabulary_reference': 'https://healthkey.ai',
            'vocabulary_version': '1.0',
            'vocabulary_concept_id': 0,
        },
    )
    Domain.objects.using(db).get_or_create(
        domain_id='Observation', defaults={'domain_name': 'Observation', 'domain_concept_id': 0},
    )
    ConceptClass.objects.using(db).get_or_create(
        concept_class_id='Clinical Observation',
        defaults={'concept_class_name': 'Clinical Observation', 'concept_class_concept_id': 0},
    )
    for concept_id, concept_code, concept_name in _CONCEPTS:
        if Concept.objects.using(db).filter(vocabulary_id=HK_VOCABULARY, concept_code=concept_code).exists():
            continue
        if Concept.objects.using(db).filter(pk=concept_id).exists():
            # A curator mint took the id on this instance; HealthKey ids are
            # not expected to agree across instances, the code is the key.
            taken = Concept.objects.using(db).filter(
                concept_id__gte=LOCAL_CONCEPT_ID_MIN,
            ).order_by('-concept_id').values_list('concept_id', flat=True).first()
            concept_id = (taken or LOCAL_CONCEPT_ID_MIN) + 1
        Concept.objects.using(db).create(
            concept_id=concept_id, vocabulary_id=HK_VOCABULARY, concept_code=concept_code,
            concept_name=concept_name, domain_id='Observation',
            concept_class_id='Clinical Observation', standard_concept=None,
            source='HealthKey', valid_start_date=date(1970, 1, 1),
            valid_end_date=date(2099, 12, 31), invalid_reason=None,
        )


def _guard(guard, value_field, string_field):
    """Q narrowing rows under a mis-identified real code to the generator's shape."""
    if guard is None:
        return Q()
    if guard == BOOLEAN:
        return Q(**{f'{value_field}__in': (0, 1), f'{string_field}__isnull': True})
    if guard == SMALL_INTEGER:
        return Q(**{f'{value_field}__gte': 0, f'{value_field}__lte': 20, f'{string_field}__isnull': True})
    if guard == PERCENT:
        return Q(**{f'{value_field}__gte': 0, f'{value_field}__lte': 100, f'{string_field}__isnull': True})
    if guard == ISOTYPE:
        return Q(**{f'{string_field}__in': MYELOMA_ISOTYPES})
    if guard.startswith(VALUES):
        return Q(**{f'{string_field}__in': guard[len(VALUES):].split('|')})
    if guard.startswith(PREFIX):
        return Q(**{f'{string_field}__startswith': guard[len(PREFIX):]})
    raise ValueError(guard)


def _recode_rows(apps, db):
    Concept = apps.get_model('omop_core', 'Concept')
    Measurement = apps.get_model('omop_core', 'Measurement')
    Observation = apps.get_model('omop_core', 'Observation')

    for old_code, (vocab, new_code, guard) in RECODE.items():
        new = Concept.objects.using(db).filter(vocabulary_id=vocab, concept_code=new_code).first()
        # The concept the old code resolved to: the invented row for an invented
        # code, the real-but-wrong row for a mis-identified one.
        old_ids = set(Concept.objects.using(db).filter(
            vocabulary_id='LOINC', concept_code=old_code,
        ).values_list('concept_id', flat=True)) | {0}
        for model, prefix in ((Measurement, 'measurement'), (Observation, 'observation')):
            source = f'{prefix}_source_value'
            concept = f'{prefix}_concept_id'
            source_concept = f'{prefix}_source_concept_id'
            if guard is None:
                # Every row under an invented code is the generator's: by source
                # value, or attached to the invented concept however it got there.
                rows = model.objects.using(db).filter(
                    Q(**{source: old_code})
                    | Q(**{f'{concept}__in': old_ids - {0}})
                    | Q(**{f'{source_concept}__in': old_ids - {0}})
                )
            else:
                rows = model.objects.using(db).filter(**{source: old_code}).filter(
                    _guard(guard, 'value_as_number', 'value_as_string'),
                    **{f'{concept}__in': old_ids},
                )
            ids = list(rows.values_list('pk', flat=True))
            if not ids:
                continue
            target = model.objects.using(db).filter(pk__in=ids)
            target.filter(**{source: old_code}).update(**{source: new_code})
            if new is not None:
                if guard is None:
                    target.update(**{concept: new.pk})
                else:
                    target.filter(**{f'{concept}__in': old_ids}).update(**{concept: new.pk})
                target.filter(**{f'{source_concept}__in': old_ids - {0}}).update(**{source_concept: new.pk})
            elif guard is None and (old_ids - {0}):
                # Replacement not loaded yet (Athena absent): the code routes the
                # row, and nothing may keep pointing at the invented concept or it
                # could never be deleted. Concept 0 is where the generator's rows
                # already sit on an instance without Athena.
                if Concept.objects.using(db).filter(pk=0).exists():
                    target.filter(**{f'{concept}__in': old_ids - {0}}).update(**{concept: 0})
                target.filter(**{f'{source_concept}__in': old_ids - {0}}).update(**{source_concept: None})


def _repoint_field_mappings(apps, db):
    Concept = apps.get_model('omop_core', 'Concept')
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')

    for field, (old_vocab, old_code, new_vocab, new_code, note) in FCM_REPOINT.items():
        row = FieldConceptMapping.objects.using(db).filter(field_name=field).first()
        if row is None:
            continue
        points_at_old = (row.vocabulary_id, row.concept_code) == (old_vocab, old_code) or (
            row.concept_id is not None
            and Concept.objects.using(db).filter(
                pk=row.concept_id, vocabulary_id=old_vocab, concept_code=old_code,
            ).exists()
        )
        if not points_at_old:
            continue
        if field == 'clonal_bone_marrow_b_lymphocytes' and (row.status == 'approved' or row.reviewed_at):
            # 42759-1 is a real concept with the wrong unit, not an invented
            # one; a curator who reviewed it is not overruled here.
            continue
        new = Concept.objects.using(db).filter(vocabulary_id=new_vocab, concept_code=new_code).first()
        if new is None:
            # Athena not loaded yet: the code routes, the FK resolves later.
            row.concept = None
        else:
            row.concept = new
        row.vocabulary_id = new_vocab
        row.concept_code = new_code
        if row.source_value == old_code:
            row.source_value = new_code
        row.notes = f'{row.notes} {note}'.strip() if row.notes else note
        row.save(update_fields=['concept', 'vocabulary_id', 'concept_code', 'source_value', 'notes'])

    # bone_imaging_result only ever pointed at the invented 24646-7 concept.
    # No vocabulary has a bone-lesion imaging result observable, so the
    # proposal is left without a concept rather than given a wrong one.
    row = FieldConceptMapping.objects.using(db).filter(
        field_name='bone_imaging_result', vocabulary_id='LOINC', concept_code='24646-7',
    ).first()
    if row is not None:
        row.concept = None
        row.vocabulary_id = ''
        row.concept_code = ''
        row.notes = (f'{row.notes} ' if row.notes else '') + (
            'Concept cleared by migration 0329: 24646-7 is not a LOINC code and no '
            'vocabulary has a bone-lesion imaging result observable.')
        row.save(update_fields=['concept', 'vocabulary_id', 'concept_code', 'notes'])


def _delete_retired_source_mappings(apps, db):
    SourceCodeConceptMapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    SourceCodeConceptMapping.objects.using(db).filter(
        source_code__in=RETIRED_SOURCE_CODES,
    ).filter(Q(source_vocabulary_id='LOINC') | Q(source_vocabulary_id='')).delete()


def _delete_invented_concepts(apps, db):
    Concept = apps.get_model('omop_core', 'Concept')
    ConceptEmbedding = apps.get_model('omop_core', 'ConceptEmbedding')
    ConceptRelationship = apps.get_model('omop_core', 'ConceptRelationship')
    ConceptSynonym = apps.get_model('omop_core', 'ConceptSynonym')
    ConceptAncestor = apps.get_model('omop_core', 'ConceptAncestor')
    SourceCodeConceptMapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    for fake in Concept.objects.using(db).filter(
        vocabulary_id='LOINC', concept_code__in=INVENTED_LOINC_CONCEPT_CODES,
    ):
        # Vocabulary metadata hangs off the concept through DO_NOTHING FKs:
        # Django would delete the concept and the deferred constraint would
        # fail the whole migration at commit (staging has a self "Maps to" /
        # "Mapped from" pair on 9001003). Remove it first; a mapping that
        # still targets the concept is a curator's row and keeps it alive.
        if SourceCodeConceptMapping.objects.using(db).filter(target_concept_id=fake.pk).exists():
            logger.warning('0329: concept %s is still a mapping destination; left in place', fake.pk)
            continue
        ConceptEmbedding.objects.using(db).filter(concept_id=fake.pk).delete()
        ConceptRelationship.objects.using(db).filter(
            Q(concept_1_id=fake.pk) | Q(concept_2_id=fake.pk)).delete()
        ConceptSynonym.objects.using(db).filter(concept_id=fake.pk).delete()
        ConceptAncestor.objects.using(db).filter(
            Q(ancestor_concept_id=fake.pk) | Q(descendant_concept_id=fake.pk)).delete()
        try:
            with transaction.atomic(using=db):
                fake.delete()
        except ProtectedError as exc:
            # Still referenced by a table this migration does not recode; the
            # rows are recoded and the concept waits for the next pass.
            logger.warning('0329: concept %s still referenced, left in place: %s', fake.pk, exc)


def forwards(apps, schema_editor):
    db = schema_editor.connection.alias
    _mint_concepts(apps, db)
    _recode_rows(apps, db)
    _repoint_field_mappings(apps, db)
    _delete_retired_source_mappings(apps, db)
    _delete_invented_concepts(apps, db)


def reverse(apps, schema_editor):
    # Recoded rows and deleted concepts are left as they are; see the docstring.
    pass


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0328_retire_invented_flc_codes')]
    operations = [migrations.RunPython(forwards, reverse)]
