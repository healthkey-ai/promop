"""Per-user trial state for the federated trial-search UI (#1142).

Two things the UI needs and PROMOP is the right place to hold: which trials
a patient bookmarked, and the filters they last searched with. EXACT is
deliberately stateless about patients, and the remote runs in two different
hosts, so per-host storage would mean two implementations and no cross-device
consistency.
"""
import pytest
from rest_framework.test import APIClient

from omop_core.models import PatientTrialEnrollment, TrialSearchPreferences
from patient_portal.models import Identity, PatientUser
from tests.factories import PersonFactory


def rows(response):
    """The list payload, paginated or not.

    These endpoints answer with a bare list today; a future page wrapper
    should not silently turn every assertion below into a TypeError.
    """
    body = response.json()
    return body['results'] if isinstance(body, dict) else body

pytestmark = pytest.mark.django_db


@pytest.fixture
def person():
    return PersonFactory()


@pytest.fixture
def client(person):
    identity = Identity.objects.create_user(email='patient@example.test', password=None)
    PatientUser.objects.create(identity=identity, person=person)
    api = APIClient()
    api.force_authenticate(user=identity)
    return api


def enrollment(person, trial_id, **kwargs):
    return PatientTrialEnrollment.objects.create(
        person=person, trial_id=trial_id, **kwargs
    )


class TestFavorites:
    def test_bookmark_is_a_field_not_a_status(self, person):
        """A patient can bookmark a trial they are also registered for.

        This is why `is_favorite` is a boolean and not a sixth `status`:
        the statuses describe participation, and as a status the two could
        not be true at once.
        """
        row = enrollment(person, 'T1', status='registered', is_favorite=True)
        row.refresh_from_db()
        assert row.status == 'registered'
        assert row.is_favorite is True

    def test_defaults_to_not_bookmarked(self, person):
        assert enrollment(person, 'T1').is_favorite is False

    def test_filters_the_list(self, client, person):
        enrollment(person, 'T1', is_favorite=True)
        enrollment(person, 'T2', is_favorite=False)
        response = client.get(
            f'/api/v1/trial-enrollments/?person_id={person.pk}&is_favorite=true'
        )
        assert response.status_code == 200
        assert [r['trial_id'] for r in rows(response)] == ['T1']

    def test_the_negative_filter_is_not_the_same_as_no_filter(self, client, person):
        """`?is_favorite=false` must select the un-bookmarked rows, not every
        row. A truthiness-only implementation returns everything here."""
        enrollment(person, 'T1', is_favorite=True)
        enrollment(person, 'T2', is_favorite=False)
        response = client.get(
            f'/api/v1/trial-enrollments/?person_id={person.pk}&is_favorite=false'
        )
        assert [r['trial_id'] for r in rows(response)] == ['T2']

    def test_ids_returns_ids_and_a_count(self, client, person):
        """The list page needs the ids on every load — to light up the
        bookmark on each card and label the Favorites tab — and the ids are
        what EXACT filters by, since only its queryset can sort and paginate
        them alongside the match scores."""
        enrollment(person, 'T1', is_favorite=True)
        enrollment(person, 'T2', is_favorite=True)
        enrollment(person, 'T3', is_favorite=False)
        response = client.get(
            f'/api/v1/trial-enrollments/ids/?person_id={person.pk}&is_favorite=true'
        )
        assert response.status_code == 200
        body = response.json()
        assert sorted(body['trial_ids']) == ['T1', 'T2']
        assert body['count'] == 2

    def test_ids_is_scoped_to_the_person_asked_for(self, client, person):
        other = PersonFactory()
        enrollment(person, 'MINE', is_favorite=True)
        enrollment(other, 'THEIRS', is_favorite=True)
        response = client.get(
            f'/api/v1/trial-enrollments/ids/?person_id={person.pk}&is_favorite=true'
        )
        assert response.json()['trial_ids'] == ['MINE']

    def test_bookmark_survives_a_status_change(self, client, person):
        row = enrollment(person, 'T1', is_favorite=True)
        response = client.patch(
            f'/api/v1/trial-enrollments/{row.pk}/',
            {'status': 'registered'},
            format='json',
        )
        assert response.status_code == 200
        row.refresh_from_db()
        assert row.status == 'registered'
        assert row.is_favorite is True


