from django.db import connection, transaction

from omop_core.models import (
    ConditionOccurrence,
    ConditionEra,
    Death,
    DoseEra,
    DrugExposure,
    DrugEra,
    Measurement,
    MeasurementOwnership,
    Note,
    NoteNlp,
    Observation,
    Organization,
    PatientDocument,
    PatientGroup,
    PatientGroupMembership,
    PatientRecord,
    PatientTrialEnrollment,
    PersonalRepresentative,
    Person,
    ProcedureOccurrence,
    Specimen,
    VisitDetail,
    VisitOccurrence,
)
from omop_oncology.models import Episode, EpisodeEvent
from patient_portal.models import Identity, PatientUser


def delete_organization_with_patient_cascade(org: Organization) -> None:
    """Delete an organization and all patient data owned by it.

    This is the canonical cleanup path for organization deletion. It removes:
    - PatientRecord rows owned by the organization
    - their linked Person rows
    - all OMOP rows that cascade from Person
    - the Organization row itself
    """
    with transaction.atomic():
        # Collect person IDs before any deletions so we can delete Person rows
        # after PatientRecord is gone (the FK constraint patient_record.person_id
        # prevents deleting Person while PatientRecord still references it).
        person_ids = list(
            PatientRecord.objects.filter(organization=org).values_list('person_id', flat=True)
        )

        patient_record_table = PatientRecord._meta.db_table
        person_subquery = (
            f"SELECT person_id FROM {patient_record_table} WHERE organization_id = %s"
        )

        statements = [
            (
                f"DELETE FROM {EpisodeEvent._meta.db_table} "
                f"WHERE episode_id IN (SELECT episode_id FROM {Episode._meta.db_table} "
                f"WHERE person_id IN ({person_subquery}))"
            ),
            (
                f"DELETE FROM {MeasurementOwnership._meta.db_table} "
                f"WHERE visit_occurrence_id IN (SELECT visit_occurrence_id FROM {VisitOccurrence._meta.db_table} "
                f"WHERE person_id IN ({person_subquery})) "
                f"OR measurement_id IN (SELECT measurement_id FROM {Measurement._meta.db_table} "
                f"WHERE person_id IN ({person_subquery}))"
            ),
            (
                f"DELETE FROM {Identity._meta.db_table} "
                f"WHERE id IN (SELECT identity_id FROM {PatientUser._meta.db_table} "
                f"WHERE person_id IN ({person_subquery}))"
            ),
            f"DELETE FROM {Episode._meta.db_table} WHERE person_id IN ({person_subquery})",
            f"DELETE FROM {ConditionOccurrence._meta.db_table} WHERE person_id IN ({person_subquery})",
            f"DELETE FROM {ConditionEra._meta.db_table} WHERE person_id IN ({person_subquery})",
            f"DELETE FROM {DrugExposure._meta.db_table} WHERE person_id IN ({person_subquery})",
            f"DELETE FROM {DrugEra._meta.db_table} WHERE person_id IN ({person_subquery})",
            f"DELETE FROM {DoseEra._meta.db_table} WHERE person_id IN ({person_subquery})",
            f"DELETE FROM {Observation._meta.db_table} WHERE person_id IN ({person_subquery})",
            f"DELETE FROM {ProcedureOccurrence._meta.db_table} WHERE person_id IN ({person_subquery})",
            f"DELETE FROM {Death._meta.db_table} WHERE person_id IN ({person_subquery})",
            f"DELETE FROM {Specimen._meta.db_table} WHERE person_id IN ({person_subquery})",
            (
                f"DELETE FROM {NoteNlp._meta.db_table} "
                f"WHERE note_id IN (SELECT note_id FROM {Note._meta.db_table} "
                f"WHERE person_id IN ({person_subquery}))"
            ),
            f"DELETE FROM {Note._meta.db_table} WHERE person_id IN ({person_subquery})",
            f"DELETE FROM {PatientDocument._meta.db_table} WHERE person_id IN ({person_subquery})",
            f"DELETE FROM {PatientTrialEnrollment._meta.db_table} WHERE person_id IN ({person_subquery})",
            f"DELETE FROM {PatientGroupMembership._meta.db_table} WHERE person_id IN ({person_subquery})",
            f"DELETE FROM {PersonalRepresentative._meta.db_table} WHERE person_id IN ({person_subquery})",
            f"DELETE FROM {Measurement._meta.db_table} WHERE person_id IN ({person_subquery})",
            f"DELETE FROM {VisitDetail._meta.db_table} WHERE person_id IN ({person_subquery})",
            f"DELETE FROM {VisitOccurrence._meta.db_table} WHERE person_id IN ({person_subquery})",
            f"DELETE FROM {PatientRecord._meta.db_table} WHERE organization_id = %s",
            f"DELETE FROM {PatientGroup._meta.db_table} WHERE organization_id = %s",
        ]

        with connection.cursor() as cursor:
            for sql in statements:
                if isinstance(sql, tuple):
                    sql = sql[0]
                cursor.execute(sql, [org.pk] * sql.count('%s'))

            # Delete Person rows using pre-collected IDs (subquery is now empty since
            # PatientRecord was deleted above; raw SQL bypasses Django's CASCADE).
            if person_ids:
                cursor.execute(
                    f"DELETE FROM {Person._meta.db_table} WHERE person_id = ANY(%s)",
                    [person_ids],
                )

        org.delete()
