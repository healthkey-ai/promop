from django.db import migrations


def rename_navigator_to_analyst(apps, schema_editor):
    GroupAccess = apps.get_model('omop_core', 'GroupAccess')
    OrgInvitation = apps.get_model('omop_core', 'OrgInvitation')
    GroupAccess.objects.filter(role='navigator').update(role='analyst')
    OrgInvitation.objects.filter(role='navigator').update(role='analyst')


def rename_analyst_to_navigator(apps, schema_editor):
    GroupAccess = apps.get_model('omop_core', 'GroupAccess')
    OrgInvitation = apps.get_model('omop_core', 'OrgInvitation')
    GroupAccess.objects.filter(role='analyst').update(role='navigator')
    OrgInvitation.objects.filter(role='analyst').update(role='navigator')


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0102_patient_info_organization_id_index'),
    ]

    operations = [
        migrations.RunPython(rename_navigator_to_analyst, rename_analyst_to_navigator),
    ]