class TestSearchPreferences:
    def url(self, person, suffix=''):
        return f'/api/v1/trial-search-preferences/{suffix}?person_id={person.pk}'

    def test_upsert_creates_the_row_on_first_use(self, client, person):
        """The first time a patient touches a filter is exactly when there is
        no row, so a plain PATCH would 404 and the client would have to
        POST-then-PATCH and handle two tabs racing."""
        response = client.patch(
            self.url(person, 'upsert/'),
            {'preferences': {'searchTitle': 'myeloma'}},
            format='json',
        )
        assert response.status_code == 200
        assert TrialSearchPreferences.objects.get(person=person).preferences == {
            'searchTitle': 'myeloma'
        }

    def test_upsert_is_idempotent(self, client, person):
        for _ in range(2):
            client.patch(
                self.url(person, 'upsert/'),
                {'preferences': {'searchTitle': 'myeloma'}},
                format='json',
            )
        assert TrialSearchPreferences.objects.filter(person=person).count() == 1

    def test_reset_clears_rather_than_merges(self, client, person):
        """Distinct from a PATCH of `{}` on `upsert`.

        Not because a partial update merges key by key — it does not, see
        `TestPreferencesAreReplacedNotMerged` — but because a body that
        omits `preferences` leaves the stored field untouched, so a PATCH
        of `{}` clears nothing and clearing needs an action of its own.
        """
        client.patch(
            self.url(person, 'upsert/'),
            {'preferences': {'searchTitle': 'myeloma', 'phase': 'PHASE3'}},
            format='json',
        )
        response = client.patch(self.url(person, 'reset/'), {}, format='json')
        assert response.status_code == 200
        assert response.json()['preferences'] == {}
        assert TrialSearchPreferences.objects.get(person=person).preferences == {}

    def test_requires_a_person(self, client):
        assert client.patch(
            '/api/v1/trial-search-preferences/upsert/',
            {'preferences': {}},
            format='json',
        ).status_code == 400
        assert client.patch(
            '/api/v1/trial-search-preferences/reset/', {}, format='json'
        ).status_code == 400


