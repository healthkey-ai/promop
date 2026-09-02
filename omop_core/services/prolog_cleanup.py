"""Removing a person's PROlog survey rows.

`prolog_surveys.SurveyResponse.participant` is `PROTECT`, so a `Person` who has
answered a survey cannot be deleted until their responses are. Three paths in
this project delete patients — organisation cleanup, `import_org_patients
--replace`, and the admin patient delete — and each of them hit that protection
independently. They share this module so the table list is maintained once: a
new PROlog table with a person FK is added here, not in three places.

The order matters and is not the model order: rows are removed leaf-first,
because organisation cleanup deletes `Person` with raw SQL, where Django's
`on_delete` does not apply and the database's own constraints are enforced.
"""

from django.db import connection

# Every PROlog table that ends up referencing a Person, leaf-first.
#
# Each entry is a DELETE with one `{persons}` placeholder for a subquery (or a
# list) yielding person ids. `surveyanswer` and `surveyconsent` hang off the
# response; `surveyadministration` off the invitation; the rest name the person
# directly.
_STATEMENTS = (
    "DELETE FROM prolog_surveys_surveyanswer WHERE response_id IN "
    "(SELECT id FROM prolog_surveys_surveyresponse WHERE participant_id IN ({persons}))",
    "DELETE FROM prolog_surveys_surveyconsent WHERE response_id IN "
    "(SELECT id FROM prolog_surveys_surveyresponse WHERE participant_id IN ({persons}))",
    "DELETE FROM prolog_surveys_surveyresponse WHERE participant_id IN ({persons})",
    # After the responses: a response points at an administration, not the
    # other way round.
    "DELETE FROM prolog_surveys_surveyadministration WHERE invitation_id IN "
    "(SELECT id FROM prolog_surveys_surveyinvitation WHERE participant_id IN ({persons}))",
    "DELETE FROM prolog_surveys_surveyinvitation WHERE participant_id IN ({persons})",
    "DELETE FROM prolog_surveys_participantmergecandidate "
    "WHERE minted_id IN ({persons}) OR existing_id IN ({persons})",
    "DELETE FROM prolog_surveys_mintedparticipant WHERE participant_id IN ({persons})",
)


def prolog_delete_statements(person_subquery: str) -> list:
    """The DELETEs that free `person_subquery`'s people from PROlog.

    `person_subquery` is interpolated, so it must be SQL this project built —
    never anything derived from a request. Its own placeholders are preserved,
    which is why the caller binds the parameters: a statement may repeat the
    subquery, so count the `%s` in the statement rather than assuming one.
    """
    return [sql.format(persons=person_subquery) for sql in _STATEMENTS]


def delete_prolog_data_for_persons(person_ids) -> None:
    """Delete the PROlog rows for these people. Safe to call when there are none."""
    ids = list(person_ids)
    if not ids:
        return
    with connection.cursor() as cursor:
        for sql in prolog_delete_statements('SELECT unnest(%s::bigint[])'):
            cursor.execute(sql, [ids] * sql.count('%s'))
