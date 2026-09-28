import uuid
import secrets

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from patient_portal.service_tokens import validate_service_scopes


def webhook_secret():
    return secrets.token_urlsafe(32)


def validate_webhook_subscription_url(value):
    """Same rule as the API: public HTTPS on 443, no credentials, no fragment.

    Referenced by name in the webhook schema migration, so renaming or moving
    this function breaks a replay from zero. Leave a stub if it ever moves.

    What this does and does not reach: like any Django validator it runs on
    ``full_clean()``, so it covers model forms (Django admin) and DRF, which
    copies model-field validators onto the serializer field. A direct
    ``objects.create()`` or ``.save()`` still writes whatever it is given —
    Django does not call ``full_clean()`` on save, and forcing it here would
    change the semantics of every write path for one field. The delivery task
    re-validates and re-resolves before sending, so such a row fails closed at
    send rather than causing an SSRF; what this adds is that the rule is stated
    on the field, and that the paths a person actually uses refuse it up front.
    """
    from patient_portal.webhooks import validate_webhook_url

    try:
        validate_webhook_url(value)
    except ValueError:
        # A fixed message, never the exception text. DRF runs model-field
        # validators before the serializer's own method, so this is the path
        # that produces the API's URL error — forwarding str(error) here would
        # quietly undo the no-details control the serializer was written for.
        raise ValidationError(
            'Webhook URL must resolve only to public HTTPS addresses on port 443.'
        ) from None


class WebhookSubscription(models.Model):
    organization = models.ForeignKey('omop_core.Organization', on_delete=models.CASCADE)
    url = models.URLField(max_length=2048, validators=[validate_webhook_subscription_url])
    event_types = models.JSONField()
    secret = models.CharField(max_length=128, default=webhook_secret, editable=False)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Removing a subscription marks it instead of deleting the row. A DELETE
    # used to cascade the delivery history with it, so the one record of where
    # an organization's events had actually been going disappeared with the
    # configuration that sent them there. Writes exclude these rows (so the
    # subscription is gone for every practical purpose); reads keep them, which
    # is what makes the history reachable afterwards.
    deleted_at = models.DateTimeField(null=True, blank=True)


class InboundWebhookEvent(models.Model):
    source = models.CharField(max_length=100)
    event_id = models.CharField(max_length=128)
    organization = models.ForeignKey('omop_core.Organization', on_delete=models.CASCADE)
    event_type = models.CharField(max_length=64)
    payload_digest = models.CharField(max_length=64)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['source', 'event_id'], name='unique_inbound_webhook_event',
        )]


class WebhookDelivery(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(WebhookSubscription, on_delete=models.CASCADE, related_name='deliveries')
    payload = models.JSONField()
    status = models.CharField(max_length=20, default='pending', choices=[
        ('pending', 'Pending'), ('sending', 'Sending'), ('retry', 'Retry'),
        ('delivered', 'Delivered'), ('dead_letter', 'Dead letter'), ('cancelled', 'Cancelled'),
    ])
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    response_status = models.PositiveSmallIntegerField(null=True)
    error = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True)
    # When this row was last handed to the broker. The recovery sweep is for
    # rows the broker lost, and it cannot tell those from rows that are simply
    # waiting their turn: without this it re-queues every due row every minute,
    # so a backlog it cannot drain turns into duplicate messages faster than
    # the workers remove them.
    queued_at = models.DateTimeField(null=True, blank=True)
    # The address this delivery is for, frozen when the row is written. An
    # outbox row addressed to "wherever the subscription points at send time"
    # cannot be audited: the URL is mutable, the row can sit through minutes of
    # retry backoff and a recovery sweep, and a PATCH in between both moves
    # PHI that was already queued and leaves any record of the old destination
    # wrong. Freezing it means a redirect governs future events only, every
    # attempt on this row went exactly here, and the row can say so.
    #
    # The API exposes the host, never this: for some receivers the path is the
    # credential. `WebhookSubscriptionChange` is where destinations over time
    # are recorded, with the actor who changed them.
    destination_url = models.URLField(max_length=2048, blank=True)

    class Meta:
        indexes = [models.Index(
            fields=['next_attempt_at'], name='webhook_due_active_idx',
            condition=Q(status__in=['pending', 'retry', 'sending']),
        )]