class TestPreferencesAreReplacedNotMerged:
    """What a PATCH of `preferences` does to the keys it does not mention.

    It removes them. `preferences` is one JSON column and the serializer
    has no custom `update`, so `partial=True` is field-level: a body that
    omits the field leaves it alone, and a body that carries it replaces
    the whole object. The endpoint is called `upsert` and takes a PATCH,
    which reads like a merge, and EXACT's client was built against that
    reading and lost saved filters by it (healthkey-ai/exact#444). These
    tests pin the contract so the next client does not have to guess it.
    """

    def url(self, person):
        return f'/api/v1/trial-search-preferences/upsert/?person_id={person.pk}'

    def test_a_second_write_replaces_the_first(self, client, person):
        """Two partial writes do not compose: the second wins outright."""
        first = client.patch(
            self.url(person), {'preferences': {'a': 1}}, format='json'
        )
        # Without this the claim is vacuous: had the first write been
        # refused, the second would still leave `{'b': 2}` and every
        # assertion below would pass against a merging server too.
        assert first.status_code == 200
        assert TrialSearchPreferences.objects.get(person=person).preferences == {
            'a': 1
        }
        response = client.patch(
            self.url(person), {'preferences': {'b': 2}}, format='json'
        )
        assert response.status_code == 200
        assert response.json()['preferences'] == {'b': 2}
        assert TrialSearchPreferences.objects.get(person=person).preferences == {
            'b': 2
        }

    def test_a_body_without_preferences_leaves_the_object_untouched(
        self, client, person
    ):
        """`partial=True` is field-level, and this is the half that merges.

        A PATCH of `{}` — which is what `upsert` called with no payload
        sends — leaves the stored object exactly as it was. This is why
        `reset` exists as a separate action rather than as this call.

        Scoped to the object deliberately: the call is not a no-op on the
        row. `updated_at` moves, and `get_or_create` will have created the
        row if there was none. See `test_an_empty_body_still_touches_the_row`.
        """
        seed = client.patch(
            self.url(person), {'preferences': {'searchTitle': 'myeloma'}}, format='json'
        )
        assert seed.status_code == 200
        response = client.patch(self.url(person), {}, format='json')
        assert response.status_code == 200
        assert TrialSearchPreferences.objects.get(person=person).preferences == {
            'searchTitle': 'myeloma'
        }

    def test_an_empty_body_still_touches_the_row(self, client, person):
        """The two side effects that survive an otherwise-empty PATCH.

        A client reading `updated_at` to decide whether anything changed
        would be misled, and a client that PATCHes `{}` to probe for a row
        creates the row by asking. Both follow from `get_or_create` running
        before the serializer, which is deliberate — see `upsert` — but
        neither is visible from "a body without `preferences` is a no-op".
        """
        assert not TrialSearchPreferences.objects.filter(person=person).exists()
        response = client.patch(self.url(person), {}, format='json')
        assert response.status_code == 200
        row = TrialSearchPreferences.objects.get(person=person)
        assert row.preferences == {}

        before = row.updated_at
        assert client.patch(self.url(person), {}, format='json').status_code == 200
        row.refresh_from_db()
        assert row.updated_at > before

    def test_an_empty_object_clears_the_row(self, client, person):
        """The distinction clients get wrong, stated as a test.

        An omitted field leaves the object alone; a field carrying `{}`
        replaces it with `{}`. Were the server merging, this call would
        leave every existing key in place.
        """
        seed = client.patch(
            self.url(person),
            {'preferences': {'searchTitle': 'myeloma', 'phase': 'PHASE3'}},
            format='json',
        )
        # Else a refused seed leaves `get_or_create`'s empty row and the
        # assertion below passes without the clear ever happening.
        assert seed.status_code == 200
        assert TrialSearchPreferences.objects.get(person=person).preferences != {}
        response = client.patch(self.url(person), {'preferences': {}}, format='json')
        assert response.status_code == 200
        assert response.json()['preferences'] == {}
        assert TrialSearchPreferences.objects.get(person=person).preferences == {}

    def test_a_nested_null_is_a_value_not_a_delete_sentinel(self, client, person):
        """There is no way to spell "delete this key" — absence is how.

        A client migrating from a merge-shaped API may reach for an
        explicit `null` to retire a filter. Under a replace it is stored as
        the value `null`, which `non_default_filter_count` then reads as
        unset. Removal is spelled by leaving the key out of the body.
        """
        seed = client.patch(
            self.url(person),
            {'preferences': {'sponsor': 'Acme', 'phase': 'PHASE3'}},
            format='json',
        )
        assert seed.status_code == 200
        response = client.patch(
            self.url(person), {'preferences': {'sponsor': None}}, format='json'
        )
        assert response.status_code == 200
        # `sponsor` kept as an explicit null rather than dropped, and
        # `phase` gone because it was absent — one call showing both rules.
        row = TrialSearchPreferences.objects.get(person=person)
        assert row.preferences == {'sponsor': None}
        # And the null reads as unset downstream, so a client reaching for
        # it does get the badge it wanted, just not the deletion.
        assert row.non_default_filter_count == 0

    def test_the_detail_route_replaces_the_same_way(self, client, person):
        """The third write path, which the contract has to cover too.

        `http_method_names` allows PATCH, so the ModelViewSet's own detail
        route is live alongside the two actions — same serializer, so the
        same wholesale replace. Worth pinning because a reader looking for
        "how do I write this" finds `upsert` and may never notice that the
        plain route exists and behaves identically.
        """
        seed = client.patch(
            self.url(person),
            {'preferences': {'searchTitle': 'myeloma', 'phase': 'PHASE3'}},
            format='json',
        )
        assert seed.status_code == 200
        row = TrialSearchPreferences.objects.get(person=person)
        response = client.patch(
            f'/api/v1/trial-search-preferences/{row.pk}/',
            {'preferences': {'sponsor': 'Acme'}},
            format='json',
        )
        assert response.status_code == 200
        row.refresh_from_db()
        assert row.preferences == {'sponsor': 'Acme'}

    def test_a_write_after_a_reset_holds_only_the_new_keys(self, client, person):
        """The cleared state, pinned rather than inferred.

        Asked for by #1201. It does not discriminate replace from merge —
        after a reset the row is `{}` and merging into `{}` looks the same
        — but it is the sequence a reader actually performs (Reset, then
        set one filter), and nothing pinned where it lands.
        """
        seed = client.patch(
            self.url(person),
            {'preferences': {'searchTitle': 'myeloma', 'phase': 'PHASE3'}},
            format='json',
        )
        assert seed.status_code == 200
        reset = client.patch(
            f'/api/v1/trial-search-preferences/reset/?person_id={person.pk}',
            {},
            format='json',
        )
        assert reset.status_code == 200
        response = client.patch(
            self.url(person), {'preferences': {'distance': 50}}, format='json'
        )
        assert response.status_code == 200
        assert TrialSearchPreferences.objects.get(person=person).preferences == {
            'distance': 50
        }

    def test_a_top_level_null_is_refused(self, client, person):
        """`{"preferences": null}` is not "clear it" — the column is not nullable.

        Worth pinning next to the nested case: the two nulls are one
        keystroke apart and mean different things. Clearing is `{}` or the
        `reset` action.
        """
        response = client.patch(
            self.url(person), {'preferences': None}, format='json'
        )
        assert response.status_code == 400
        # Named, because this route answers 400 for a missing `person_id`
        # too and a bare status code cannot tell the two apart — nor tell
        # that `allow_null=False` is still what refuses this.
        assert 'preferences' in response.json()


