"""Per-user trial state for the federated trial-search UI (#1142).

Two things the UI needs and PROMOP is the right place to hold: which trials
a patient bookmarked, and the filters they last searched with. EXACT is
deliberately stateless about patients, and the remote runs in two different
hosts, so per-host storage would mean two implementations and no cross-device
consistency.
"""
import threading

import pytest
from django.db import connection, connections
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
        keystroke apart and mean different things. Clearing is `{"preferences": {}}` or the
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


class TestIfMatchPrecondition:
    """Opting out of last-writer-wins (#1312).

    Replacing the whole object obliges a client to read-modify-write, and a
    read-modify-write with no precondition is a lost update: two writers
    each read the same set, each merge their own edit over it, and the
    second erases the first one's key with nobody seeing an error. One
    client implementation is not one writer — two tabs instantiate it
    twice.

    `If-Match` is opt-in: absent, the write is unconditional, because every
    client written before this sent no header and must keep working.
    """

    def url(self, person, action='upsert'):
        return f'/api/v1/trial-search-preferences/{action}/?person_id={person.pk}'

    def seed(self, client, person, preferences):
        """Write an initial set and hand back the row's current ETag."""
        response = client.patch(
            self.url(person), {'preferences': preferences}, format='json'
        )
        assert response.status_code == 200
        assert response['ETag']
        return response['ETag']

    def test_a_matching_etag_lets_the_write_through(self, client, person):
        etag = self.seed(client, person, {'searchTitle': 'myeloma'})
        response = client.patch(
            self.url(person),
            {'preferences': {'searchTitle': 'myeloma', 'phase': 'PHASE3'}},
            format='json',
            HTTP_IF_MATCH=etag,
        )
        assert response.status_code == 200
        assert TrialSearchPreferences.objects.get(person=person).preferences == {
            'searchTitle': 'myeloma',
            'phase': 'PHASE3',
        }

    def test_a_stale_etag_is_refused_and_writes_nothing(self, client, person):
        """The lost update, as the two tabs actually produce it.

        Tab A reads, tab B writes, then tab A saves what it was holding.
        Without the header A's stale set replaces B's; with it, A is told.
        """
        stale = self.seed(client, person, {'searchTitle': 'myeloma'})
        other_tab = client.patch(
            self.url(person), {'preferences': {'sponsor': 'Acme'}}, format='json'
        )
        assert other_tab.status_code == 200

        response = client.patch(
            self.url(person),
            {'preferences': {'searchTitle': 'myeloma', 'phase': 'PHASE3'}},
            format='json',
            HTTP_IF_MATCH=stale,
        )
        assert response.status_code == 412
        # The refusal has to be total: a 412 that still wrote would be the
        # lost update wearing an error code.
        assert TrialSearchPreferences.objects.get(person=person).preferences == {
            'sponsor': 'Acme'
        }

    def test_a_refusal_carries_the_current_etag_for_the_retry(self, client, person):
        """So re-read-and-retry does not need a round trip to learn it."""
        stale = self.seed(client, person, {'searchTitle': 'myeloma'})
        current = self.seed(client, person, {'sponsor': 'Acme'})

        refused = client.patch(
            self.url(person), {'preferences': {'phase': 'PHASE3'}},
            format='json', HTTP_IF_MATCH=stale,
        )
        assert refused.status_code == 412
        assert refused['ETag'] == current
        assert refused.json()['etag'] == current

        retried = client.patch(
            self.url(person), {'preferences': {'phase': 'PHASE3'}},
            format='json', HTTP_IF_MATCH=refused['ETag'],
        )
        assert retried.status_code == 200

    def test_no_header_still_writes_unconditionally(self, client, person):
        """The half that must not change: every client today sends none."""
        self.seed(client, person, {'searchTitle': 'myeloma'})
        response = client.patch(
            self.url(person), {'preferences': {'sponsor': 'Acme'}}, format='json'
        )
        assert response.status_code == 200
        assert TrialSearchPreferences.objects.get(person=person).preferences == {
            'sponsor': 'Acme'
        }

    def test_the_etag_is_the_updated_at_a_list_read_hands_back(self, client, person):
        """The whole reason the ETag is not a hash.

        Clients read this endpoint as a list, and a list response carries
        no ETag header. If the value could not be rebuilt from the body,
        every writer would have to switch to a detail read first.
        """
        self.seed(client, person, {'searchTitle': 'myeloma'})
        listed = client.get(
            f'/api/v1/trial-search-preferences/?person_id={person.pk}'
        )
        assert listed.status_code == 200
        row = rows(listed)[0]
        built = '"{}"'.format(row['updated_at'])

        response = client.patch(
            self.url(person), {'preferences': {'phase': 'PHASE3'}},
            format='json', HTTP_IF_MATCH=built,
        )
        assert response.status_code == 200

    def test_a_detail_read_carries_the_etag_as_a_header(self, client, person):
        self.seed(client, person, {'searchTitle': 'myeloma'})
        row = TrialSearchPreferences.objects.get(person=person)
        response = client.get(f'/api/v1/trial-search-preferences/{row.pk}/')
        assert response.status_code == 200
        assert response['ETag'] == self._etag_of(row)

    @staticmethod
    def _etag_of(row):
        from rest_framework import serializers as drf

        row.refresh_from_db()
        return '"{}"'.format(drf.DateTimeField().to_representation(row.updated_at))

    def test_a_star_requires_a_row_and_does_not_create_one(self, client, person):
        """`If-Match: *` asks "provided there is a row" — there is not.

        The order matters here: were the row created first, the `*` would
        be satisfied by the row this very request produced, which would
        tell a client its stale view was current. So a refused precondition
        must also leave no row behind.
        """
        response = client.patch(
            self.url(person), {'preferences': {'phase': 'PHASE3'}},
            format='json', HTTP_IF_MATCH='*',
        )
        assert response.status_code == 412
        assert not TrialSearchPreferences.objects.filter(person=person).exists()

    def test_a_star_matches_once_a_row_exists(self, client, person):
        self.seed(client, person, {'searchTitle': 'myeloma'})
        response = client.patch(
            self.url(person), {'preferences': {'phase': 'PHASE3'}},
            format='json', HTTP_IF_MATCH='*',
        )
        assert response.status_code == 200

    def test_a_weak_validator_never_satisfies_if_match(self, client, person):
        """RFC 9110 §13.1.1: `If-Match` takes the STRONG comparison.

        This module's other matcher, `_etag_matches`, strips `W/` before
        comparing because `If-None-Match` on a GET is allowed to. Reusing
        it here would let a weak validator authorize a write.
        """
        etag = self.seed(client, person, {'searchTitle': 'myeloma'})
        response = client.patch(
            self.url(person), {'preferences': {'phase': 'PHASE3'}},
            format='json', HTTP_IF_MATCH='W/' + etag,
        )
        assert response.status_code == 412

    def test_one_of_several_offered_etags_is_enough(self, client, person):
        """A client that holds two candidate versions may offer both."""
        stale = self.seed(client, person, {'searchTitle': 'myeloma'})
        current = self.seed(client, person, {'sponsor': 'Acme'})
        response = client.patch(
            self.url(person), {'preferences': {'phase': 'PHASE3'}},
            format='json', HTTP_IF_MATCH=f'{stale}, {current}',
        )
        assert response.status_code == 200

    def test_an_unparseable_header_is_refused_not_ignored(self, client, person):
        """A client that asked for a precondition gets one, or an error.

        Treating a header we cannot read as "no header" would silently
        downgrade a conditional write to an unconditional one — the exact
        outcome the client used the header to avoid.
        """
        self.seed(client, person, {'searchTitle': 'myeloma'})
        response = client.patch(
            self.url(person), {'preferences': {'phase': 'PHASE3'}},
            format='json', HTTP_IF_MATCH='not-a-valid-etag',
        )
        assert response.status_code == 412

    def test_reset_honours_the_precondition_too(self, client, person):
        stale = self.seed(client, person, {'searchTitle': 'myeloma'})
        assert self.seed(client, person, {'sponsor': 'Acme'})
        refused = client.patch(
            self.url(person, 'reset'), {}, format='json', HTTP_IF_MATCH=stale
        )
        assert refused.status_code == 412
        assert TrialSearchPreferences.objects.get(person=person).preferences == {
            'sponsor': 'Acme'
        }

    def test_the_detail_route_honours_the_precondition_too(self, client, person):
        """Else a second writer routes around the guarantee entirely."""
        stale = self.seed(client, person, {'searchTitle': 'myeloma'})
        assert self.seed(client, person, {'sponsor': 'Acme'})
        row = TrialSearchPreferences.objects.get(person=person)
        refused = client.patch(
            f'/api/v1/trial-search-preferences/{row.pk}/',
            {'preferences': {'phase': 'PHASE3'}},
            format='json',
            HTTP_IF_MATCH=stale,
        )
        assert refused.status_code == 412
        assert TrialSearchPreferences.objects.get(person=person).preferences == {
            'sponsor': 'Acme'
        }


