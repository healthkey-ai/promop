"""Drop the retired survey tables — but never with anything still in them.

`omop_core.Survey` / `PatientSurveyResponse` are replaced by PROlog (decision 9
in PROlog's requirements). This drops them, which destroys patient-entered data
if it has not been converted first, and `start.sh` runs `migrate` on every
deploy — so the check below fails the deploy rather than the data.

The upgrade is:

    python manage.py migrate prolog_surveys                     # create PROlog's tables
    python manage.py migrate_surveys_to_prolog                  # see what would move
    python manage.py migrate_surveys_to_prolog --apply          # convert it
    #   ... check the conversion in the portal ...
    python manage.py migrate_surveys_to_prolog --apply --purge-source
    python manage.py migrate                                    # then this runs

The first step is not redundant on a deployment upgrading from a release that
never had `prolog_surveys`: the converter writes through PROlog's models, and
they have no tables until that migration runs.

The purge step is not optional. Conversion *copies*: it leaves the legacy rows
in place so the original is still there while the result is checked, so the
check below still counts them and the migration still refuses. `--purge-source`
is what removes the rows it converted — and only those.

The converter reads these tables with SQL rather than through models, so it
still works in this release, where the models are already gone and the tables
are not. That gap is deliberate: it is the window in which an operator upgrades.
"""

from django.db import migrations

_TEMPLATES_UNCONVERTED = (
    "{count} survey template(s) in `survey` have no PROlog counterpart: {names}.\n"
    "\n"
    "Dropping this table now would destroy them. A template with no responses\n"
    "is still an instrument somebody wrote. Convert them:\n"
    "\n"
    "    python manage.py migrate_surveys_to_prolog --apply\n"
    "    python manage.py migrate_surveys_to_prolog --apply --purge-source\n"
    "\n"
    "or delete them deliberately if they are not wanted."
)

_STILL_POPULATED = (
    "{count} survey response(s) are still in `patient_survey_response`.\n"
    "\n"
    "Dropping this table now would destroy them. Convert them, then release\n"
    "the originals:\n"
    "\n"
    "    python manage.py migrate_surveys_to_prolog --apply\n"
    "    python manage.py migrate_surveys_to_prolog --apply --purge-source\n"
    "\n"
    "then run migrate again. Converting alone does not clear this check: it\n"
    "copies, and leaves the original in place on purpose. If these responses\n"
    "are genuinely not wanted, delete them deliberately — this migration will\n"
    "not decide that for you."
)


def refuse_if_unmigrated(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM patient_survey_response")
        (count,) = cursor.fetchone()
        if count:
            raise RuntimeError(_STILL_POPULATED.format(count=count))

        # Templates too. A template nobody has answered yet is not covered by
        # the response count — a newly authored or seasonal instrument, or one
        # an operator left out of a `--survey`-restricted run — and dropping
        # `survey` would take it with no record of what it said. The converter
        # records the legacy id it came from, so an unconverted one is one no
        # PROlog version names.
        #
        # PROlog's own tables may not exist yet: this migration declares no
        # dependency on that app, and on a fresh database omop_core's chain can
        # run first. Nothing can have been converted in that case, which is the
        # right answer — and on a fresh database there are no templates either,
        # so it costs nothing.
        cursor.execute("SELECT to_regclass('prolog_surveys_surveyversion')")
        (prolog_installed,) = cursor.fetchone()
        if prolog_installed:
            cursor.execute(
                """
                SELECT name FROM survey
                WHERE 'legacy survey:' || id NOT IN (
                    SELECT source FROM prolog_surveys_surveyversion WHERE source IS NOT NULL
                )
                ORDER BY name
                """
            )
        else:
            cursor.execute("SELECT name FROM survey ORDER BY name")
        unconverted = [row[0] for row in cursor.fetchall()]
    if unconverted:
        raise RuntimeError(_TEMPLATES_UNCONVERTED.format(
            count=len(unconverted), names=", ".join(repr(n) for n in unconverted),
        ))


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