class TestNonDefaultFilterCount:
    """The number the UI paints on its Filters button.

    Counted server-side so every client agrees; a count computed in the
    browser drifts from what the server would say.
    """

    @pytest.mark.parametrize(
        'preferences,expected',
        [
            ({}, 0),
            ({'searchTitle': 'myeloma'}, 1),
            ({'searchTitle': 'myeloma', 'phase': 'PHASE3'}, 2),
            # A cleared text input hands back "" before anything normalizes
            # it, and an unticked checkbox is False. Neither is a filter.
            ({'searchTitle': '', 'validatedOnly': False, 'sponsor': None}, 0),
            # EXACT gates on `if study_info.distance:`, so zero applies no
            # limit at all — counting it would claim a narrowing that is not
            # running.
            ({'distance': 0}, 0),
            ({'distance': 50}, 1),
            # The tab and the sort control are not filters; counting them
            # would tick the badge up when the reader switches tab.
            ({'type': 'potential', 'sort': 'distance'}, 0),
            ({'type': 'potential', 'searchTitle': 'x'}, 1),
        ],
    )
    def test_counts_only_what_the_patient_set(self, person, preferences, expected):
        prefs = TrialSearchPreferences.objects.create(
            person=person, preferences=preferences
        )
        assert prefs.non_default_filter_count == expected

    def test_is_served_with_the_row(self, client, person):
        client.patch(
            f'/api/v1/trial-search-preferences/upsert/?person_id={person.pk}',
            {'preferences': {'searchTitle': 'myeloma', 'sort': 'distance'}},
            format='json',
        )
        response = client.get(
            f'/api/v1/trial-search-preferences/?person_id={person.pk}'
        )
        assert response.status_code == 200
        row = rows(response)[0]
        assert row['non_default_filter_count'] == 1