class WebhookSubscriptionChange(models.Model):
    """Append-only record of who pointed an organization's events where.

    The generic audit row (``AuditLogMiddleware``) carries method, path and
    status, which for ``PATCH /api/v1/webhooks/subscriptions/<id>/`` says that
    an egress configuration changed but not what it became. The destination is
    the fact that matters: without this, an admin could point an organization's
    patient events at a host of their choosing, leave it for a week, delete the
    subscription, and leave nothing behind saying where they had gone.

    Append-only is enforced here for the paths people use — ``save()`` on an
    existing row and ``delete()`` both refuse. ``QuerySet.update()``, a direct
    SQL statement, ``bulk_create`` and a fixture load all get through, and a
    model cannot stop them: the durable guarantee for those is a database role
    that cannot write this table, which belongs to the deployment.

    What does not depend on the deployment is that the same facts are written a
    second time, into the ``AuditEvent`` row for the request (``detail``), which
    is HMAC-signed and hash-chained to its predecessor. Rewriting a row here
    without also breaking that chain leaves the two trails contradicting each
    other. This table is the queryable index — typed columns, its own history
    per subscription, and never pruned by retention — rather than the
    tamper-evidence mechanism.

    Rows survive what they describe. The organization and subscription
    references are nullable and their identifying values are copied in, so
    deleting either leaves the record readable rather than taking it along.
    """
    ACTION_CREATE = 'create'
    ACTION_UPDATE = 'update'
    ACTION_DELETE = 'delete'
    ACTION_ROTATE = 'rotate_secret'
    ACTIONS = [
        (ACTION_CREATE, 'Created'),
        (ACTION_UPDATE, 'Updated'),
        (ACTION_DELETE, 'Deleted'),
        # A rotation moves no destination, so both URL sides carry the same
        # value; what it records is who replaced the credential, and when.
        (ACTION_ROTATE, 'Signing secret rotated'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(
        WebhookSubscription, on_delete=models.SET_NULL, null=True, related_name='changes',
    )
    # Matches WebhookSubscription's BigAutoField: a narrower column would make
    # the audit insert the thing that fails, and take the change it records
    # down with it.
    subscription_pk = models.PositiveBigIntegerField()
    organization = models.ForeignKey('omop_core.Organization', on_delete=models.SET_NULL, null=True)
    organization_slug = models.CharField(max_length=255)
    action = models.CharField(max_length=16, choices=ACTIONS)
    # Empty on the side where there is no destination: before a create, after a
    # delete. A URL is always both recorded and attributable to an actor.
    url_before = models.URLField(max_length=2048, blank=True)
    url_after = models.URLField(max_length=2048, blank=True)
    event_types_before = models.JSONField(null=True)
    event_types_after = models.JSONField(null=True)
    active_before = models.BooleanField(null=True)
    active_after = models.BooleanField(null=True)
    # The string form of the acting Identity's pk, in the same shape AuditEvent
    # stores it under `user_id`, so the two trails join on that pair and the
    # value outlives the Identity row.
    actor_id = models.CharField(max_length=64, blank=True)
    actor_email = models.CharField(max_length=254, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'webhook_subscription_change'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['subscription_pk', '-created_at'],
                         name='webhook_change_sub_ts_idx'),
            models.Index(fields=['organization_slug', '-created_at'],
                         name='webhook_change_org_ts_idx'),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Webhook subscription changes are append-only.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Webhook subscription changes are append-only.')


class IdentityManager(BaseUserManager):
    use_in_migrations = True

    def get_or_create_from_claims(self, claims):
        """Get or create an Identity from TokenClaims."""
        return self.get_or_create(
            issuer=claims.issuer,
            sub=claims.sub,
            defaults={"uid": f"{claims.issuer}:{claims.sub}"},
        )

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email)
        extra_fields.pop("sub", None)
        # A staff account is made by an operator at a shell, who vouches for the
        # address. Everyone else proves theirs by following an emailed link.
        if extra_fields.get("is_staff") and "email_verified_at" not in extra_fields:
            from django.utils import timezone
            extra_fields["email_verified_at"] = timezone.now()
        identity = self.model(
            issuer="urn:local",
            sub=str(uuid.uuid4()),
            email=email,
            **extra_fields,
        )
        identity.set_password(password)
        identity.save(using=self._db)
        return identity

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)


