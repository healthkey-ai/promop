"""Drop the retired survey tables — but never with anything still in them.

`omop_core.Survey` / `PatientSurveyResponse` are replaced by PROlog (decision 9
in PROlog's requirements). This drops them, which destroys patient-entered data
if it has not been converted first, and `start.sh` runs `migrate` on every
deploy — so the check below fails the deploy rather than the data.

The upgrade is:

    python manage.py migrate_surveys_to_prolog            # see what would move
    python manage.py migrate_surveys_to_prolog --apply    # move it
    python manage.py migrate                              # then this runs

The converter reads these tables with SQL rather than through models, so it
still works in this release, where the models are already gone and the tables
are not. That gap is deliberate: it is the window in which an operator upgrades.
"""

from django.db import migrations

_STILL_POPULATED = (
    "{count} survey response(s) are still in `patient_survey_response`.\n"
    "\n"
    "Dropping this table now would destroy them. Convert them first:\n"
    "\n"
    "    python manage.py migrate_surveys_to_prolog --apply\n"
    "\n"
    "then run migrate again. If they are genuinely not wanted, delete them\n"
    "deliberately — this migration will not decide that for you."
)


def refuse_if_unmigrated(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM patient_survey_response")
        (count,) = cursor.fetchone()
    if count:
        raise RuntimeError(_STILL_POPULATED.format(count=count))


def noop(apps, schema_editor):
    """Reversing drops nothing, so there is nothing to check on the way back."""


class Migration(migrations.Migration):
    dependencies = [
        ("omop_core", "0200_merge_20260901_0000"),
    ]

    operations = [
        migrations.RunPython(refuse_if_unmigrated, noop),
        migrations.DeleteModel(name="PatientSurveyResponse"),
        migrations.DeleteModel(name="Survey"),
    ]
