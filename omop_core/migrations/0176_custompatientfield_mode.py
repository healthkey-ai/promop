from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [('omop_core', '0175_add_remission_duration')]

    operations = [
        migrations.AddField(
            model_name='custompatientfield',
            name='mode',
            field=models.CharField(
                choices=[('editable', 'Editable'), ('computed', 'Computed')],
                default='editable', max_length=20,
            ),
        ),
    ]