class TestPersonIdCoercion:
    def test_a_person_id_from_the_query_string_is_the_same_person(self, client, person):
        """It always arrives as a string over HTTP.

        `get_or_create(person_id='5')` leaves the string on the in-memory
        instance, and the object-level ownership check then compares `'5'`
        with the integer `5`, decides the row is someone else's, and 403s
        the patient out of their own preferences.
        """
        response = client.patch(
            f'/api/v1/trial-search-preferences/upsert/?person_id={person.pk}',
            {'preferences': {'searchTitle': 'x'}},
            format='json',
        )
        assert response.status_code == 200

    def test_a_non_numeric_person_id_is_a_400_not_a_500(self, client):
        response = client.patch(
            '/api/v1/trial-search-preferences/upsert/?person_id=abc',
            {'preferences': {}},
            format='json',
        )
        assert response.status_code == 400


class TestFavoriteToggle:
    """Bookmarking a trial the patient never registered for.

    The obvious route — POST a new enrollment — is closed to the person who
    needs it: `ScopedTokenPermission` grants a session-authenticated patient
    safe methods and PATCH only. Without a PATCH action, a patient could
    toggle a bookmark only on a trial somebody else had enrolled them in.
    """

    def url(self, person, trial_id):
        return (
            f'/api/v1/trial-enrollments/upsert/'
            f'?person_id={person.pk}&trial_id={trial_id}'
        )

    def test_creates_the_row_for_a_trial_with_no_enrollment(self, client, person):
        response = client.patch(self.url(person, 'T9'), {'is_favorite': True}, format='json')
        assert response.status_code == 200
        row = PatientTrialEnrollment.objects.get(person=person, trial_id='T9')
        assert row.is_favorite is True
        # A bookmark is not participation; the row carries the default status.
        assert row.status == 'interested'

    def test_unbookmarks_without_deleting_the_enrollment(self, client, person):
        enrollment(person, 'T1', status='registered', is_favorite=True)
        response = client.patch(self.url(person, 'T1'), {'is_favorite': False}, format='json')
        assert response.status_code == 200
        row = PatientTrialEnrollment.objects.get(person=person, trial_id='T1')
        assert row.is_favorite is False
        assert row.status == 'registered'

    def test_is_idempotent(self, client, person):
        for _ in range(2):
            client.patch(self.url(person, 'T1'), {'is_favorite': True}, format='json')
        assert PatientTrialEnrollment.objects.filter(person=person, trial_id='T1').count() == 1

    def test_rejects_a_non_boolean(self, client, person):
        response = client.patch(self.url(person, 'T1'), {'is_favorite': 'yes'}, format='json')
        assert response.status_code == 400


class TestOwnership:
    """Writes must be authorized BEFORE they happen."""

    @pytest.fixture
    def other(self):
        return PersonFactory()

    def test_cannot_bookmark_for_another_person(self, client, person, other):
        response = client.patch(
            f'/api/v1/trial-enrollments/upsert/?person_id={other.pk}&trial_id=T1',
            {'is_favorite': True},
            format='json',
        )
        assert response.status_code == 403
        assert not PatientTrialEnrollment.objects.filter(person=other).exists()

    def test_cannot_create_another_persons_preferences_row(self, client, person, other):
        """`get_or_create` before the ownership check let any authenticated
        patient persist a row for every person in the system — each call
        refused afterwards, every row created."""
        response = client.patch(
            f'/api/v1/trial-search-preferences/upsert/?person_id={other.pk}',
            {'preferences': {'searchTitle': 'x'}},
            format='json',
        )
        assert response.status_code == 403
        assert not TrialSearchPreferences.objects.filter(person=other).exists()

    def test_reset_cannot_create_another_persons_row_either(self, client, person, other):
        response = client.patch(
            f'/api/v1/trial-search-preferences/reset/?person_id={other.pk}',
            {},
            format='json',
        )
        assert response.status_code == 403
        assert not TrialSearchPreferences.objects.filter(person=other).exists()


class TestPreferencesShape:
    def test_a_non_object_payload_is_rejected(self, client, person):
        """A JSONField accepts a list or a bare string. Stored, it would 500
        every later read of that row through `non_default_filter_count`, not
        just the write."""
        for payload in ([], 'nope', 42):
            response = client.patch(
                f'/api/v1/trial-search-preferences/upsert/?person_id={person.pk}',
                {'preferences': payload},
                format='json',
            )
            assert response.status_code == 400, f'{payload!r} was accepted'

    def test_a_legacy_non_object_row_does_not_raise(self, person):
        prefs = TrialSearchPreferences.objects.create(person=person, preferences=[])
        assert prefs.non_default_filter_count == 0


