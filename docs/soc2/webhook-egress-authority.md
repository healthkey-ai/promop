# Who may configure webhook egress

**Decision.** Creating, editing or deleting a webhook subscription requires
platform staff or a direct `org_admin` grant on the organization named in the
subscription, **presented in an interactive session**. Organization and domain
trusts do not carry that authority, and neither does a credential the holder of
that authority handed to a machine.

**Status:** implemented (#1220, acting on the review of #1318). Enforced in
`patient_portal/api/webhook_views.py` by `WebhookManagementPermission` and by
`WebhookSubscriptionViewSet.get_queryset`, both routing writes through
`omop_core.services.access.get_direct_admin_orgs`, with the session requirement
from `patient_portal.api.permissions.is_interactive_session`. Covered by
`tests/test_webhooks.py::test_a_trust_no_longer_reaches_subscription_creation`
and the two tests beside it, and by
`test_a_delegated_machine_credential_cannot_configure_egress`.

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

## Which credential, not only which user

`get_direct_admin_orgs` answers "does this user hold the authority". It does not
answer "is this the user". A third-party application holding an OAuth2 access
token that an `org_admin` delegated to it carries `patient/*.write` and passes
every check above — the authority is real and the user is right — yet the person
is not present. Creating a subscription mints a long-lived signing secret and
names where the organization's patient events are sent, so it converts a scoped,
expiring, revocable grant into a permanent egress channel the application
chooses the destination for. Revoking the grant does not close the channel.

Writes therefore additionally require `is_interactive_session`: a session or a
partner (Firebase/PHR) token, the same rule `ServiceApplicationViewSet` applies
to service-credential administration, and for the same reason. The authenticator
is identified positively rather than inferred from `request.auth is None`,
because HTTP Basic also reports no token and `ENABLE_BASIC_AUTH` is a supported
deployment setting.

This closes an asymmetry rather than adding a new policy: promop had already
decided that administering a long-lived credential requires the person (#1372,
shipped in #1379). The webhook endpoint was written in parallel with that change
and did not pick it up.

## What is deliberately unchanged

Reading subscriptions and their delivery history still follows the full
`get_admin_orgs` reach, including trusts, and is open to every authentication
class the endpoint accepts: a listing discloses no secret — `create()` is the
only response that carries one — and an integration that watches the
subscriptions it already relies on is a legitimate caller. The list a professional sees therefore
matches the data they already work with, and narrowing reads would hide an
organization's egress configuration from someone the organization has already
trusted with its patients — the wrong direction for review and for incident
response.

## What the trail must show

The authority above is auditable only if the record survives the person who
exercised it. Two properties make that true, and both were absent in the first
implementation:

- **Every create, edit and delete appends a `webhook_subscription_change` row**
  naming the acting identity, the organization, and the URL and event types on
  both sides of the change. The generic audit row records that
  `PATCH /api/v1/webhooks/subscriptions/{id}/` happened; it does not record the
  destination, and the destination is the fact an investigation needs. The rows
  are append-only in the model and are never pruned by retention. Enforcing that
  at the database — a role that cannot write the table — is a deployment control
  and is not claimed here.
- **Removing a subscription marks it rather than deleting it**, and each
  delivery freezes the address it was written for. A cascading delete used to
  remove the delivery history along with the configuration, so the sequence
  "point the events at a host, leave it a week, delete the subscription" left
  nothing behind. It now leaves the change rows and the deliveries, each stating
  the address it was written for; a row's `attempts` is what says whether
  anything was actually sent there. Freezing the address also means changing a URL cannot redirect
  events that were already queued under the old one — a redirect applies to what
  the organization sends next, which is the only thing an admin should be able
  to decide after the fact.

A change made outside the API — Django admin, a shell, a data migration — writes
no change row, because the actor cannot be named from there. Treat direct
database and admin access to `webhook_subscription` as the privileged path it
is.

## Residual risk

A direct `org_admin` grant is still sufficient to point an organization's event
stream anywhere public. That is the intended authority, and it is bounded by the
same controls as the rest of the endpoint: the URL must be public HTTPS on 443
and is re-resolved at send, CSRF is enforced on every mutation, `create()`
discloses the secret once with `Cache-Control: no-store`, and every call is
audited. Revoking the grant does not delete subscriptions it created — review
them when revoking.