class Identity(AbstractBaseUser, PermissionsMixin):
    """OIDC-based identity model: (issuer, sub) tuple."""
    issuer = models.CharField(max_length=255)
    sub = models.CharField(max_length=255)
    uid = models.CharField(max_length=512, unique=True, editable=False)

    email = models.EmailField(blank=True, default="")
    name = models.CharField(max_length=255, blank=True, default="")

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_premium = models.BooleanField(default=False, help_text="Grants premium-tier feature access (e.g. data export) across all connected apps.")
    created_at = models.DateTimeField(auto_now_add=True)

    # Authentication-policy state (PHR-S FM TI.1.1). Applies to local
    # (email/password) accounts; OIDC/service identities carry an unusable password.
    must_change_password = models.BooleanField(
        default=False, help_text="Force a password change on next successful login (e.g. after an admin reset).",
    )
    email_verified_at = models.DateTimeField(
        null=True, blank=True,
        help_text=(
            "When this account proved it receives mail at `email`: by following an "
            "emailed verification, invitation or password-reset link. Null for a "
            "local account that only typed the address in. Anything that grants "
            "access because of the address (a domain trust, a pending invitation, "
            "matching a person by email) must go through `has_verified_email`."
        ),
    )
    failed_login_count = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(
        null=True, blank=True, help_text="If set and in the future, local logins are refused (lockout).",
    )

    objects = IdentityManager()

    @property
    def is_locked(self) -> bool:
        return bool(self.locked_until and self.locked_until > timezone.now())

    USERNAME_FIELD = "uid"
    REQUIRED_FIELDS = ["email"]

    class Meta:
        db_table = "identity"
        verbose_name_plural = "identities"
        constraints = [
            models.UniqueConstraint(
                fields=["issuer", "sub"],
                name="uq_identity_issuer_sub",
            ),
        ]

    def save(self, *args, **kwargs):
        fields = kwargs.get('update_fields')
        if self.pk and self.email_verified_at is not None and (fields is None or 'email' in fields):
            previous_email = type(self).objects.filter(pk=self.pk).values_list('email', flat=True).first()
            if previous_email is not None and previous_email.lower() != self.email.lower():
                self.email_verified_at = None
                if fields is not None:
                    kwargs['update_fields'] = list(fields) + ['email_verified_at']
        self.uid = f"{self.issuer}:{self.sub}"
        if kwargs.get("update_fields") is not None and "uid" not in kwargs["update_fields"]:
            kwargs["update_fields"] = list(kwargs["update_fields"]) + ["uid"]
        super().save(*args, **kwargs)

    @property
    def is_local(self) -> bool:
        return self.issuer == "urn:local"

    @property
    def has_verified_email(self) -> bool:
        """Whether `email` may be used to decide what this account can reach.

        Federated logins record the provider's verified claim explicitly too;
        a stored address alone is never evidence of mailbox ownership.
        """
        return bool(self.email) and self.email_verified_at is not None

    @property
    def verified_email_domain(self) -> str:
        """The lowercased domain of a verified email, else ''."""
        if not self.has_verified_email:
            return ''
        return self.email.rpartition('@')[2].lower()

    def mark_email_verified(self, save=True):
        if self.email_verified_at is None:
            from django.utils import timezone
            self.email_verified_at = timezone.now()
            if save:
                self.save(update_fields=['email_verified_at'])

    @property
    def username(self):
        return self.email or self.sub

    def __str__(self):
        if self.email:
            return self.email
        return f"{self.issuer}|{self.sub}"


class PasswordHistory(models.Model):
    """Prior password hashes for an Identity, to enforce no-reuse policy
    (PHR-S FM TI.1.1#04 time-based and #05 count-based reuse limits)."""
    identity = models.ForeignKey(
        Identity, on_delete=models.CASCADE, related_name='password_history',
    )
    password = models.CharField(max_length=128)  # the hashed password
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'password_history'
        ordering = ['-created_at']
        indexes = [models.Index(fields=['identity', '-created_at'], name='pwhist_identity_ts_idx')]

    def __str__(self):
        return f"PasswordHistory({self.identity_id} @ {self.created_at:%Y-%m-%d})"


