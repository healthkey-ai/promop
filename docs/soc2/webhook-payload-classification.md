# What a webhook payload is, and how it must be treated

**Decision: a webhook notification is PHI.** Destinations require a BAA, the
same as any other recipient of patient data.

## What the payload actually contains

Every event carries `person_id`, the resource type, and the operation; some
carry a `resource_id` or a `count`. No clinical value — no result, no
diagnosis, no medication — is ever in the body.

That was the basis of the earlier, narrower reading: identifiers and event
shape, not clinical content. It is the wrong reading, for three reasons.

1. **The sender identifies the patient population.** A subscription belongs to
   one organization. At an oncology practice, "this organization has a patient
   `person_id=41988`" is itself a statement about that person, before any
   event type is read.
2. **The event type is clinical.** `lab.updated` for a given `person_id`, at a
   known clinical organization, on a known date, says that patient had a lab
   result that day. A stream of them is a treatment timeline at day
   granularity. `document.received` and `foundation.synced` say the same kind
   of thing about other classes of record.
3. **`person_id` is not anonymous to the recipient.** The subscriber is, by
   construction, a system that already holds records for these patients — that
   is why it subscribes. For it, the identifier resolves to a person.

Under 45 CFR 164.514(b) the identifiers here are not removed and the recipient
can re-identify, so the de-identification safe harbour does not apply. Treat
the stream as PHI.

## What follows

- **A destination needs a BAA.** Configuring a subscription to an organization
  with no business-associate agreement in place is a disclosure, not a
  technical detail. This is a contractual control: the code cannot check it,
  and the code does not pretend to.
- **The list of destinations is reviewable, and the trail of who set them is
  durable.** `webhook_subscription_change` records every change made through
  the API with its actor and the destination on each side of it — a create has
  no "before" and a delete no "after" — and the matching `AuditEvent` row is
  signed and chained ([egress authority](webhook-egress-authority.md)). A
  change made at a shell or by a data migration writes no row, because the
  actor cannot be named from there; direct database access to
  `webhook_subscription` is a privileged path and is controlled as one.
- **Delivery is minimised in what it carries and where it goes.** Public HTTPS
  on 443 only, TLS verified against a pinned address, signed with a
  per-subscription secret, and the body holds no clinical value — so a
  destination that turns out to be wrong discloses the fact of care, not its
  content.
- **Relayed inbound events are constrained.** An event arriving from a partner
  is republished to that organization's subscribers under this deployment's
  own signature. `resource_id` is passed through, so it must contain no
  whitespace: a partner cannot use it to push clinical narrative into a payload
  this document classifies as identifiers only.

## What this does not decide

Encryption of the signing secret at rest is decided in the encryption-at-rest
work, not here; the secret is a credential, not patient data, and follows that
decision. Retention of delivery history is `WEBHOOK_RETENTION_DAYS` (30 days by
default) — the change log is separate and is not pruned.