class TestIfMatchGapsAndFirstWrite:
    """The cases the first cut of #1312 left unpinned, and the first write.

    Split from `TestIfMatchPrecondition` only to keep that class about the
    refusals; these are the acceptances, the no-row shapes, and
    `If-None-Match`.
    """

    def url(self, person, action='upsert'):
        return f'/api/v1/trial-search-preferences/{action}/?person_id={person.pk}'

    def seed(self, client, person, preferences):
        response = client.patch(
            self.url(person), {'preferences': preferences}, format='json'
        )
        assert response.status_code == 200
        return response['ETag']

    def test_a_successful_write_returns_a_different_etag(self, client, person):
        """Else a client would retry with the one it just spent."""
        first = self.seed(client, person, {'searchTitle': 'myeloma'})
        second = client.patch(
            self.url(person), {'preferences': {'phase': 'PHASE3'}},
            format='json', HTTP_IF_MATCH=first,
        )
        assert second.status_code == 200
        assert second['ETag'] != first
        assert second['ETag'] == '"{}"'.format(second.json()['updated_at'])

    def test_a_specific_etag_against_no_row_has_no_etag_to_return(
        self, client, person
    ):
        """The 412 shape a client is likeliest to crash on.

        There is no row, so there is no entity-tag — the header is absent
        and the body's `etag` is null. A generated client that reads
        `response.headers['ETag']` on every 412 breaks here, which is why
        the schema's 412 description calls this case out separately.
        """
        response = client.patch(
            self.url(person), {'preferences': {'phase': 'PHASE3'}},
            format='json', HTTP_IF_MATCH='"2026-01-01T00:00:00Z"',
        )
        assert response.status_code == 412
        assert 'ETag' not in response
        assert response.json()['etag'] is None
        assert not TrialSearchPreferences.objects.filter(person=person).exists()

    def test_reset_accepts_a_matching_etag(self, client, person):
        etag = self.seed(client, person, {'searchTitle': 'myeloma'})
        response = client.patch(
            self.url(person, 'reset'), {}, format='json', HTTP_IF_MATCH=etag
        )
        assert response.status_code == 200
        assert TrialSearchPreferences.objects.get(person=person).preferences == {}
        assert response['ETag'] != etag

    def test_the_detail_route_accepts_a_matching_etag(self, client, person):
        etag = self.seed(client, person, {'searchTitle': 'myeloma'})
        row = TrialSearchPreferences.objects.get(person=person)
        response = client.patch(
            f'/api/v1/trial-search-preferences/{row.pk}/',
            {'preferences': {'sponsor': 'Acme'}},
            format='json',
            HTTP_IF_MATCH=etag,
        )
        assert response.status_code == 200
        row.refresh_from_db()
        assert row.preferences == {'sponsor': 'Acme'}

    def test_a_star_among_other_tokens_is_not_a_wildcard(self, client, person):
        """RFC 9110: `If-Match = "*" / #entity-tag` — `*` is not a list member.

        Honouring it inside a list would turn a header that is malformed
        into one that writes, which is the dangerous direction. Refusing it
        in the wrong PLACE is the other one: decided at comparison time
        instead of as a syntax error, `"<current>", *` reaches the "row
        changed" answer, so the 412 tells a client whose row did not change
        that it did — and hands back the client's own tag as proof, which
        makes re-read-and-retry loop forever.
        """
        etag = self.seed(client, person, {'searchTitle': 'myeloma'})
        for header in ('"nope", *', f'{etag}, *', f'*, {etag}', '*, *'):
            response = client.patch(
                self.url(person), {'preferences': {'phase': 'PHASE3'}},
                format='json', HTTP_IF_MATCH=header,
            )
            assert response.status_code == 412, header
            assert 'could not be read' in response.json()['error'], header
        assert TrialSearchPreferences.objects.get(person=person).preferences == {
            'searchTitle': 'myeloma'
        }

    def test_the_detail_id_is_in_the_payload_a_client_reads(self, client, person):
        """Without it the detail routes are documented but unreachable.

        The read every client uses is the list; if the id is not in it,
        there is no way to learn the pk that `/{id}/` needs.
        """
        self.seed(client, person, {'searchTitle': 'myeloma'})
        listed = client.get(
            f'/api/v1/trial-search-preferences/?person_id={person.pk}'
        )
        row = rows(listed)[0]
        assert 'id' in row
        assert client.get(
            f'/api/v1/trial-search-preferences/{row["id"]}/'
        ).status_code == 200

    def test_a_rejected_first_write_leaves_no_row_to_block_the_retry(
        self, client, person
    ):
        """A 400 is not a write, and must not act like one.

        `get_or_create` runs before the payload is validated, so without a
        transaction a rejected body committed an empty row — and every
        later `If-None-Match: *`, from this tab or any other, then 412'd
        forever against a row nobody meant to create. Same invariant the
        `If-Match: *` path states: a refused precondition must not be the
        thing that makes itself true next time.
        """
        rejected = client.patch(
            self.url(person), {'preferences': 'not-an-object'},
            format='json', HTTP_IF_NONE_MATCH='*',
        )
        assert rejected.status_code == 400
        assert not TrialSearchPreferences.objects.filter(person=person).exists()

        retried = client.patch(
            self.url(person), {'preferences': {'searchTitle': 'myeloma'}},
            format='json', HTTP_IF_NONE_MATCH='*',
        )
        assert retried.status_code == 200

    def test_if_match_is_evaluated_before_if_none_match(self, client, person):
        """RFC 9110 §13.2.2 order, which the first cut had inverted.

        A request carrying both, aimed at a person with no row, had its
        `If-Match` discarded and CREATED the row its `If-Match` said must
        already exist — a must-refuse turned into a write.
        """
        response = client.patch(
            self.url(person), {'preferences': {'phase': 'PHASE3'}},
            format='json',
            HTTP_IF_MATCH='"2020-01-01T00:00:00Z"',
            HTTP_IF_NONE_MATCH='*',
        )
        assert response.status_code == 412
        assert not TrialSearchPreferences.objects.filter(person=person).exists()

    def test_an_if_none_match_this_endpoint_cannot_honour_is_refused(
        self, client, person
    ):
        """Not ignored — ignored means an unconditional write in disguise.

        A client that asked for a condition and silently got none has no
        way to tell. Refusing is the same call the `If-Match` path makes
        for a header it cannot read.
        """
        etag = self.seed(client, person, {'searchTitle': 'myeloma'})
        # A list of entity-tags is the caching idiom; on a write it would
        # mean something this endpoint does not implement.
        response = client.patch(
            self.url(person), {'preferences': {'phase': 'PHASE3'}},
            format='json', HTTP_IF_NONE_MATCH=etag,
        )
        assert response.status_code == 412
        assert TrialSearchPreferences.objects.get(person=person).preferences == {
            'searchTitle': 'myeloma'
        }

    def test_if_none_match_is_refused_on_reset_and_on_the_detail_route(
        self, client, person
    ):
        """`*` is honoured on `upsert` only, and the others say so."""
        self.seed(client, person, {'searchTitle': 'myeloma'})
        row = TrialSearchPreferences.objects.get(person=person)
        assert client.patch(
            self.url(person, 'reset'), {}, format='json', HTTP_IF_NONE_MATCH='*'
        ).status_code == 412
        assert client.patch(
            f'/api/v1/trial-search-preferences/{row.pk}/',
            {'preferences': {'sponsor': 'Acme'}},
            format='json', HTTP_IF_NONE_MATCH='*',
        ).status_code == 412
        # Refused means nothing happened.
        assert TrialSearchPreferences.objects.get(person=person).preferences == {
            'searchTitle': 'myeloma'
        }

    def test_a_good_tag_beside_a_malformed_one_does_not_authorize(
        self, client, person
    ):
        """One bad member condemns the header.

        Accepting because something in the list matched would write for a
        request we only half understood, and would contradict the
        documented refusal of an unreadable header. `"bogus"` is a
        well-formed tag that merely does not match, and is still fine; a
        trailing comma is an empty list element, which RFC 9110 §5.6.1
        says to ignore.
        """
        etag = self.seed(client, person, {'searchTitle': 'myeloma'})

        refused = client.patch(
            self.url(person), {'preferences': {'phase': 'PHASE3'}},
            format='json', HTTP_IF_MATCH=f'{etag}, garbage',
        )
        assert refused.status_code == 412
        assert TrialSearchPreferences.objects.get(person=person).preferences == {
            'searchTitle': 'myeloma'
        }

        assert client.patch(
            self.url(person), {'preferences': {'phase': 'PHASE3'}},
            format='json', HTTP_IF_MATCH=f'"no-such-tag", {etag}',
        ).status_code == 200

        etag2 = '"{}"'.format(
            rows(
                client.get(
                    f'/api/v1/trial-search-preferences/?person_id={person.pk}'
                )
            )[0]['updated_at']
        )
        assert client.patch(
            self.url(person), {'preferences': {'sponsor': 'Acme'}},
            format='json', HTTP_IF_MATCH=f'{etag2},',
        ).status_code == 200

    def test_a_412_says_which_precondition_failed(self, client, person):
        """One 412 text cannot serve a stale tag and an unreadable header.

        "Re-read and re-apply" is right for the first and useless for the
        second: obeying it re-sends the same broken header and 412s again,
        forever, with no signal that the header is the problem.
        """
        stale = self.seed(client, person, {'searchTitle': 'myeloma'})
        self.seed(client, person, {'sponsor': 'Acme'})

        changed = client.patch(
            self.url(person), {'preferences': {'phase': 'PHASE3'}},
            format='json', HTTP_IF_MATCH=stale,
        )
        assert changed.status_code == 412
        assert 're-read' in changed.json()['error']

        malformed = client.patch(
            self.url(person), {'preferences': {'phase': 'PHASE3'}},
            format='json', HTTP_IF_MATCH='not-an-entity-tag',
        )
        assert malformed.status_code == 412
        assert 'could not be read' in malformed.json()['error']

        weak = client.patch(
            self.url(person), {'preferences': {'phase': 'PHASE3'}},
            format='json', HTTP_IF_MATCH='W/"2020-01-01T00:00:00Z"',
        )
        assert weak.status_code == 412
        assert 'weak' in weak.json()['error']

    def test_a_broken_header_is_named_even_when_there_is_no_row(
        self, client, person
    ):
        """Header shape is judged before row existence, and must be.

        The other order answers "there are no stored preferences" to a
        client whose header is simply unreadable — whose natural next move
        is `If-None-Match: *`, which succeeds and creates a row on behalf
        of a client we could not understand.
        """
        assert not TrialSearchPreferences.objects.filter(person=person).exists()
        malformed = client.patch(
            self.url(person), {'preferences': {'phase': 'PHASE3'}},
            format='json', HTTP_IF_MATCH='garbage',
        )
        assert malformed.status_code == 412
        assert 'could not be read' in malformed.json()['error']

        weak = client.patch(
            self.url(person), {'preferences': {'phase': 'PHASE3'}},
            format='json', HTTP_IF_MATCH='W/"2020-01-01T00:00:00Z"',
        )
        assert weak.status_code == 412
        assert 'weak' in weak.json()['error']

        # And the genuinely-absent row still says so.
        missing = client.patch(
            self.url(person), {'preferences': {'phase': 'PHASE3'}},
            format='json', HTTP_IF_MATCH='"2020-01-01T00:00:00Z"',
        )
        assert missing.status_code == 412
        assert 'no stored preferences' in missing.json()['error']
        assert not TrialSearchPreferences.objects.filter(person=person).exists()

    def test_an_unconditional_detail_write_returns_its_own_etag(
        self, client, person
    ):
        """The tag matches the body it came with.

        Single-threaded, so this pins the agreement and not the reason for
        it: the implementation builds the tag from the instance it just
        serialized rather than re-reading the row, because a writer landing
        between two reads would make the tag describe THEIR version. That
        race is argued at the call site; nothing here can observe it.
        """
        self.seed(client, person, {'searchTitle': 'myeloma'})
        row = TrialSearchPreferences.objects.get(person=person)
        response = client.patch(
            f'/api/v1/trial-search-preferences/{row.pk}/',
            {'preferences': {'sponsor': 'Acme'}},
            format='json',
        )
        assert response.status_code == 200
        assert response['ETag'] == '"{}"'.format(response.json()['updated_at'])

    def test_if_none_match_star_writes_only_when_there_is_no_row(
        self, client, person
    ):
        """The write `If-Match` cannot cover: the first one.

        Before the row exists there is no etag to quote, so two tabs
        bootstrapping the same patient would both write unconditionally and
        the second would replace the first.
        """
        created = client.patch(
            self.url(person), {'preferences': {'searchTitle': 'myeloma'}},
            format='json', HTTP_IF_NONE_MATCH='*',
        )
        assert created.status_code == 200
        assert TrialSearchPreferences.objects.get(person=person).preferences == {
            'searchTitle': 'myeloma'
        }

        second = client.patch(
            self.url(person), {'preferences': {'sponsor': 'Acme'}},
            format='json', HTTP_IF_NONE_MATCH='*',
        )
        assert second.status_code == 412
        assert second['ETag'] == created['ETag']
        # Refused means refused: the losing tab's payload is not written.
        assert TrialSearchPreferences.objects.get(person=person).preferences == {
            'searchTitle': 'myeloma'
        }


