from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0094_concept_name_trigram_index'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # 1. Rename the model (renames the table professional_group_access → group_access)
        migrations.RenameModel(
            old_name='ProfessionalGroupAccess',
            new_name='GroupAccess',
        ),
        # 2. Make group nullable (was non-nullable)
        migrations.AlterField(
            model_name='groupaccess',
            name='group',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='access_grants',
                to='omop_core.patientgroup',
            ),
        ),
        # 3. Add org FK
        migrations.AddField(
            model_name='groupaccess',
            name='org',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='access_grants',
                to='omop_core.organization',
            ),
        ),
        # 4. Replace old unique constraint with partial ones + check constraint
        migrations.RemoveConstraint(
            model_name='groupaccess',
            name='uq_identity_group',
        ),
        migrations.AddConstraint(
            model_name='groupaccess',
            constraint=models.CheckConstraint(
                check=(
                    Q(org__isnull=False, group__isnull=True) |
                    Q(org__isnull=True, group__isnull=False)
                ),
                name='group_access_org_xor_group',
            ),
        ),
        migrations.AddConstraint(
            model_name='groupaccess',
            constraint=models.UniqueConstraint(
                fields=['identity', 'group'],
                condition=Q(group__isnull=False),
                name='uq_identity_group',
            ),
        ),
        migrations.AddConstraint(
            model_name='groupaccess',
            constraint=models.UniqueConstraint(
                fields=['identity', 'org'],
                condition=Q(org__isnull=False),
                name='uq_identity_org',
            ),
        ),
        # 5. Update role choices (Python state only — no DB op needed)
        migrations.AlterField(
            model_name='groupaccess',
            name='role',
            field=models.CharField(
                choices=[
                    ('org_admin', 'Org Admin'),
                    ('doctor', 'Doctor'),
                    ('navigator', 'Navigator'),
                ],
                max_length=20,
            ),
        ),
        # 6. Update db_table (RenameModel auto-renames but we need to set the explicit Meta)
        migrations.AlterModelTable(
            name='groupaccess',
            table='group_access',
        ),
    ]
