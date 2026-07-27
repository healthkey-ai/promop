import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models import Q
from django.utils import timezone


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
        self.uid = f"{self.issuer}:{self.sub}"
        if kwargs.get("update_fields") is not None and "uid" not in kwargs["update_fields"]:
            kwargs["update_fields"] = list(kwargs["update_fields"]) + ["uid"]
        super().save(*args, **kwargs)

    @property
    def is_local(self) -> bool:
        return self.issuer == "urn:local"

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
