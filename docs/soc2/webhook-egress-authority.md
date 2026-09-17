# Who may configure webhook egress

**Decision.** Creating, editing or deleting a webhook subscription requires
platform staff or a direct `org_admin` grant on the organization named in the
subscription. Organization and domain trusts do not carry that authority.

**Status:** implemented (#1220, acting on the review of #1318). Enforced in
`patient_portal/api/webhook_views.py` by `WebhookManagementPermission` and by
`WebhookSubscriptionViewSet.get_queryset`, both routing writes through
`omop_core.services.access.get_direct_admin_orgs`. Covered by
`tests/test_webhooks.py::test_a_trust_no_longer_reaches_subscription_creation`
and the two tests beside it.

## What was true before

`get_admin_orgs` answers "whose data may this user reach". For a non-staff user
it resolves through `get_admin_access_paths`, which returns three kinds of path:
a direct `org_grant`, an `organization_trust`, and a `domain_trust`. Subscription
writes used that set, so a professional at organization A holding a trust into
organization B could create a subscription streaming B's patient events to any
public HTTPS URL, and `create()` returns the signing secret to whoever made it.

The payloads carry identifiers and event shape — `person_id`, resource type,
operation, count — not clinical values, which bounds the severity. It is still a
continuous, authenticated feed naming which of B's patients changed and when.

## Why the trust does not carry it

A trust is granted so a professional can work with another organization's
patients. Egress configuration is a different act: it decides where that
organization's data goes, it persists after the professional's involvement ends,
and it is invisible to the organization whose data it names unless someone reads
the subscription list. Nothing in granting a trust expresses an intent to
delegate that.

The organization that owns the data keeps the authority over where it flows. An
organization that wants a partner to run its integrations grants that partner an
`org_admin` role, which is an explicit, revocable, auditable act with the right
name on it.

## What is deliberately unchanged

Reading subscriptions and their delivery history still follows the full
`get_admin_orgs` reach, including trusts. The list a professional sees therefore
matches the data they already work with, and narrowing reads would hide an
organization's egress configuration from someone the organization has already
trusted with its patients — the wrong direction for review and for incident
response.

## Residual risk

A direct `org_admin` grant is still sufficient to point an organization's event
stream anywhere public. That is the intended authority, and it is bounded by the
same controls as the rest of the endpoint: the URL must be public HTTPS on 443
and is re-resolved at send, CSRF is enforced on every mutation, `create()`
discloses the secret once with `Cache-Control: no-store`, and every call is
audited. Revoking the grant does not delete subscriptions it created — review
them when revoking.