class TestNonPatientCallers:
    """An identity with no `PatientUser` link must not write a patient's row.

    `PatientSelfScopePermission` answers "is this row mine?" and returns True
    for any identity that is not a patient at all — a provider account, an
    org token, an OAuth client-credentials caller — so its own class
    docstring says a mutating view must add a person-level check of its own.
    These endpoints do (`_deny_unless_may_write_person`).

    Honest note on coverage: disabling that gate does NOT make these tests
    fail — something else in the stack refuses the same calls, and a review
    that measured a 200 here could not be reproduced afterwards. So they
    pin the OUTCOME, which is what matters, but they do not isolate the
    gate, and nobody should read them as proof that it is load-bearing.
    """

    @pytest.fixture
    def non_patient(self):
        identity = Identity.objects.create_user(email='provider@example.test', password=None)
        api = APIClient()
        api.force_authenticate(user=identity)
        return api

    def test_cannot_write_preferences_for_a_patient(self, non_patient, person):
        response = non_patient.patch(
            f'/api/v1/trial-search-preferences/upsert/?person_id={person.pk}',
            {'preferences': {'searchTitle': 'x'}},
            format='json',
        )
        assert response.status_code == 403
        assert not TrialSearchPreferences.objects.filter(person=person).exists()

    def test_cannot_reset_a_patients_preferences(self, non_patient, person):
        TrialSearchPreferences.objects.create(
            person=person, preferences={'searchTitle': 'mine'}
        )
        response = non_patient.patch(
            f'/api/v1/trial-search-preferences/reset/?person_id={person.pk}',
            {},
            format='json',
        )
        assert response.status_code == 403
        assert TrialSearchPreferences.objects.get(person=person).preferences == {
            'searchTitle': 'mine'
        }

    def test_cannot_bookmark_for_a_patient(self, non_patient, person):
        response = non_patient.patch(
            f'/api/v1/trial-enrollments/upsert/?person_id={person.pk}&trial_id=T1',
            {'is_favorite': True},
            format='json',
        )
        assert response.status_code == 403
        assert not PatientTrialEnrollment.objects.filter(person=person).exists()


class TestEnrollmentCannotBeMoved:
    def test_a_patient_cannot_reparent_their_row(self, client, person):
        """Object permission inspects the row BEFORE the update, so a PATCH
        of `person` on a row that is legitimately mine passes every check —
        and plants the enrollment, with its status and notes, on someone
        else's chart."""
        other = PersonFactory()
        row = enrollment(person, 'T1')
        response = client.patch(
            f'/api/v1/trial-enrollments/{row.pk}/',
            {'person': other.pk},
            format='json',
        )
        assert response.status_code == 400
        row.refresh_from_db()
        assert row.person_id == person.pk


class TestIsFavoriteParsing:
    def test_an_empty_value_is_no_filter(self, client, person):
        """What a UI that always sends the key emits. Coerced to False it
        silently hid every bookmark."""
        enrollment(person, 'T1', is_favorite=True)
        enrollment(person, 'T2', is_favorite=False)
        response = client.get(
            f'/api/v1/trial-enrollments/?person_id={person.pk}&is_favorite='
        )
        assert sorted(r['trial_id'] for r in rows(response)) == ['T1', 'T2']

    def test_junk_is_refused_rather_than_read_as_false(self, client, person):
        enrollment(person, 'T1', is_favorite=True)
        response = client.get(
            f'/api/v1/trial-enrollments/?person_id={person.pk}&is_favorite=garbage'
        )
        assert response.status_code == 400

    @pytest.mark.parametrize('value,expected', [
        ('true', ['T1']), ('True', ['T1']), ('1', ['T1']), ('yes', ['T1']),
        ('false', ['T2']), ('False', ['T2']), ('0', ['T2']), ('no', ['T2']),
    ])
    def test_accepted_spellings(self, client, person, value, expected):
        enrollment(person, 'T1', is_favorite=True)
        enrollment(person, 'T2', is_favorite=False)
        response = client.get(
            f'/api/v1/trial-enrollments/?person_id={person.pk}&is_favorite={value}'
        )
        assert [r['trial_id'] for r in rows(response)] == expected


