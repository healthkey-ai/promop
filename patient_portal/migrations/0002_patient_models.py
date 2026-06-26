"""Create PatientUser, PatientMessage, PatientConsent models.

On dev this migration was generated normally and is already applied.

PRODUCTION SAFETY — why SeparateDatabaseAndState is used here:
  main's 0001_initial was completely different: it created PatientUser/PatientMessage
  using auth.User, not Identity.  After the AUTH_USER_MODEL switch to
  patient_portal.Identity the migration history was restructured, so the
  production DB (main branch) has:
    - 0001_initial "applied" (by name) but it created the OLD schema (no identity table)
    - identity table    : MISSING
    - patient_user      : EXISTS (old structure with user_id → auth_user FK)
    - patient_message   : EXISTS
    - patient_consent   : EXISTS

  database_operations: CREATE TABLE IF NOT EXISTS for every table.
    • Production: creates identity + M2M junction tables; patient_user/message/consent
      statements are no-ops (tables already exist).
    • Fresh DB: identity and M2M tables already created by 0001_initial → all IF NOT EXISTS
      are no-ops.  patient_user/message/consent are created here.
    • Dev (already applied): Django never re-runs applied migrations.

  state_operations: the original AlterField + CreateModel operations, unchanged.
    These update Django's ORM state without touching the DB.

  0004_rename_patient_user_user_id_to_identity_id handles the column rename on
  production DBs that still have user_id instead of identity_id.
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0070_add_loinc_code_class'),
        ('auth', '0012_alter_user_first_name_max_length'),
        ('patient_portal', '0001_initial'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            # ------------------------------------------------------------------
            # DATABASE OPERATIONS
            # Every statement uses IF NOT EXISTS / DO $$ … END $$ so this
            # migration is safe on all three DB states described above.
            # ------------------------------------------------------------------
            database_operations=[
                migrations.RunSQL(
                    sql="""
-- Identity table (AUTH_USER_MODEL).
-- On production the old 0001_initial created PatientUser (not Identity).
CREATE TABLE IF NOT EXISTS identity (
    id           bigserial    PRIMARY KEY,
    password     varchar(128) NOT NULL,
    last_login   timestamptz,
    is_superuser boolean      NOT NULL DEFAULT false,
    issuer       varchar(255) NOT NULL,
    sub          varchar(255) NOT NULL,
    email        varchar(254) NOT NULL DEFAULT '',
    name         varchar(255) NOT NULL DEFAULT '',
    is_active    boolean      NOT NULL DEFAULT true,
    is_staff     boolean      NOT NULL DEFAULT false,
    created_at   timestamptz  NOT NULL DEFAULT NOW()
);
-- M2M junction tables for Identity.groups and Identity.user_permissions.
-- Django's CreateModel creates these on fresh DBs; production is missing them.
CREATE TABLE IF NOT EXISTS identity_groups (
    id           bigserial PRIMARY KEY,
    identity_id  bigint    NOT NULL
        REFERENCES identity(id) DEFERRABLE INITIALLY DEFERRED,
    group_id     integer   NOT NULL
        REFERENCES auth_group(id) DEFERRABLE INITIALLY DEFERRED,
    UNIQUE (identity_id, group_id)
);
CREATE INDEX IF NOT EXISTS identity_groups_identity_id_idx
    ON identity_groups (identity_id);
CREATE INDEX IF NOT EXISTS identity_groups_group_id_idx
    ON identity_groups (group_id);

CREATE TABLE IF NOT EXISTS identity_user_permissions (
    id             bigserial PRIMARY KEY,
    identity_id    bigint    NOT NULL
        REFERENCES identity(id) DEFERRABLE INITIALLY DEFERRED,
    permission_id  integer   NOT NULL
        REFERENCES auth_permission(id) DEFERRABLE INITIALLY DEFERRED,
    UNIQUE (identity_id, permission_id)
);
CREATE INDEX IF NOT EXISTS identity_user_permissions_identity_id_idx
    ON identity_user_permissions (identity_id);
CREATE INDEX IF NOT EXISTS identity_user_permissions_permission_id_idx
    ON identity_user_permissions (permission_id);