class PatientUser(models.Model):
    """Links an OIDC identity to an OMOP Person for patient portal access."""
    identity = models.OneToOneField(
        Identity, on_delete=models.CASCADE,
        related_name='patient_user',
    )
    person = models.OneToOneField(
        'omop_core.Person', on_delete=models.CASCADE,
        related_name='portal_user',
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_login = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'patient_user'

    def __str__(self):
        return f"{self.identity} - Person {self.person.person_id}"


class PatientInvitation(models.Model):
    """An email invitation for a patient to claim (sign up for) their own record.

    Created by staff/providers against a Person. The patient receives a link,
    sets a password, and on acceptance a local Identity is created (or reused)
    and bound to the Person via a PatientUser — turning them into a first-class
    PHR Account Holder (PHR-S FM PH.1). Mirrors OrgInvitation, but the invitee
    sets their own password instead of requiring pre-approved account creation.
    """
    STATUS_PENDING = 'pending'
    STATUS_ACCEPTED = 'accepted'
    STATUS_EXPIRED = 'expired'
    STATUS_CANCELLED = 'cancelled'

    person = models.ForeignKey(
        'omop_core.Person', on_delete=models.CASCADE,
        related_name='patient_invitations',
    )
    email = models.EmailField()
    token = models.CharField(max_length=64, unique=True)
    invited_by = models.ForeignKey(
        Identity, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'patient_invitation'
        constraints = [
            models.UniqueConstraint(
                fields=['person'],
                condition=Q(accepted_at__isnull=True, cancelled_at__isnull=True),
                name='uq_patient_invitation_pending',
            ),
        ]

    @property
    def status(self):
        from django.utils import timezone
        if self.accepted_at:
            return self.STATUS_ACCEPTED
        if self.cancelled_at:
            return self.STATUS_CANCELLED
        if timezone.now() > self.expires_at:
            return self.STATUS_EXPIRED
        return self.STATUS_PENDING

    def __str__(self):
        return f"Invite Person {self.person_id} ({self.email})"


class PatientConsent(models.Model):
    """Track patient consent for data sharing and clinical trials"""
    patient_user = models.ForeignKey(PatientUser, on_delete=models.CASCADE, related_name='consents')
    consent_type = models.CharField(max_length=50, choices=[
        ('data_sharing', 'Data Sharing'),
        ('clinical_trial', 'Clinical Trial Participation'),
        ('research', 'Research Use'),
    ])
    consent_granted = models.BooleanField(default=False)
    consent_date = models.DateTimeField(auto_now=True)
    consent_document = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'patient_consent'
        unique_together = ['patient_user', 'consent_type']

    def __str__(self):
        return f"{self.patient_user} - {self.consent_type}"


class PatientMessage(models.Model):
    """Messages between patients and healthcare providers"""
    CONFIDENTIALITY_NORMAL = 'normal'
    CONFIDENTIALITY_RESTRICTED = 'restricted'
    CONFIDENTIALITY_VERY_RESTRICTED = 'very_restricted'
    CONFIDENTIALITY_CHOICES = [
        (CONFIDENTIALITY_NORMAL, 'Normal'),
        (CONFIDENTIALITY_RESTRICTED, 'Restricted'),
        (CONFIDENTIALITY_VERY_RESTRICTED, 'Very restricted'),
    ]

    patient_user = models.ForeignKey(PatientUser, on_delete=models.CASCADE, related_name='messages')
    parent = models.ForeignKey(
        'self', on_delete=models.CASCADE, null=True, blank=True, related_name='replies',
        help_text='Parent message for threading (null = top-level message)',
    )
    sender = models.ForeignKey(
        Identity, on_delete=models.SET_NULL, null=True, blank=True, related_name='sent_messages',
        help_text='Identity of the sender',
    )
    subject = models.CharField(max_length=200)
    message = models.TextField()
    sender_is_patient = models.BooleanField(default=True)
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True, help_text='When the message was read')
    confidentiality = models.CharField(
        max_length=20, choices=CONFIDENTIALITY_CHOICES, default=CONFIDENTIALITY_NORMAL, db_index=True,
        help_text='Sensitivity level (PHR-S FM PH.6.3#08); restricted messages are hidden '
                  'from staff other than the sender.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'patient_message'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.subject} - {self.created_at}"


class AuditEvent(models.Model):
    """A persisted audit-trail entry — HL7 PHR-S FM TI.2 (Audit).

    One row per audited API request (reads and writes), written by
    AuditLogMiddleware in addition to the stdout JSON line. Reviewable via
    the read-only /api/v1/audit-events/ endpoint (TI.2.3).

    `user_id` is the string form of the acting Identity's PK (matching the
    stdout log), so it is comparable even after the Identity is deleted.
    """
    EVENT_VIEW = 'record_view'
    EVENT_CREATE = 'record_create'
    EVENT_UPDATE = 'record_update'
    EVENT_DELETE = 'record_delete'
    EVENT_AUTH = 'auth'
    EVENT_CONSENT = 'consent'
    EVENT_ADMIN = 'admin'                # Django-admin / background system activity (TI.2.1)
    EVENT_AUDIT_REVIEW = 'audit_review'  # access to the audit trail itself (TI.2.2#04)
    EVENT_BREAK_GLASS = 'break_glass'    # emergency-access authorization (TI.2.3#04)
    EVENT_OTHER = 'other'
    EVENT_TYPES = [
        (EVENT_VIEW, 'Record view'),
        (EVENT_CREATE, 'Record create'),
        (EVENT_UPDATE, 'Record update'),
        (EVENT_DELETE, 'Record delete'),
        (EVENT_AUTH, 'Authentication'),
        (EVENT_CONSENT, 'Consent'),
        (EVENT_ADMIN, 'Administrative / system'),
        (EVENT_AUDIT_REVIEW, 'Audit-log access'),
        (EVENT_BREAK_GLASS, 'Break-glass emergency access'),
        (EVENT_OTHER, 'Other'),
    ]

    event_type = models.CharField(max_length=32, choices=EVENT_TYPES, db_index=True)
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    method = models.CharField(max_length=8)
    path = models.CharField(max_length=512)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    user_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    user_email = models.CharField(max_length=254, null=True, blank=True)
    client_id = models.CharField(max_length=255, null=True, blank=True)
    resource_id = models.CharField(max_length=255, null=True, blank=True)
    ip_address = models.CharField(max_length=64, null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    detail = models.JSONField(null=True, blank=True)
    # Tamper-evidence: HMAC over the row's immutable content (TI.2.2.1#01). Any
    # later alteration is detectable by recomputing and comparing (see
    # `verify_audit_integrity`). Set automatically on first save.
    signature = models.CharField(max_length=64, blank=True, default='')
    # Hash chain: HMAC over (previous row's chain_hash + this row's signature),
    # so deletion or insertion of a row breaks the chain and is detectable
    # (TI.2.2.1#01). Empty on pre-chain rows and when AUDIT_HASH_CHAIN_ENABLED is off.
    chain_hash = models.CharField(max_length=64, blank=True, default='', db_index=True)

    class Meta:
        db_table = 'audit_event'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['user_id', '-timestamp'], name='audit_user_ts_idx'),
            models.Index(fields=['event_type', '-timestamp'], name='audit_type_ts_idx'),
        ]

    def compute_signature(self) -> str:
        """HMAC-SHA256 over the immutable content fields (excludes id/signature)."""
        import hashlib
        import hmac
        import json as _json
        from django.conf import settings

        ts = self.timestamp.isoformat() if self.timestamp else ''
        canonical = '|'.join(str(v) for v in [
            ts, self.event_type, self.method, self.path, self.status_code,
            self.user_id, self.user_email, self.client_id, self.resource_id,
            self.ip_address, self.duration_ms,
            _json.dumps(self.detail, sort_keys=True, default=str),
        ])
        key = (getattr(settings, 'AUDIT_HMAC_KEY', '') or settings.SECRET_KEY).encode()
        return hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()

    def compute_chain_hash(self, prev_chain_hash: str) -> str:
        """HMAC over (previous row's chain_hash | this row's signature). Linking each
        row to its predecessor makes row deletion/insertion detectable (TI.2.2.1#01)."""
        import hashlib
        import hmac
        from django.conf import settings

        key = (getattr(settings, 'AUDIT_HMAC_KEY', '') or settings.SECRET_KEY).encode()
        material = f"{prev_chain_hash}|{self.signature}".encode()
        return hmac.new(key, material, hashlib.sha256).hexdigest()

    # Advisory-lock key that serializes chain appends (Postgres) so concurrent
    # audit writes can't fork the chain.
    _CHAIN_LOCK_KEY = 728143

    def save(self, *args, **kwargs):
        from django.conf import settings

        if not self.signature:
            self.signature = self.compute_signature()

        chain_on = getattr(settings, 'AUDIT_HASH_CHAIN_ENABLED', True)
        if self.pk is None and not self.chain_hash and chain_on:
            # Seal into the tamper-evident chain: serialize the "read latest → link →
            # insert" so concurrent writers extend one linear chain rather than forking.
            from django.db import connection, transaction
            with transaction.atomic():
                if connection.vendor == 'postgresql':
                    with connection.cursor() as cur:
                        cur.execute('SELECT pg_advisory_xact_lock(%s)', [self._CHAIN_LOCK_KEY])
                prev = (
                    AuditEvent.objects.order_by('-id').values_list('chain_hash', flat=True).first()
                    or ''
                )
                self.chain_hash = self.compute_chain_hash(prev)
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    def signature_valid(self) -> bool:
        import hmac
        return bool(self.signature) and hmac.compare_digest(self.signature, self.compute_signature())

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M:%S} {self.event_type} {self.method} {self.path}"


