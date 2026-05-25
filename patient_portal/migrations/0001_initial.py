"""Identity model (AUTH_USER_MODEL) — no omop_core dependency.

Split from PatientUser to break circular dependency:
patient_portal → omop_core → oauth2_provider → patient_portal (via AUTH_USER_MODEL)
"""
from django.db import migrations, models
import patient_portal.models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.CreateModel(
            name='Identity',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('password', models.CharField(max_length=128, verbose_name='password')),
                ('last_login', models.DateTimeField(blank=True, null=True, verbose_name='last login')),
                ('is_superuser', models.BooleanField(default=False, verbose_name='superuser status')),
                ('issuer', models.CharField(max_length=255)),
                ('sub', models.CharField(max_length=255, unique=True)),
                ('email', models.EmailField(blank=True, default='', max_length=254)),
                ('name', models.CharField(blank=True, default='', max_length=255)),
                ('is_active', models.BooleanField(default=True)),
                ('is_staff', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('groups', models.ManyToManyField(blank=True, related_name='user_set', related_query_name='user', to='auth.group', verbose_name='groups')),
                ('user_permissions', models.ManyToManyField(blank=True, related_name='user_set', related_query_name='user', to='auth.permission', verbose_name='user permissions')),
            ],
            options={
                'verbose_name_plural': 'identities',
                'db_table': 'identity',
            },
            managers=[
                ('objects', patient_portal.models.IdentityManager()),
            ],
        ),
        migrations.AddConstraint(
            model_name='identity',
            constraint=models.UniqueConstraint(fields=('issuer', 'sub'), name='uq_identity_issuer_sub'),
        ),
    ]