-- PatientUser: on production already exists with user_id column.
-- 0004_rename_patient_user_user_id_to_identity_id handles the rename.
CREATE TABLE IF NOT EXISTS patient_user (
    id           bigserial   PRIMARY KEY,
    is_active    boolean     NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT NOW(),
    last_login   timestamptz,
    identity_id  bigint      UNIQUE
        REFERENCES identity(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
    person_id    bigint      UNIQUE
        REFERENCES person(person_id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
);

-- PatientMessage: on production already exists.
CREATE TABLE IF NOT EXISTS patient_message (
    id                bigserial    PRIMARY KEY,
    subject           varchar(200) NOT NULL,
    message           text         NOT NULL,
    sender_is_patient boolean      NOT NULL DEFAULT true,
    is_read           boolean      NOT NULL DEFAULT false,
    created_at        timestamptz  NOT NULL DEFAULT NOW(),
    patient_user_id   bigint       NOT NULL
        REFERENCES patient_user(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX IF NOT EXISTS patient_message_patient_user_id_idx
    ON patient_message (patient_user_id);

-- PatientConsent: on production already exists.
CREATE TABLE IF NOT EXISTS patient_consent (
    id               bigserial   PRIMARY KEY,
    consent_type     varchar(50) NOT NULL,
    consent_granted  boolean     NOT NULL DEFAULT false,
    consent_date     timestamptz NOT NULL DEFAULT NOW(),
    consent_document text,
    patient_user_id  bigint      NOT NULL
        REFERENCES patient_user(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
    UNIQUE (patient_user_id, consent_type)
);
CREATE INDEX IF NOT EXISTS patient_consent_patient_user_id_idx
    ON patient_consent (patient_user_id);
""",
                    reverse_sql=migrations.RunSQL.noop,
                ),
            ],
            # ------------------------------------------------------------------
            # STATE OPERATIONS (identical to the original migration content)
            # These update Django's ORM state; no DB operations are performed.
            # ------------------------------------------------------------------
            state_operations=[
                migrations.AlterField(
                    model_name='identity',
                    name='groups',
                    field=models.ManyToManyField(
                        blank=True,
                        help_text='The groups this user belongs to. A user will get all permissions granted to each of their groups.',
                        related_name='user_set', related_query_name='user',
                        to='auth.group', verbose_name='groups',
                    ),
                ),
                migrations.AlterField(
                    model_name='identity',
                    name='is_superuser',
                    field=models.BooleanField(
                        default=False,
                        help_text='Designates that this user has all permissions without explicitly assigning them.',
                        verbose_name='superuser status',
                    ),
                ),
                migrations.AlterField(
                    model_name='identity',
                    name='user_permissions',
                    field=models.ManyToManyField(
                        blank=True,
                        help_text='Specific permissions for this user.',
                        related_name='user_set', related_query_name='user',
                        to='auth.permission', verbose_name='user permissions',
                    ),
                ),
                migrations.CreateModel(
                    name='PatientUser',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('is_active', models.BooleanField(default=True)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('last_login', models.DateTimeField(blank=True, null=True)),
                        ('identity', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='patient_user', to=settings.AUTH_USER_MODEL)),
                        ('person', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='portal_user', to='omop_core.person')),
                    ],
                    options={
                        'db_table': 'patient_user',
                    },
                ),
                migrations.CreateModel(
                    name='PatientMessage',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('subject', models.CharField(max_length=200)),
                        ('message', models.TextField()),
                        ('sender_is_patient', models.BooleanField(default=True)),
                        ('is_read', models.BooleanField(default=False)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('patient_user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='messages', to='patient_portal.patientuser')),
                    ],
                    options={
                        'db_table': 'patient_message',
                        'ordering': ['-created_at'],
                    },
                ),
                migrations.CreateModel(
                    name='PatientConsent',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('consent_type', models.CharField(choices=[('data_sharing', 'Data Sharing'), ('clinical_trial', 'Clinical Trial Participation'), ('research', 'Research Use')], max_length=50)),
                        ('consent_granted', models.BooleanField(default=False)),
                        ('consent_date', models.DateTimeField(auto_now_add=True)),
                        ('consent_document', models.TextField(blank=True, null=True)),
                        ('patient_user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='consents', to='patient_portal.patientuser')),
                    ],
                    options={
                        'db_table': 'patient_consent',
                        'unique_together': {('patient_user', 'consent_type')},
                    },
                ),
            ],
        ),
    ]
