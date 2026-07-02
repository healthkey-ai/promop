# Generated 2026-07-02 — add standalone index on patient_info.organization_id
#
# The composite index ix_pi_org_updated_at (organization_id, updated_at DESC)
# already exists, but PostgreSQL may prefer a single-column index for pure
# equality filters without an ORDER BY clause (e.g. cohort queries,
# PatientInfoViewSet list, bulk_delete ACL checks).
#
# Use RunSQL with CONCURRENTLY so the index builds without locking the table.

from django.db import migrations


class Migration(migrations.Migration):
    # CONCURRENTLY cannot run inside a transaction block; mark the whole
    # migration non-atomic so Django doesn't wrap it in BEGIN/COMMIT.
    # Safe here because IF NOT EXISTS makes the operation idempotent on retry.
    atomic = False

    dependencies = [
        ("omop_core", "0101_organization_allows_public_aggregated_data"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_pi_organization_id
                ON patient_info (organization_id);
            """,
            reverse_sql="DROP INDEX IF EXISTS ix_pi_organization_id;",
        ),
    ]
