"""Retire the invented kappa/lambda FLC codes and move their rows to real LOINC.

The MM demo generator (``_mm_generator``) labelled kappa free light chains
``33944-8`` and lambda ``33945-5``. Neither is a LOINC code: the LOINC release
has no such entries, and ``concept_fixtures`` seeded Concept rows for them
under promop's own ids (9001004, 9001005) with ``vocabulary_id='LOINC'``, so
an invented code sat in the standard range one digit away from the real lambda
code 33944-0. The real codes are kappa ``36916-5`` (Athena 3034860) and lambda
``33944-0`` (Athena 3047169), both already approved mappings or aliases of
``kappa_flc`` / ``lambda_flc``.

For each invented code this migration:

- rewrites ``measurement_source_value`` to the real code on every Measurement
  row carrying it, and repoints ``measurement_concept`` (and
  ``measurement_source_concept``) from the invented Concept to the real one
  when that Concept is loaded; without Athena the FK is left as is, so the
  row still has a concept and the code alone routes it;
- deletes the invented Concept's embeddings and then the Concept itself, when
  nothing references it any more (a still-referenced one is left, protected).

Reverse is a no-op: the recoded rows are not moved back and the deleted
concepts are not recreated; the original codes are recoverable only from a
backup. Idempotent: a second run finds no rows under the invented codes.
"""
from django.db import migrations, transaction
from django.db.models import Q
from django.db.models.deletion import ProtectedError


# invented code → (invented concept id, real LOINC code)
RECODE = {
    '33944-8': (9001004, '36916-5'),
    '33945-5': (9001005, '33944-0'),
}


def recode_demo_flc(apps, schema_editor):
    Concept = apps.get_model('omop_core', 'Concept')
    Measurement = apps.get_model('omop_core', 'Measurement')
    ConceptEmbedding = apps.get_model('omop_core', 'ConceptEmbedding')
    db = schema_editor.connection.alias

    for fake_code, (fake_id, real_code) in RECODE.items():
        real = Concept.objects.using(db).filter(
            vocabulary_id='LOINC', concept_code=real_code,
        ).first()
        fake = Concept.objects.using(db).filter(
            pk=fake_id, vocabulary_id='LOINC', concept_code=fake_code,
        ).first()
        rows = Measurement.objects.using(db).filter(
            Q(measurement_source_value=fake_code)
            | Q(measurement_concept_id=fake_id)
            | Q(measurement_source_concept_id=fake_id)
        )
        rows.filter(measurement_source_value=fake_code).update(
            measurement_source_value=real_code,
        )
        if real is not None:
            rows.filter(measurement_concept_id=fake_id).update(
                measurement_concept_id=real.pk,
            )
            rows.filter(measurement_source_concept_id=fake_id).update(
                measurement_source_concept_id=real.pk,
            )
        if fake is None:
            continue
        ConceptEmbedding.objects.using(db).filter(concept_id=fake_id).delete()
        try:
            with transaction.atomic(using=db):
                fake.delete()
        except ProtectedError:
            # Still referenced (no real concept to repoint to, or another
            # table); the rows are recoded, the concept waits for Athena.
            pass


def reverse(apps, schema_editor):
    # Recoded rows and deleted concepts are left as they are; see the docstring.
    pass


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0326_approve_lab_field_mappings')]
    operations = [migrations.RunPython(recode_demo_flc, reverse)]
