import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models


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


class PatientConsent(models.Model):
    """Track patient consent for data sharing and clinical trials"""
    patient_user = models.ForeignKey(PatientUser, on_delete=models.CASCADE, related_name='consents')
    consent_type = models.CharField(max_length=50, choices=[
        ('data_sharing', 'Data Sharing'),
        ('clinical_trial', 'Clinical Trial Participation'),
        ('research', 'Research Use'),
    ])
    consent_granted = models.BooleanField(default=False)
    consent_date = models.DateTimeField(auto_now_add=True)
    consent_document = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'patient_consent'
        unique_together = ['patient_user', 'consent_type']

    def __str__(self):
        return f"{self.patient_user} - {self.consent_type}"


class PatientMessage(models.Model):
    """Messages between patients and healthcare providers"""
    patient_user = models.ForeignKey(PatientUser, on_delete=models.CASCADE, related_name='messages')
    subject = models.CharField(max_length=200)
    message = models.TextField()
    sender_is_patient = models.BooleanField(default=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'patient_message'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.subject} - {self.created_at}"
