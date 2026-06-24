from django.db import migrations

# Raw disease titles that EXACT's matcher does not recognise, mapped to the
# canonical titles it gates on (ADR 0001). Mirrors _DISEASE_ALIASES in
# omop_core/services/patient_info_service.py, kept self-contained here so the
# migration is stable even if the service mapping later changes. Keyed by the
# lowercased disease string; only exact matches are remapped.
_DISEASE_ALIASES = {
    'myeloma': 'multiple myeloma',
}


def _slugify_disease(name: str) -> str:
    import re
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    return slug[:100]


def canonicalize_disease(apps, schema_editor):
    """Canonicalize existing PatientInfo.disease (and disease_slug) values.

    Only rows whose disease, lowercased and trimmed, exactly matches an alias key
    are touched; every other row passes through untouched (preserve, don't drop).
    The matching read-model derivation now applies the same aliases on refresh
    (patient_info_service._canonicalize_disease), so refreshed rows stay consistent.
    """
    import logging
    logger = logging.getLogger(__name__)

    PatientInfo = apps.get_model('omop_core', 'PatientInfo')
    rows_to_update = []
    remapped: dict = {}

    qs = PatientInfo.objects.exclude(disease__isnull=True).exclude(disease='')
    for pi in qs.iterator():
        canonical = _DISEASE_ALIASES.get((pi.disease or '').strip().lower())
        if canonical is None or canonical == pi.disease:
            continue
        remapped[pi.disease] = remapped.get(pi.disease, 0) + 1
        pi.disease = canonical
        pi.disease_slug = _slugify_disease(canonical)
        rows_to_update.append(pi)

    if rows_to_update:
        PatientInfo.objects.bulk_update(
            rows_to_update, ['disease', 'disease_slug'], batch_size=500
        )

    if remapped:
        logger.warning(
            "canonicalize_disease: remapped %d PatientInfo row(s) to canonical "
            "disease titles: %s",
            sum(remapped.values()),
            remapped,
        )


def reverse_canonicalize_disease(apps, schema_editor):
    # The original (pre-canonical) disease strings cannot be recovered after
    # remapping — multiple raw forms collapse onto one canonical title. Rolling
    # back leaves the canonical values in place. Manual correction is required
    # if the original strings must be restored.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0088_add_person_actor_identity'),
    ]

    operations = [
        migrations.RunPython(canonicalize_disease, reverse_canonicalize_disease),
    ]
