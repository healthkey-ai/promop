import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models import Q


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

    objects = IdentityManager()

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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'patient_message'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.subject} - {self.created_at}"