@pytest.mark.skipif(
    connection.vendor != 'postgresql',
    reason=(
        'Needs row-level locking. `select_for_update` is a no-op on the '
        'SQLite dev fallback, so these would fail there — not because the '
        'code is wrong but because that backend cannot provide what they '
        'assert. `SET lock_timeout` is PostgreSQL-only too.'
    ),
)
@pytest.mark.django_db(transaction=True)
class TestTwoWritersAtOnce:
    """The feature's actual subject, with two real connections.

    Every other test in this file runs one request at a time, and against a
    check-then-write implementation they ALL pass while the lost update is
    still there — it was reproduced on the first try with two threads
    before the lock existed. So this is the test that distinguishes the
    feature from its appearance.

    Parametrized over all three conditional write paths, because a guard on
    one of them lets a future refactor delete the other two locks with a
    green suite — which was measured: removing the `reset` or detail lock
    left the whole file passing.

    `transaction=True` because the point is committed state seen across
    connections, which the usual test-wrapping transaction hides.
    """

    def _actor(self):
        person = PersonFactory()
        identity = Identity.objects.create_user(
            email=f'race{person.pk}@example.test', password=None
        )
        PatientUser.objects.create(identity=identity, person=person)
        return person, identity

    def _client(self, identity):
        api = APIClient()
        api.force_authenticate(user=identity)
        return api

    @pytest.mark.parametrize('path', ['upsert', 'reset', 'detail'])
    def test_two_writers_holding_one_etag_cannot_both_win(self, path):
        person, identity = self._actor()
        upsert_url = (
            f'/api/v1/trial-search-preferences/upsert/?person_id={person.pk}'
        )
        seeded = self._client(identity).patch(
            upsert_url, {'preferences': {'seed': True}}, format='json'
        )
        assert seeded.status_code == 200
        etag = seeded['ETag']
        row_pk = TrialSearchPreferences.objects.get(person=person).pk

        if path == 'upsert':
            def send(client, name):
                return client.patch(
                    upsert_url, {'preferences': {name: 1}}, format='json',
                    HTTP_IF_MATCH=etag,
                )
        elif path == 'reset':
            def send(client, name):
                return client.patch(
                    f'/api/v1/trial-search-preferences/reset/?person_id={person.pk}',
                    {}, format='json', HTTP_IF_MATCH=etag,
                )
        else:
            def send(client, name):
                return client.patch(
                    f'/api/v1/trial-search-preferences/{row_pk}/',
                    {'preferences': {name: 1}}, format='json',
                    HTTP_IF_MATCH=etag,
                )

        start = threading.Barrier(2)
        results = {}

        def write(name):
            try:
                # A writer that blocks on the row lock must fail, not hang.
                # Without this a deadlock leaves the thread holding a lock
                # while `transaction=True` teardown TRUNCATEs the table,
                # and CI hangs instead of reporting.
                with connections['default'].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '10s'")
                start.wait(timeout=10)
                results[name] = send(self._client(identity), name).status_code
            finally:
                # Each thread gets its own connection; leaving them open
                # makes the test database undroppable afterwards.
                connections.close_all()

        threads = [threading.Thread(target=write, args=(n,)) for n in ('A', 'B')]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in threads), 'a writer deadlocked'

        assert sorted(results.values()) == [200, 412], results
        stored = TrialSearchPreferences.objects.get(person=person).preferences
        if path == 'reset':
            # Both writers asked for the same thing, so the winner is not
            # identifiable from the row — what matters is that one was told
            # no rather than both being told yes.
            assert stored == {}
        else:
            # The row holds the winner's payload whole — not a blend, and
            # not the loser's.
            winner = next(name for name, code in results.items() if code == 200)
            assert stored == {winner: 1}

    def test_two_first_writers_cannot_both_create(self):
        """`If-None-Match: *`, which the unique constraint arbitrates.

        The first write has no etag to quote, so this is the only thing
        standing between two tabs bootstrapping the same patient.
        """
        person, identity = self._actor()
        url = f'/api/v1/trial-search-preferences/upsert/?person_id={person.pk}'
        start = threading.Barrier(2)
        results = {}

        def write(name):
            try:
                with connections['default'].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '10s'")
                start.wait(timeout=10)
                results[name] = self._client(identity).patch(
                    url, {'preferences': {name: 1}}, format='json',
                    HTTP_IF_NONE_MATCH='*',
                ).status_code
            finally:
                connections.close_all()

        threads = [threading.Thread(target=write, args=(n,)) for n in ('A', 'B')]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in threads), 'a writer deadlocked'

        assert sorted(results.values()) == [200, 412], results
        winner = next(name for name, code in results.items() if code == 200)
        assert TrialSearchPreferences.objects.get(person=person).preferences == {
            winner: 1
        }


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
