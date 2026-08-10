"""Correct concept 0 to the OMOP-specified "No matching concept" row.

concept 0 is OMOP's universal sentinel, written to any `*_concept_id` when
source data cannot be mapped. It is domain-agnostic by design.

On staging it was stored as:

    (0, 'No matching concept', Measurement, HK-Labs, Lab Test, NULL, '0', 'HealthKey')

against the specification:

    (0, 'No matching concept', Metadata, None, Undefined, NULL, 'No matching concept')

Because it is referenced across every domain — 12,352 observations, 8,131 drug
exposures, 290 conditions and 14 measurements — the wrong vocabulary and domain
made ~20,800 unmapped rows of all kinds appear, to any query grouping by
vocabulary or domain, as HealthKey lab tests. That directly caused a misreading
during the #415 audit.

`source='HealthKey'` was also wrong: concept 0 is standard OMOP content, not a
local mint, so it polluted the `?source=external` vocabulary-mirror filter.

Root cause: 0068_create_hk_labs_vocabulary creates concept 0 with
(Measurement, HK-Labs, Lab Test) — it needed the sentinel for LOINC-unmatched
measurements and borrowed the vocabulary it had just created.
0077_seed_concept_zero then tried to seed the correct values, but with
get_or_create, which no-ops when the row already exists. The correction was
therefore written but never applied on any database that had run 0068.

This migration uses save() on the existing row for exactly that reason.

This is a metadata-only correction to a single row. It touches no clinical data
and no foreign keys: every reference to concept 0 stays valid, because the
concept_id does not change.

Safe as an auto-applied migration (start.sh runs migrate on deploy) precisely
because of that — unlike the bulk row rewrites in #413, which were deliberately
kept as management commands.
"""
from django.db import migrations

CONCEPT_ZERO = {
    'concept_name': 'No matching concept',
    'domain_id': 'Metadata',
    'vocabulary_id': 'None',
    'concept_class_id': 'Undefined',
    'standard_concept': None,
    'concept_code': 'No matching concept',
    'source': None,
}

# Prerequisite rows for concept 0's foreign keys. get_or_create'd rather than
# assumed: a database seeded only by seed_omop_concepts before this change has
# no 'None' vocabulary and no 'Undefined' concept class.
_VOCABULARY = ('None', 'OMOP Standardized Vocabularies', 'OMOP generated', 'v5')
_DOMAIN = ('Metadata', 'Metadata')
_CONCEPT_CLASS = ('Undefined', 'Undefined')


def fix_concept_zero(apps, schema_editor):
    Concept = apps.get_model('omop_core', 'Concept')
    Vocabulary = apps.get_model('omop_core', 'Vocabulary')
    Domain = apps.get_model('omop_core', 'Domain')
    ConceptClass = apps.get_model('omop_core', 'ConceptClass')

    concept = Concept.objects.filter(concept_id=0).first()
    if concept is None:
        # Nothing to correct. Do not create it here — seed_omop_concepts owns
        # seeding, and a fresh database gets the correct row from there.
        return

    vid, vname, vref, vver = _VOCABULARY
    Vocabulary.objects.get_or_create(
        vocabulary_id=vid,
        defaults={'vocabulary_name': vname, 'vocabulary_reference': vref,
                  'vocabulary_version': vver, 'vocabulary_concept_id': 0},
    )
    did, dname = _DOMAIN
    Domain.objects.get_or_create(
        domain_id=did, defaults={'domain_name': dname, 'domain_concept_id': 0})
    ccid, ccname = _CONCEPT_CLASS
    ConceptClass.objects.get_or_create(
        concept_class_id=ccid,
        defaults={'concept_class_name': ccname, 'concept_class_concept_id': 0})

    for field, value in CONCEPT_ZERO.items():
        setattr(concept, field, value)
    concept.save(update_fields=list(CONCEPT_ZERO))


def reverse_noop(apps, schema_editor):
    """Irreversible by design.

    The previous values were themselves incorrect and varied by database — one
    had (HK-Labs, Measurement, Lab Test), a freshly seeded one had no concept 0
    at all. There is no single prior state to restore, and restoring a wrong one
    would be worse than leaving the corrected row in place.
    """


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0139_patient_info_view_death_date'),
    ]

    operations = [
        migrations.RunPython(fix_concept_zero, reverse_noop),
    ]