class BreakGlassGrant(models.Model):
    """A time-boxed emergency-access ("break-glass") authorization (PHR-S FM
    TI.2.3#04). Grants the requesting identity emergency visibility of a specific
    patient's audit-log entries, with a captured reason, for a short window.
    Creating a grant is itself audited."""
    identity = models.ForeignKey(
        Identity, on_delete=models.CASCADE, related_name='break_glass_grants',
    )
    person_id = models.BigIntegerField(db_index=True)
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = 'break_glass_grant'
        ordering = ['-created_at']
        indexes = [models.Index(fields=['identity', 'expires_at'], name='breakglass_identity_exp_idx')]

    @property
    def is_active(self) -> bool:
        return self.expires_at > timezone.now()

    def __str__(self):
        return f"BreakGlass({self.identity_id} -> Person {self.person_id})"


class ServiceApplication(models.Model):
    """An editable application record with a stable service principal."""
    name = models.CharField(max_length=160)
    service_id = models.CharField(max_length=128, unique=True, validators=[
        RegexValidator(
            r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$', 'Use letters, numbers, dots, underscores, or hyphens.',
        ),
    ])
    description = models.TextField(blank=True)
    owner_contact = models.CharField(max_length=255, blank=True)
    scopes = models.CharField(max_length=512, default='patient/*.read', blank=True,
                              validators=[validate_service_scopes])
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name', 'pk']

    SCOPES_IN_USE = ('Clearing scopes would leave this application\'s live tokens '
                     'granting nothing. Revoke them first, or choose scopes.')

    def live_tokens(self):
        """Tokens that can still authenticate: not revoked, not past their expiry.

        An expired token is refused by stored_credential already, so counting it
        would make an operator revoke credentials that are dead anyway.
        """
        return self.tokens.filter(revoked_at__isnull=True).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))

    def clean(self):
        """Blank scopes are storable, but not while a live token depends on them.

        The field validator cannot express this — it sees a value, not the row —
        and the serializer's copy of the rule does not reach Django admin, which
        edits `scopes` as free text for any staff user. That is the same gap the
        scope cap fell through before it moved onto the field.
        """
        super().clean()
        if (self.scopes or '').split() or not self.pk:
            return
        if self.live_tokens().exists():
            raise ValidationError({'scopes': [self.SCOPES_IN_USE]})

    def __str__(self):
        return self.name


class ServiceAccessToken(models.Model):
    """Only the SHA-256 digest of a high-entropy bearer secret is persisted."""
    application = models.ForeignKey(ServiceApplication, on_delete=models.PROTECT, related_name='tokens')
    label = models.CharField(max_length=160)
    digest = models.CharField(max_length=64, unique=True, editable=False)
    suffix = models.CharField(max_length=4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(Identity, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='issued_service_tokens')
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(Identity, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='revoked_service_tokens')

    class Meta:
        ordering = ['-created_at', '-pk']

    def __str__(self):
        return f'{self.application.name}: {self.label} (…{self.suffix})'