class TestRegisteredInterest:
    """Registering interest is a database write and nothing else.

    No email, no notification to a coordinator — deliberately. If that
    changes it should be a separately reviewed addition, not a side effect
    of a PATCH nobody looked at again.
    """

    def url(self, person, trial_id):
        return (
            f'/api/v1/trial-enrollments/upsert/'
            f'?person_id={person.pk}&trial_id={trial_id}'
        )

    def test_registers_a_trial_with_no_prior_row(self, client, person):
        response = client.patch(self.url(person, 'T7'), {'status': 'registered'}, format='json')
        assert response.status_code == 200
        row = PatientTrialEnrollment.objects.get(person=person, trial_id='T7')
        assert row.status == 'registered'
        assert row.is_favorite is False

    def test_status_and_bookmark_are_independent(self, client, person):
        client.patch(self.url(person, 'T1'), {'is_favorite': True}, format='json')
        client.patch(self.url(person, 'T1'), {'status': 'registered'}, format='json')
        row = PatientTrialEnrollment.objects.get(person=person, trial_id='T1')
        assert row.is_favorite is True
        assert row.status == 'registered'

    def test_omitting_a_field_leaves_it_alone(self, client, person):
        enrollment(person, 'T1', status='registered', is_favorite=True)
        client.patch(self.url(person, 'T1'), {'is_favorite': False}, format='json')
        row = PatientTrialEnrollment.objects.get(person=person, trial_id='T1')
        assert row.is_favorite is False
        assert row.status == 'registered'

    def test_an_unknown_status_is_refused(self, client, person):
        response = client.patch(self.url(person, 'T1'), {'status': 'enrolled'}, format='json')
        assert response.status_code == 400
        assert not PatientTrialEnrollment.objects.filter(person=person).exists()

    def test_an_empty_body_is_refused(self, client, person):
        """Neither field sent is a request that means nothing. Creating an
        empty row for it would leave a record nobody asked for."""
        response = client.patch(self.url(person, 'T1'), {}, format='json')
        assert response.status_code == 400
        assert not PatientTrialEnrollment.objects.filter(person=person).exists()

    def test_cannot_register_for_another_person(self, client, person):
        other = PersonFactory()
        response = client.patch(
            f'/api/v1/trial-enrollments/upsert/?person_id={other.pk}&trial_id=T1',
            {'status': 'registered'},
            format='json',
        )
        assert response.status_code == 403
        assert not PatientTrialEnrollment.objects.filter(person=other).exists()

    def test_the_registered_ids_list(self, client, person):
        enrollment(person, 'T1', status='registered')
        enrollment(person, 'T2', status='interested')
        response = client.get(
            f'/api/v1/trial-enrollments/ids/?person_id={person.pk}&status=registered'
        )
        assert response.json() == {'trial_ids': ['T1'], 'count': 1}

    def test_an_unknown_status_filter_is_refused_not_answered_empty(self, client, person):
        """An empty list would read as "you have registered for none", which
        is a different statement from "that is not a status"."""
        enrollment(person, 'T1', status='registered')
        response = client.get(
            f'/api/v1/trial-enrollments/ids/?person_id={person.pk}&status=enrolled'
        )
        assert response.status_code == 400

    def test_the_two_filters_compose(self, client, person):
        enrollment(person, 'BOTH', status='registered', is_favorite=True)
        enrollment(person, 'REG', status='registered', is_favorite=False)
        enrollment(person, 'FAV', status='interested', is_favorite=True)
        response = client.get(
            f'/api/v1/trial-enrollments/ids/'
            f'?person_id={person.pk}&status=registered&is_favorite=true'
        )
        assert response.json()['trial_ids'] == ['BOTH']
