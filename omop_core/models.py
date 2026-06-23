from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.contrib.postgres.indexes import GinIndex


class ProvenanceRecord(models.Model):
    """Audit trail for every clinical write — who created/modified a record and why."""
    SOURCE_CHOICES = [
        ('PATIENT_SELF',        'Patient self-entry'),
        ('ADMIN_CORRECTION',    'Admin on-behalf modification'),
        ('EHR_SYNC',            'EHR system sync'),
        ('DOCUMENT_EXTRACTION', 'AI document extraction'),
    ]
    source = models.CharField(max_length=50, choices=SOURCE_CHOICES)
    source_user_id = models.CharField(max_length=255, blank=True, default='')
    target_patient_id = models.CharField(max_length=255, null=True, blank=True)
    modification_reason = models.TextField(null=True, blank=True)
    organization = models.ForeignKey(
        'Organization', on_delete=models.SET_NULL, null=True, blank=True,
        help_text="Tenant organization that authorized this write",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    content_object = GenericForeignKey('content_type', 'object_id')

    class Meta:
        db_table = 'provenance_record'
        indexes = [models.Index(fields=['content_type', 'object_id'])]
        constraints = [
            models.UniqueConstraint(
                fields=['content_type', 'object_id', 'source_user_id', 'source'],
                name='uq_provenance_object_actor_source',
            ),
        ]

    def __str__(self):
        return f"{self.source} → {self.content_type} #{self.object_id}"


class Organization(models.Model):
    """A tenant organization (hospital, foundation, analytics service) that owns patient records."""
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=60, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )

    class Meta:
        db_table = 'organization'

    def __str__(self):
        return self.name


class OrgTrust(models.Model):
    """Grants access to an org via a domain or an org-to-org trust."""
    granting_org = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='trusts_granted',
    )
    trusted_org = models.ForeignKey(
        Organization, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='trusted_by',
    )
    trusted_domain = models.CharField(max_length=255, blank=True, default='')
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'org_trust'
        constraints = [
            models.CheckConstraint(
                check=(
                    Q(trusted_org__isnull=False, trusted_domain='') |
                    Q(trusted_org__isnull=True, trusted_domain__gt='')
                ),
                name='org_trust_org_xor_domain',
            ),
            models.CheckConstraint(
                check=Q(trusted_org__isnull=True) | ~Q(trusted_org=F('granting_org')),
                name='org_trust_no_self_trust',
            ),
        ]

    def __str__(self):
        if self.trusted_org_id:
            return f"{self.granting_org.slug} trusts org {self.trusted_org_id}"
        return f"{self.granting_org.slug} trusts domain {self.trusted_domain}"


class OrgInvitation(models.Model):
    """An email invitation to join an org with a specific role."""
    STATUS_PENDING = 'pending'
    STATUS_CONFIRMED = 'confirmed'
    STATUS_EXPIRED = 'expired'
    STATUS_CANCELLED = 'cancelled'
    STATUS = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_CONFIRMED, 'Confirmed'),
        (STATUS_EXPIRED, 'Expired'),
        (STATUS_CANCELLED, 'Cancelled'),
    ]
    ROLE = [
        ('org_admin', 'Org Admin'),
        ('doctor', 'Doctor'),
        ('navigator', 'Navigator'),
    ]
    org = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='invitations',
    )
    email = models.EmailField()
    role = models.CharField(max_length=20, choices=ROLE, default='doctor')
    token = models.CharField(max_length=64, unique=True)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'org_invitation'
        constraints = [
            models.UniqueConstraint(
                fields=['org', 'email'],
                condition=Q(confirmed_at__isnull=True, cancelled_at__isnull=True),
                name='uq_org_invitation_pending',
            ),
        ]

    @property
    def status(self):
        from django.utils import timezone
        if self.confirmed_at:
            return self.STATUS_CONFIRMED
        if self.cancelled_at:
            return self.STATUS_CANCELLED
        if timezone.now() > self.expires_at:
            return self.STATUS_EXPIRED
        return self.STATUS_PENDING

    def __str__(self):
        return f"Invite {self.email} to {self.org.slug} ({self.role})"


class ApplicationOrganization(models.Model):
    """Links an OAuth2 Application to an Organization for multi-tenant scoping."""
    application = models.OneToOneField(
        'oauth2_provider.Application',
        on_delete=models.CASCADE,
        related_name='org_profile',
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name='applications',
    )

    class Meta:
        db_table = 'application_organization'

    def __str__(self):
        return f"{self.application.name} → {self.organization.name}"


class PatientGroup(models.Model):
    """A group of patients for access control purposes."""
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name='patient_groups',
    )
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=60)
    description = models.TextField(blank=True, default='')
    rule_managed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )

    class Meta:
        db_table = 'patient_group'
        constraints = [
            models.UniqueConstraint(
                fields=['organization', 'slug'],
                name='uq_patient_group_org_slug',
            ),
        ]

    def __str__(self):
        return f"{self.organization.slug}/{self.name}"


class PatientGroupMembership(models.Model):
    """Links a patient (by person_id) to a group. No FK to Person — different DB."""
    SOURCE_CHOICES = [
        ('manual', 'Manual assignment'),
        ('rule', 'Rule-based auto-assignment'),
    ]
    group = models.ForeignKey(
        PatientGroup, on_delete=models.CASCADE, related_name='memberships',
    )
    person_id = models.BigIntegerField(db_index=True)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default='manual')
    added_at = models.DateTimeField(auto_now_add=True)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )

    class Meta:
        db_table = 'patient_group_membership'
        constraints = [
            models.UniqueConstraint(
                fields=['group', 'person_id'],
                name='uq_group_person',
            ),
        ]

    def __str__(self):
        return f"Person {self.person_id} in {self.group.name}"


class GroupAccess(models.Model):
    """Grants a professional (Identity) access to an org or a patient group."""
    ROLE_CHOICES = [
        ('org_admin', 'Org Admin'),
        ('doctor',    'Doctor'),
        ('navigator', 'Navigator'),
    ]
    identity = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='group_access_grants',
    )
    org = models.ForeignKey(
        'Organization', on_delete=models.CASCADE,
        null=True, blank=True, related_name='access_grants',
    )
    group = models.ForeignKey(
        PatientGroup, on_delete=models.CASCADE,
        null=True, blank=True, related_name='access_grants',
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    expires_at = models.DateTimeField(null=True, blank=True)
    granted_at = models.DateTimeField(auto_now_add=True)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )

    class Meta:
        db_table = 'group_access'
        constraints = [
            models.CheckConstraint(
                check=(
                    Q(org__isnull=False, group__isnull=True) |
                    Q(org__isnull=True, group__isnull=False)
                ),
                name='group_access_org_xor_group',
            ),
            models.UniqueConstraint(
                fields=['identity', 'group'],
                condition=Q(group__isnull=False),
                name='uq_identity_group',
            ),
            models.UniqueConstraint(
                fields=['identity', 'org'],
                condition=Q(org__isnull=False),
                name='uq_identity_org',
            ),
        ]

    def __str__(self):
        scope = f"org={self.org_id}" if self.org_id else f"group={self.group_id}"
        return f"{self.identity} → {scope} ({self.role})"


class PersonalRepresentative(models.Model):
    """Links an Identity to a person they represent (child, parent, spouse, etc.).
    No FK to Person — different DB."""
    RELATIONSHIP_CHOICES = [
        ('parent', 'Parent'),
        ('child', 'Child'),
        ('spouse', 'Spouse'),
        ('guardian', 'Guardian'),
        ('caregiver', 'Caregiver'),
        ('other', 'Other'),
    ]
    VERIFICATION_CHOICES = [
        ('PENDING', 'Pending'),
        ('VERIFIED', 'Verified'),
        ('REJECTED', 'Rejected'),
    ]
    representative = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='represented_persons',
    )
    person_id = models.BigIntegerField(db_index=True)
    relationship = models.CharField(max_length=20, choices=RELATIONSHIP_CHOICES)
    verification_status = models.CharField(
        max_length=20, choices=VERIFICATION_CHOICES, default='PENDING',
    )
    granted_at = models.DateTimeField(auto_now_add=True)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )

    class Meta:
        db_table = 'personal_representative'
        constraints = [
            models.UniqueConstraint(
                fields=['representative', 'person_id'],
                name='uq_representative_person',
            ),
        ]

    def __str__(self):
        return f"{self.representative} represents Person {self.person_id} ({self.relationship})"


class Vocabulary(models.Model):
    """OMOP CDM Vocabulary table - standardized vocabularies."""
    vocabulary_id = models.CharField(max_length=20, primary_key=True)
    vocabulary_name = models.CharField(max_length=255)
    vocabulary_reference = models.CharField(max_length=255, null=True, blank=True)
    vocabulary_version = models.CharField(max_length=255, null=True, blank=True)
    vocabulary_concept_id = models.IntegerField()

    class Meta:
        db_table = 'vocabulary'

    def __str__(self):
        return f"{self.vocabulary_id}: {self.vocabulary_name}"


class Domain(models.Model):
    """OMOP CDM Domain table - high-level classification of concepts."""
    domain_id = models.CharField(max_length=20, primary_key=True)
    domain_name = models.CharField(max_length=255)
    domain_concept_id = models.IntegerField()

    class Meta:
        db_table = 'domain'

    def __str__(self):
        return f"{self.domain_id}: {self.domain_name}"


class ConceptClass(models.Model):
    """OMOP CDM Concept Class table - classification of concepts within domains."""
    concept_class_id = models.CharField(max_length=20, primary_key=True)
    concept_class_name = models.CharField(max_length=255)
    concept_class_concept_id = models.IntegerField()

    class Meta:
        db_table = 'concept_class'

    def __str__(self):
        return f"{self.concept_class_id}: {self.concept_class_name}"


class Concept(models.Model):
    """OMOP CDM Concept table - standardized terminologies."""
    concept_id = models.IntegerField(primary_key=True)
    concept_name = models.CharField(max_length=255)
    domain = models.ForeignKey(Domain, on_delete=models.PROTECT, db_column='domain_id')
    vocabulary = models.ForeignKey(Vocabulary, on_delete=models.PROTECT, db_column='vocabulary_id')
    concept_class = models.ForeignKey(ConceptClass, on_delete=models.PROTECT, db_column='concept_class_id')
    standard_concept = models.CharField(max_length=1, null=True, blank=True)
    concept_code = models.CharField(max_length=50)
    valid_start_date = models.DateField()
    valid_end_date = models.DateField()
    invalid_reason = models.CharField(max_length=1, null=True, blank=True)

    class Meta:
        db_table = 'concept'
        indexes = [
            models.Index(
                fields=['vocabulary_id', 'concept_code'],
                name='ix_concept_vocab_code',
            ),
            # GIN trigram index — makes concept_name__icontains fast on large vocab tables.
            # Requires pg_trgm extension (added via TrigramExtension() in migration).
            GinIndex(
                fields=['concept_name'],
                name='ix_concept_name_trgm',
                opclasses=['gin_trgm_ops'],
            ),
        ]

    def __str__(self):
        return f"{self.concept_id}: {self.concept_name}"


class Relationship(models.Model):
    """OMOP CDM Relationship table - defines relationships between concepts."""
    relationship_id = models.CharField(max_length=20, primary_key=True)
    relationship_name = models.CharField(max_length=255)
    is_hierarchical = models.IntegerField()
    defines_ancestry = models.IntegerField()
    reverse_relationship_id = models.CharField(max_length=20)
    relationship_concept_id = models.IntegerField()

    class Meta:
        db_table = 'relationship'

    def __str__(self):
        return self.relationship_id


class ConceptRelationship(models.Model):
    """OMOP CDM Concept Relationship table - pairwise relationships between concepts."""
    concept_1 = models.ForeignKey(
        Concept, on_delete=models.DO_NOTHING,
        related_name='relationships_as_source', db_column='concept_id_1',
    )
    concept_2 = models.ForeignKey(
        Concept, on_delete=models.DO_NOTHING,
        related_name='relationships_as_target', db_column='concept_id_2',
    )
    relationship = models.ForeignKey(
        Relationship, on_delete=models.DO_NOTHING, db_column='relationship_id',
    )
    valid_start_date = models.DateField()
    valid_end_date = models.DateField()
    invalid_reason = models.CharField(max_length=1, null=True, blank=True)

    class Meta:
        db_table = 'concept_relationship'
        unique_together = [('concept_1', 'concept_2', 'relationship')]

    def __str__(self):
        return f'{self.concept_1_id} --[{self.relationship_id}]--> {self.concept_2_id}'


class ConceptAncestor(models.Model):
    """OMOP CDM Concept Ancestor table - hierarchical ancestry between concepts."""
    ancestor_concept = models.ForeignKey(
        Concept, on_delete=models.DO_NOTHING,
        related_name='descendants', db_column='ancestor_concept_id',
    )
    descendant_concept = models.ForeignKey(
        Concept, on_delete=models.DO_NOTHING,
        related_name='ancestors', db_column='descendant_concept_id',
    )
    min_levels_of_separation = models.IntegerField()
    max_levels_of_separation = models.IntegerField()

    class Meta:
        db_table = 'concept_ancestor'
        unique_together = [('ancestor_concept', 'descendant_concept')]

    def __str__(self):
        return f'{self.ancestor_concept_id} -> {self.descendant_concept_id}'


class Location(models.Model):
    """OMOP CDM Location table - geographic locations."""
    location_id = models.BigIntegerField(primary_key=True)
    address_1 = models.CharField(max_length=50, null=True, blank=True)
    address_2 = models.CharField(max_length=50, null=True, blank=True)
    city = models.CharField(max_length=50, null=True, blank=True)
    state = models.CharField(max_length=2, null=True, blank=True)
    zip = models.CharField(max_length=9, null=True, blank=True)
    county = models.CharField(max_length=20, null=True, blank=True)
    country = models.CharField(max_length=100, null=True, blank=True)
    location_source_value = models.CharField(max_length=50, null=True, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    class Meta:
        db_table = 'location'

    def __str__(self):
        return f"Location {self.location_id}: {self.city}, {self.state}, {self.country}"


class Person(models.Model):
    """OMOP CDM Person table - stores demographic information"""
    person_id = models.IntegerField(primary_key=True)
    
    # Gender
    gender_concept = models.ForeignKey(
        Concept, 
        on_delete=models.PROTECT, 
        related_name='person_gender', 
        null=True, 
        blank=True,
        db_column='gender_concept_id'
    )
    gender_source_value = models.CharField(max_length=50, null=True, blank=True)
    gender_source_concept = models.ForeignKey(
        Concept,
        on_delete=models.PROTECT,
        related_name='person_gender_source',
        null=True,
        blank=True,
        db_column='gender_source_concept_id'
    )
    
    # Birth date fields - all nullable
    year_of_birth = models.IntegerField(null=True, blank=True)
    month_of_birth = models.IntegerField(null=True, blank=True)
    day_of_birth = models.IntegerField(null=True, blank=True)
    birth_datetime = models.DateTimeField(null=True, blank=True)
    
    # Race
    race_concept = models.ForeignKey(
        Concept,
        on_delete=models.PROTECT,
        related_name='person_race',
        null=True,
        blank=True,
        db_column='race_concept_id'
    )
    race_source_value = models.CharField(max_length=50, null=True, blank=True)
    race_source_concept = models.ForeignKey(
        Concept,
        on_delete=models.PROTECT,
        related_name='person_race_source',
        null=True,
        blank=True,
        db_column='race_source_concept_id'
    )
    
    # Ethnicity
    ethnicity_concept = models.ForeignKey(
        Concept, 
        on_delete=models.PROTECT, 
        related_name='person_ethnicity', 
        null=True, 
        blank=True,
        db_column='ethnicity_concept_id'
    )
    ethnicity_source_value = models.CharField(max_length=50, null=True, blank=True)
    ethnicity_source_concept = models.ForeignKey(
        Concept,
        on_delete=models.PROTECT,
        related_name='person_ethnicity_source',
        null=True,
        blank=True,
        db_column='ethnicity_source_concept_id'
    )
    
    # Location and provider references
    location_id = models.IntegerField(null=True, blank=True)
    provider_id = models.IntegerField(null=True, blank=True)
    care_site_id = models.IntegerField(null=True, blank=True)
    
    # Name fields (extension to OMOP CDM for practical use)
    given_name = models.CharField(max_length=100, null=True, blank=True, help_text="First/Given name")
    family_name = models.CharField(max_length=100, null=True, blank=True, help_text="Last/Family name")

    # External identity (OpenID Connect) — used by phr-etl find_or_create
    actor_iss = models.CharField(max_length=255, null=True, blank=True, help_text="OIDC issuer URL")
    actor_sub = models.CharField(max_length=255, null=True, blank=True, help_text="OIDC subject (Firebase UID)")

    class Meta:
        db_table = 'person'
        constraints = [
            models.UniqueConstraint(
                fields=['actor_iss', 'actor_sub'],
                condition=models.Q(actor_iss__isnull=False, actor_sub__isnull=False),
                name='person_actor_iss_sub_unique',
            )
        ]
    
    def __str__(self):
        if self.given_name or self.family_name:
            return f"{self.given_name} {self.family_name}".strip()
        return f"Person {self.person_id}"


class PersonLanguageSkill(models.Model):
    """Language skills for a person - supports multiple languages with different skill levels."""
    
    SKILL_LEVEL_CHOICES = [
        ('speak', 'Speak'),
        ('write', 'Write'),
        ('both', 'Both Speak and Write'),
    ]
    
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name='language_skills')
    language_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='person_language_skills', 
                                        db_column='language_concept_id')
    skill_level = models.CharField(max_length=10, choices=SKILL_LEVEL_CHOICES, 
                                  help_text="Language skill level: speak, write, both")
    is_primary = models.BooleanField(default=False, help_text="Is this the person's primary language?")
    created_date = models.DateTimeField(auto_now_add=True)
    updated_date = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'person_language_skill'
        unique_together = ['person', 'language_concept']
        indexes = [
            models.Index(fields=['person', 'is_primary']),
        ]

    def __str__(self):
        return f"Person {self.person_id}: {self.language_concept.concept_name} ({self.skill_level})"


class VisitOccurrence(models.Model):
    """OMOP CDM Visit Occurrence table - healthcare visits."""
    visit_occurrence_id = models.BigIntegerField(primary_key=True)
    person = models.ForeignKey(Person, on_delete=models.CASCADE, db_column='person_id')
    visit_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='visit_occurrences', db_column='visit_concept_id')
    visit_start_date = models.DateField()
    visit_start_datetime = models.DateTimeField(null=True, blank=True)
    visit_end_date = models.DateField()
    visit_end_datetime = models.DateTimeField(null=True, blank=True)
    visit_type_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='visit_type_occurrences', db_column='visit_type_concept_id')
    provider_id = models.IntegerField(null=True, blank=True)
    care_site_id = models.IntegerField(null=True, blank=True)
    visit_source_value = models.CharField(max_length=255, null=True, blank=True)
    visit_source_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='visit_source_occurrences', db_column='visit_source_concept_id', null=True, blank=True)
    admitted_from_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='visit_admitted_from', db_column='admitted_from_concept_id', null=True, blank=True)
    admitted_from_source_value = models.CharField(max_length=50, null=True, blank=True)
    discharged_to_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='visit_discharged_to', db_column='discharged_to_concept_id', null=True, blank=True)
    discharged_to_source_value = models.CharField(max_length=50, null=True, blank=True)
    preceding_visit_occurrence_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = 'visit_occurrence'
        constraints = [
            models.UniqueConstraint(
                fields=['person', 'visit_start_date', 'care_site_id', 'visit_source_value'],
                condition=models.Q(visit_source_value__isnull=False) & ~models.Q(visit_source_value=''),
                name='uq_visit_person_date_site_source',
            ),
        ]

    def __str__(self):
        return f"Visit {self.visit_occurrence_id} for Person {self.person_id}"


class ConditionOccurrence(models.Model):
    """OMOP CDM Condition Occurrence table - diagnoses and conditions."""
    condition_occurrence_id = models.BigIntegerField(primary_key=True)
    person = models.ForeignKey(Person, on_delete=models.CASCADE, db_column='person_id')
    condition_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='condition_occurrences', db_column='condition_concept_id')
    condition_start_date = models.DateField()
    condition_start_datetime = models.DateTimeField(null=True, blank=True)
    condition_end_date = models.DateField(null=True, blank=True)
    condition_end_datetime = models.DateTimeField(null=True, blank=True)
    condition_type_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='condition_type_occurrences', db_column='condition_type_concept_id')
    condition_status_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='condition_status_occurrences', db_column='condition_status_concept_id', null=True, blank=True)
    stop_reason = models.CharField(max_length=20, null=True, blank=True)
    provider_id = models.IntegerField(null=True, blank=True)
    visit_occurrence = models.ForeignKey(VisitOccurrence, on_delete=models.SET_NULL, db_column='visit_occurrence_id', null=True, blank=True)
    visit_detail_id = models.IntegerField(null=True, blank=True)
    condition_source_value = models.CharField(max_length=50, null=True, blank=True)
    condition_source_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='condition_source_occurrences', db_column='condition_source_concept_id', null=True, blank=True)
    condition_status_source_value = models.CharField(max_length=50, null=True, blank=True)

    class Meta:
        db_table = 'condition_occurrence'

    def __str__(self):
        return f"Condition {self.condition_occurrence_id} for Person {self.person_id}"


class DrugExposure(models.Model):
    """OMOP CDM Drug Exposure table - medications and treatments."""
    drug_exposure_id = models.BigIntegerField(primary_key=True)
    person = models.ForeignKey(Person, on_delete=models.CASCADE, db_column='person_id')
    drug_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='drug_exposures', db_column='drug_concept_id')
    drug_exposure_start_date = models.DateField()
    drug_exposure_start_datetime = models.DateTimeField(null=True, blank=True)
    drug_exposure_end_date = models.DateField(null=True, blank=True)
    drug_exposure_end_datetime = models.DateTimeField(null=True, blank=True)
    verbatim_end_date = models.DateField(null=True, blank=True)
    drug_type_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='drug_type_exposures', db_column='drug_type_concept_id')
    stop_reason = models.CharField(max_length=20, null=True, blank=True)
    refills = models.IntegerField(null=True, blank=True)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    days_supply = models.IntegerField(null=True, blank=True)
    sig = models.TextField(null=True, blank=True)
    route_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='drug_route_exposures', db_column='route_concept_id', null=True, blank=True)
    lot_number = models.CharField(max_length=50, null=True, blank=True)
    provider_id = models.IntegerField(null=True, blank=True)
    visit_occurrence = models.ForeignKey(VisitOccurrence, on_delete=models.SET_NULL, db_column='visit_occurrence_id', null=True, blank=True)
    visit_detail_id = models.IntegerField(null=True, blank=True)
    drug_source_value = models.CharField(max_length=50, null=True, blank=True)
    drug_source_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='drug_source_exposures', db_column='drug_source_concept_id', null=True, blank=True)
    route_source_value = models.CharField(max_length=50, null=True, blank=True)
    dose_unit_source_value = models.CharField(max_length=50, null=True, blank=True)

    class Meta:
        db_table = 'drug_exposure'

    def __str__(self):
        return f"Drug Exposure {self.drug_exposure_id} for Person {self.person_id}"


class ProcedureOccurrence(models.Model):
    """OMOP CDM Procedure Occurrence table - medical procedures and therapies."""
    procedure_occurrence_id = models.BigIntegerField(primary_key=True)
    person = models.ForeignKey(Person, on_delete=models.CASCADE, db_column='person_id')
    procedure_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='procedure_occurrences', db_column='procedure_concept_id')
    procedure_date = models.DateField()
    procedure_datetime = models.DateTimeField(null=True, blank=True)
    procedure_end_date = models.DateField(null=True, blank=True)
    procedure_end_datetime = models.DateTimeField(null=True, blank=True)
    procedure_type_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='procedure_types', db_column='procedure_type_concept_id')
    modifier_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='procedure_modifiers', db_column='modifier_concept_id', null=True, blank=True)
    quantity = models.IntegerField(null=True, blank=True)
    provider_id = models.IntegerField(null=True, blank=True)
    visit_occurrence = models.ForeignKey(VisitOccurrence, on_delete=models.SET_NULL, db_column='visit_occurrence_id', null=True, blank=True)
    visit_detail_id = models.IntegerField(null=True, blank=True)
    procedure_source_value = models.CharField(max_length=50, null=True, blank=True)
    procedure_source_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='procedure_sources', db_column='procedure_source_concept_id', null=True, blank=True)
    modifier_source_value = models.CharField(max_length=50, null=True, blank=True)

    class Meta:
        db_table = 'procedure_occurrence'

    def __str__(self):
        return f"Procedure {self.procedure_occurrence_id} for Person {self.person_id}"


class Measurement(models.Model):
    """OMOP CDM Measurement table - laboratory tests and vital signs."""
    measurement_id = models.BigIntegerField(primary_key=True)
    person = models.ForeignKey(Person, on_delete=models.CASCADE, db_column='person_id')
    measurement_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='measurements', db_column='measurement_concept_id')
    measurement_date = models.DateField()
    measurement_datetime = models.DateTimeField(null=True, blank=True)
    measurement_time = models.CharField(max_length=10, null=True, blank=True)
    measurement_type_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='measurement_types', db_column='measurement_type_concept_id')
    operator_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='measurement_operators', db_column='operator_concept_id', null=True, blank=True)
    value_as_number = models.DecimalField(max_digits=15, decimal_places=5, null=True, blank=True)
    value_as_string = models.CharField(max_length=60, null=True, blank=True)
    value_as_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='measurement_values', db_column='value_as_concept_id', null=True, blank=True)
    qualifier_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='measurement_qualifiers', db_column='qualifier_concept_id', null=True, blank=True)
    unit_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='measurement_units', db_column='unit_concept_id', null=True, blank=True)
    range_low = models.DecimalField(max_digits=15, decimal_places=5, null=True, blank=True)
    range_high = models.DecimalField(max_digits=15, decimal_places=5, null=True, blank=True)
    provider_id = models.IntegerField(null=True, blank=True)
    visit_occurrence = models.ForeignKey(VisitOccurrence, on_delete=models.SET_NULL, db_column='visit_occurrence_id', null=True, blank=True)
    visit_detail_id = models.IntegerField(null=True, blank=True)
    measurement_source_value = models.CharField(max_length=50, null=True, blank=True)
    measurement_source_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='measurement_sources', db_column='measurement_source_concept_id', null=True, blank=True)
    unit_source_value = models.CharField(max_length=50, null=True, blank=True)
    unit_source_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='measurement_unit_sources', db_column='unit_source_concept_id', null=True, blank=True)
    qualifier_source_value = models.CharField(max_length=50, null=True, blank=True)
    value_source_value = models.CharField(max_length=50, null=True, blank=True)
    measurement_event_id = models.BigIntegerField(null=True, blank=True)
    meas_event_field_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='measurement_event_fields', db_column='meas_event_field_concept_id', null=True, blank=True)

    class Meta:
        db_table = 'measurement'
        indexes = [
            models.Index(
                fields=['person', 'measurement_concept', 'measurement_date'],
                name='ix_meas_person_concept_date',
            ),
            models.Index(
                fields=['person', 'measurement_source_concept', 'measurement_date'],
                name='ix_meas_person_srcconcept_date',
            ),
        ]

    def __str__(self):
        return f"Measurement {self.measurement_id} for Person {self.person_id}"


class MeasurementOwnership(models.Model):
    """Tracks which VisitOccurrences (uploads) contributed to a Measurement.

    When the same lab result is uploaded multiple times, dedup reuses the
    existing Measurement but adds an ownership record for each visit.
    A Measurement is only deleted when its last ownership record is removed.
    """
    measurement_id = models.BigIntegerField()
    visit_occurrence_id = models.BigIntegerField()

    class Meta:
        db_table = 'measurement_ownership'
        unique_together = [('measurement_id', 'visit_occurrence_id')]
        indexes = [
            models.Index(fields=['visit_occurrence_id'], name='ix_measown_visit'),
        ]


class Observation(models.Model):
    """OMOP CDM Observation table - clinical facts that don't fit other domains."""
    observation_id = models.BigIntegerField(primary_key=True)
    person = models.ForeignKey(Person, on_delete=models.CASCADE, db_column='person_id')
    observation_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='observations', db_column='observation_concept_id')
    observation_date = models.DateField()
    observation_datetime = models.DateTimeField(null=True, blank=True)
    observation_type_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='observation_types', db_column='observation_type_concept_id')
    value_as_number = models.DecimalField(max_digits=15, decimal_places=5, null=True, blank=True)
    value_as_string = models.CharField(max_length=60, null=True, blank=True)
    value_as_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='observation_values', db_column='value_as_concept_id', null=True, blank=True)
    qualifier_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='observation_qualifiers', db_column='qualifier_concept_id', null=True, blank=True)
    unit_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='observation_units', db_column='unit_concept_id', null=True, blank=True)
    provider_id = models.IntegerField(null=True, blank=True)
    visit_occurrence = models.ForeignKey(VisitOccurrence, on_delete=models.SET_NULL, db_column='visit_occurrence_id', null=True, blank=True)
    visit_detail_id = models.IntegerField(null=True, blank=True)
    observation_source_value = models.CharField(max_length=50, null=True, blank=True)
    observation_source_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='observation_sources', db_column='observation_source_concept_id', null=True, blank=True)
    unit_source_value = models.CharField(max_length=50, null=True, blank=True)
    qualifier_source_value = models.CharField(max_length=50, null=True, blank=True)
    value_source_value = models.CharField(max_length=50, null=True, blank=True)
    observation_event_id = models.BigIntegerField(null=True, blank=True)
    obs_event_field_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='observation_event_fields', db_column='obs_event_field_concept_id', null=True, blank=True)

    class Meta:
        db_table = 'observation'

    def __str__(self):
        return f"Observation {self.observation_id} for Person {self.person_id}"


class CareSite(models.Model):
    """OMOP CDM Care Site table — healthcare delivery locations (labs, clinics)."""
    care_site_id = models.BigIntegerField(primary_key=True)
    care_site_name = models.CharField(max_length=255, null=True, blank=True)
    place_of_service_concept = models.ForeignKey(
        Concept, on_delete=models.PROTECT,
        related_name='care_sites', db_column='place_of_service_concept_id',
        null=True, blank=True,
    )
    location = models.ForeignKey(
        Location, on_delete=models.SET_NULL,
        db_column='location_id', null=True, blank=True,
    )
    care_site_source_value = models.CharField(max_length=50, null=True, blank=True)
    place_of_service_source_value = models.CharField(max_length=50, null=True, blank=True)

    class Meta:
        db_table = 'care_site'

    def __str__(self):
        return f"CareSite {self.care_site_id}: {self.care_site_name}"


class LoincClass(models.Model):
    """LOINC CLASS → display name mapping from LoincClass.csv (loinc.org archive)."""
    code = models.CharField(max_length=64, primary_key=True)
    display_name = models.CharField(max_length=128)

    class Meta:
        db_table = 'loinc_class'

    def __str__(self):
        return f"{self.code}: {self.display_name}"


class LoincCodeClass(models.Model):
    """Maps LOINC codes to their CLASS value (from Loinc.csv)."""
    loinc_num = models.CharField(max_length=20, primary_key=True)
    loinc_class = models.ForeignKey(
        LoincClass, on_delete=models.CASCADE,
        db_column='loinc_class_code', to_field='code',
    )

    class Meta:
        db_table = 'loinc_code_class'

    def __str__(self):
        return f"{self.loinc_num} → {self.loinc_class_id}"


# Choice classes for PatientInfo model
class GenderChoices(models.TextChoices):
    """Gender choices for PatientInfo"""
    MALE = 'M', 'Male'
    FEMALE = 'F', 'Female'
    UNKNOWN = 'U', 'Unknown'


class WeightUnits(models.TextChoices):
    """Weight unit choices"""
    KG = 'kg', 'Kilograms'
    LB = 'lb', 'Pounds'


class HeightUnits(models.TextChoices):
    """Height unit choices"""
    CM = 'cm', 'Centimeters'
    IN = 'in', 'Inches'


class HemoglobinUnits(models.TextChoices):
    """Hemoglobin unit choices"""
    G_DL = 'G/DL', 'g/dL'
    G_L = 'G/L', 'g/L'
    MMOL_L = 'MMOL/L', 'mmol/L'


class PlateletCountUnits(models.TextChoices):
    """Platelet count unit choices"""
    CELLS_UL = 'CELLS/UL', '10^3/μL'
    CELLS_L = 'CELLS/L', '10^9/L'


class SerumCalciumUnits(models.TextChoices):
    """Serum calcium unit choices"""
    MG_DL = 'MG/DL', 'mg/dL'
    MMOL_L = 'MMOL/L', 'mmol/L'


class SerumCreatinineUnits(models.TextChoices):
    """Serum creatinine unit choices"""
    MG_DL = 'MG/DL', 'mg/dL'
    UMOL_L = 'UMOL/L', 'μmol/L'


class SerumBilirubinUnits(models.TextChoices):
    """Serum bilirubin unit choices"""
    MG_DL = 'MG/DL', 'mg/dL'
    UMOL_L = 'UMOL/L', 'μmol/L'


class AlbuminUnits(models.TextChoices):
    """Albumin unit choices"""
    G_DL = 'G/DL', 'g/dL'
    G_L = 'G/L', 'g/L'


# ---------------------------------------------------------------------------
# Controlled vocabulary / lookup models (parity with cancerbot)
# Each model mirrors cancerbot's OptionsListMixin: code + title + llm_hint
# ---------------------------------------------------------------------------

class VocabularyLookup(models.Model):
    """Abstract base for all controlled-vocabulary lookup tables."""
    code = models.TextField(blank=False, null=False, db_index=True, unique=True)
    title = models.TextField(blank=False, null=False, db_index=True, unique=True)
    llm_hint = models.TextField(blank=True, null=True)
    source_name = models.TextField(blank=True, null=True, help_text="Authoritative vocabulary source name")
    source_url  = models.TextField(blank=True, null=True, help_text="URL to the vocabulary standard")

    class Meta:
        abstract = True

    def __str__(self):
        return self.title


class Ethnicity(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_ethnicity'


class StemCellTransplant(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_stem_cell_transplant'


class SctEligibility(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_sct_eligibility'


class HistologicType(VocabularyLookup):
    sort_key = models.IntegerField(blank=True, null=True)

    class Meta:
        db_table = 'vocabulary_histologic_type'
        ordering = ['sort_key']


class EstrogenReceptorStatus(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_estrogen_receptor_status'


class ProgesteroneReceptorStatus(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_progesterone_receptor_status'


class Her2Status(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_her2_status'


class HrStatus(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_hr_status'


class HrdStatus(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_hrd_status'


class MutationOrigin(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_mutation_origin'


class MutationGene(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_mutation_gene'


class MutationInterpretation(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_mutation_interpretation'


class MutationCode(VocabularyLookup):
    gene = models.ForeignKey(
        MutationGene,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='mutation_codes',
    )

    class Meta:
        db_table = 'vocabulary_mutation_code'


class TumorStage(VocabularyLookup):
    sort_key = models.IntegerField(blank=True, null=True)

    class Meta:
        db_table = 'vocabulary_tumor_stage'
        ordering = ['sort_key']


class NodesStage(VocabularyLookup):
    sort_key = models.IntegerField(blank=True, null=True)

    class Meta:
        db_table = 'vocabulary_nodes_stage'
        ordering = ['sort_key']


class DistantMetastasisStage(VocabularyLookup):
    sort_key = models.IntegerField(blank=True, null=True)

    class Meta:
        db_table = 'vocabulary_distant_metastasis_stage'
        ordering = ['sort_key']


class StagingModality(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_staging_modality'


class ToxicityGrade(models.Model):
    """Toxicity grade uses integer codes, not text codes."""
    code = models.IntegerField(blank=False, null=False, db_index=True, unique=True)
    title = models.TextField(blank=False, null=False, db_index=True, unique=True)
    llm_hint = models.TextField(blank=True, null=True)
    source_name = models.TextField(blank=True, null=True, help_text="Authoritative vocabulary source name")
    source_url  = models.TextField(blank=True, null=True, help_text="URL to the vocabulary standard")

    class Meta:
        db_table = 'vocabulary_toxicity_grade'
        ordering = ['code']

    def __str__(self):
        return self.title


class Language(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_language'


class LanguageSkillLevel(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_language_skill_level'


class BinetStage(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_binet_stage'


class ProteinExpression(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_protein_expression'


class RichterTransformation(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_richter_transformation'


class TumorBurden(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_tumor_burden'


class MorphologicVariant(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_morphologic_variant'


class DiseaseActivity(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_disease_activity'


class PreExistingConditionCategory(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_pre_existing_condition_category'


class Disease(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_disease'


class CancerStage(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_cancer_stage'


class KarnofskyScore(VocabularyLookup):
    """Karnofsky performance score — integer codes stored as text; sort_key orders numerically."""
    sort_key = models.IntegerField(blank=True, null=True)

    class Meta:
        db_table = 'vocabulary_karnofsky_score'
        ordering = ['sort_key']


class EcogStatus(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_ecog_status'


class PeripheralNeuropathyGrade(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_peripheral_neuropathy_grade'


class InfectionStatus(VocabularyLookup):
    """Shared vocabulary for HIV, Hepatitis B, and Hepatitis C status fields."""
    class Meta:
        db_table = 'vocabulary_infection_status'


class DiseaseProgression(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_disease_progression'


class MeasurableDisease(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_measurable_disease'


class GelfCriteria(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_gelf_criteria'


class FlipIScore(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_flipi_score'


class FollicularLymphomaGrade(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_follicular_lymphoma_grade'


class BreastCancerFirstLineTherapy(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_breast_cancer_first_line_therapy'


class BreastCancerSecondLineTherapy(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_breast_cancer_second_line_therapy'


class BreastCancerLaterLineTherapy(VocabularyLookup):
    class Meta:
        db_table = 'vocabulary_breast_cancer_later_line_therapy'


# ---------------------------------------------------------------------------
# End controlled vocabulary models
# ---------------------------------------------------------------------------


class PatientInfo(models.Model):
    """
    Comprehensive patient information model adapted from exactomop repository
    Integrated with OMOP CDM Person model for clinical trial matching and research
    """
    # Link to OMOP Person
    person = models.OneToOneField(Person, on_delete=models.CASCADE, related_name='patient_info')
    
    # General Information
    email = models.EmailField(max_length=255, null=True, blank=True, db_index=True)
    date_of_birth = models.DateField(null=True, blank=True)
    
    # Demographics
    patient_age = models.IntegerField(help_text="What is the patient's age?", blank=True, null=True)
    gender = models.CharField(
        max_length=2,
        choices=GenderChoices.choices,
        blank=True,
        null=True,
        help_text="Patient's gender"
    )
    weight = models.FloatField(help_text="Patient's weight", blank=True, null=True)
    weight_units = models.CharField(
        max_length=2,
        choices=WeightUnits.choices,
        blank=True,
        null=True,
        default='kg',
        help_text="Units for the patient's weight"
    )
    height = models.FloatField(help_text="Patient's height", blank=True, null=True)
    height_units = models.CharField(
        max_length=2,
        choices=HeightUnits.choices,
        blank=True,
        null=True,
        default='cm',
        help_text="Units for the patient's height"
    )
    bmi = models.FloatField(editable=False, help_text="Patient's BMI (computed)", blank=True, null=True)
    race = models.TextField(blank=True, null=True)
    ethnicity = models.TextField(blank=True, null=True)
    systolic_blood_pressure = models.IntegerField(help_text="Patient's systolic blood pressure", blank=True, null=True)
    diastolic_blood_pressure = models.IntegerField(help_text="Patient's diastolic blood pressure", blank=True, null=True)

    # Geographic location
    country = models.CharField(max_length=255, blank=True, null=True)
    region = models.CharField(max_length=255, blank=True, null=True)
    city = models.CharField(max_length=255, blank=True, null=True)
    postal_code = models.CharField(max_length=20, blank=True, null=True)
    longitude = models.FloatField(blank=True, null=True)
    latitude = models.FloatField(blank=True, null=True)

    # Disease information
    disease = models.TextField(blank=True, null=True)
    stage = models.TextField(blank=True, null=True)
    karnofsky_performance_score = models.IntegerField(blank=True, null=True, default=100)
    ecog_performance_status = models.IntegerField(blank=True, null=True)
    no_other_active_malignancies = models.BooleanField(blank=False, null=False, default=True)
    no_pre_existing_conditions = models.BooleanField(blank=True, null=True)
    preexisting_conditions = models.JSONField(blank=True, null=True, default=list, help_text="List of pre-existing condition categories from PreExistingConditionCategory vocabulary")
    peripheral_neuropathy_grade = models.IntegerField(blank=True, null=True)

    # Cancer-specific fields
    cytogenic_markers = models.TextField(blank=True, null=True)
    molecular_markers = models.TextField(blank=True, null=True)
    stem_cell_transplant_history = models.JSONField(blank=True, null=True, default=list)
    sct_date = models.DateField(blank=True, null=True)
    sct_eligibility = models.JSONField(blank=True, null=True, default=list,
        help_text="Multi-select from SctEligibility vocabulary")
    plasma_cell_leukemia = models.BooleanField(blank=True, null=True, default=None)
    progression = models.TextField(blank=True, null=True)

    # Vital signs
    heartrate = models.IntegerField(help_text="Patient's heart rate", blank=True, null=True)
    heartrate_variability = models.IntegerField(help_text="Patient's heart rate variability", blank=True, null=True)

    # Legacy condition codes
    condition_code_icd_10 = models.TextField(blank=True, null=True)
    condition_code_snomed_ct = models.TextField(blank=True, null=True)

    # Treatment history
    prior_therapy = models.TextField(blank=True, null=True)
    first_line_therapy = models.TextField(blank=True, null=True)
    first_line_date = models.DateField(blank=True, null=True)
    first_line_start_date = models.DateField(blank=True, null=True, help_text="First Line Therapy Start Date")
    first_line_end_date = models.DateField(blank=True, null=True, help_text="First Line Therapy End Date")
    first_line_outcome = models.TextField(blank=True, null=True)
    first_line_intent = models.CharField(max_length=50, blank=True, null=True, help_text="First Line Therapy Intent (Adjuvant/Neoadjuvant/Metastatic)")
    first_line_discontinuation_reason = models.CharField(max_length=50, blank=True, null=True, help_text="First Line Reason for Discontinuation (Progression/Toxicity/Completion)")
    second_line_therapy = models.TextField(blank=True, null=True)
    second_line_date = models.DateField(blank=True, null=True)
    second_line_start_date = models.DateField(blank=True, null=True, help_text="Second Line Therapy Start Date")
    second_line_end_date = models.DateField(blank=True, null=True, help_text="Second Line Therapy End Date")
    second_line_outcome = models.TextField(blank=True, null=True)
    second_line_intent = models.CharField(max_length=50, blank=True, null=True, help_text="Second Line Therapy Intent (Adjuvant/Neoadjuvant/Metastatic)")
    second_line_discontinuation_reason = models.CharField(max_length=50, blank=True, null=True, help_text="Second Line Reason for Discontinuation (Progression/Toxicity/Completion)")
    later_therapy = models.TextField(blank=True, null=True)
    # HemOnc concept_id references for therapy lines
    first_line_therapy_id = models.BigIntegerField(
        null=True, blank=True,
        help_text="HemOnc concept_id for first-line regimen",
        db_index=True,
    )
    second_line_therapy_id = models.BigIntegerField(
        null=True, blank=True,
        help_text="HemOnc concept_id for second-line regimen",
        db_index=True,
    )
    later_therapy_ids = models.JSONField(
        null=True, blank=True, default=list,
        help_text="List of HemOnc concept_ids for later-line regimens (3L+)",
    )
    later_date = models.DateField(blank=True, null=True)
    later_start_date = models.DateField(blank=True, null=True, help_text="Later Line Therapy Start Date")
    later_end_date = models.DateField(blank=True, null=True, help_text="Later Line Therapy End Date")
    later_outcome = models.TextField(blank=True, null=True)
    later_intent = models.CharField(max_length=50, blank=True, null=True, help_text="Later Line Therapy Intent (Adjuvant/Neoadjuvant/Metastatic)")
    later_discontinuation_reason = models.CharField(max_length=50, blank=True, null=True, help_text="Later Line Reason for Discontinuation (Progression/Toxicity/Completion)")
    supportive_therapies = models.TextField(blank=True, null=True)
    supportive_therapy_date = models.DateField(blank=True, null=True)
    supportive_therapy_start_date = models.DateField(blank=True, null=True, help_text="Supportive Therapy Start Date")
    supportive_therapy_end_date = models.DateField(blank=True, null=True, help_text="Supportive Therapy End Date")
    supportive_therapy_intent = models.CharField(max_length=50, blank=True, null=True, help_text="Supportive Therapy Intent")
    relapse_count = models.IntegerField(blank=True, null=True)
    treatment_refractory_status = models.CharField(max_length=255, blank=True, null=True)

    # Legacy therapy fields
    therapy_lines_count = models.IntegerField(blank=True, null=True)
    line_of_therapy = models.TextField(blank=True, null=True)

    # Blood work with units
    absolute_neutrophile_count = models.DecimalField(decimal_places=2, max_digits=10, blank=True, null=True)
    absolute_neutrophile_count_units = models.CharField(
        max_length=10,
        choices=PlateletCountUnits.choices,
        blank=True,
        null=True,
        default='CELLS/UL'
    )
    platelet_count = models.IntegerField(blank=True, null=True)
    platelet_count_units = models.CharField(
        max_length=10,
        choices=PlateletCountUnits.choices,
        blank=True,
        null=True,
        default='CELLS/UL'
    )
    white_blood_cell_count = models.DecimalField(decimal_places=2, max_digits=10, blank=True, null=True)
    white_blood_cell_count_units = models.CharField(
        max_length=10,
        choices=PlateletCountUnits.choices,
        blank=True,
        null=True,
        default='CELLS/L'
    )
    red_blood_cell_count = models.DecimalField(decimal_places=2, max_digits=10, blank=True, null=True)
    red_blood_cell_count_units = models.CharField(
        max_length=10,
        choices=PlateletCountUnits.choices,
        blank=True,
        null=True,
        default='CELLS/L'
    )

    serum_calcium_level = models.DecimalField(decimal_places=2, max_digits=10, blank=True, null=True)
    serum_calcium_level_units = models.CharField(
        max_length=15,
        choices=SerumCalciumUnits.choices,
        blank=True,
        null=True,
        default='MG/DL'
    )
    creatinine_clearance_rate = models.IntegerField(blank=True, null=True)
    serum_creatinine_level = models.DecimalField(decimal_places=2, max_digits=10, blank=True, null=True)
    serum_creatinine_level_units = models.CharField(
        max_length=15,
        choices=SerumCreatinineUnits.choices,
        blank=True,
        null=True,
        default='MG/DL'
    )
    hemoglobin_level = models.DecimalField(decimal_places=2, max_digits=10, blank=True, null=True)
    hemoglobin_level_units = models.CharField(
        max_length=10,
        choices=HemoglobinUnits.choices,
        blank=True,
        null=True,
        default='G/DL'
    )

    # Blood count fields (simplified names for UI)
    hemoglobin_g_dl = models.DecimalField(decimal_places=1, max_digits=5, blank=True, null=True, help_text="Hemoglobin (g/dL)")
    hematocrit_percent = models.DecimalField(decimal_places=1, max_digits=5, blank=True, null=True, help_text="Hematocrit (%)")
    wbc_count_thousand_per_ul = models.DecimalField(decimal_places=1, max_digits=6, blank=True, null=True, help_text="White Blood Cell Count (10³/µL)")
    rbc_million_per_ul = models.DecimalField(decimal_places=2, max_digits=5, blank=True, null=True, help_text="Red Blood Cell Count (10⁶/µL)")
    platelet_count_thousand_per_ul = models.DecimalField(decimal_places=1, max_digits=6, blank=True, null=True, help_text="Platelet Count (10³/µL)")
    anc_thousand_per_ul = models.DecimalField(decimal_places=1, max_digits=6, blank=True, null=True, help_text="Absolute Neutrophil Count (10³/µL)")
    alc_thousand_per_ul = models.DecimalField(decimal_places=1, max_digits=6, blank=True, null=True, help_text="Absolute Lymphocyte Count (10³/µL)")
    amc_thousand_per_ul = models.DecimalField(decimal_places=1, max_digits=6, blank=True, null=True, help_text="Absolute Monocyte Count (10³/µL)")
    
    # Additional kidney and electrolyte fields (simplified names for UI)
    serum_calcium_mg_dl = models.DecimalField(decimal_places=1, max_digits=5, blank=True, null=True, help_text="Serum Calcium (mg/dL)")
    serum_creatinine_mg_dl = models.DecimalField(decimal_places=2, max_digits=5, blank=True, null=True, help_text="Serum Creatinine (mg/dL)")
    creatinine_clearance_ml_min = models.DecimalField(decimal_places=1, max_digits=6, blank=True, null=True, help_text="Creatinine Clearance (mL/min)")
    creatinine_mg_dl = models.DecimalField(decimal_places=2, max_digits=5, blank=True, null=True, help_text="Creatinine (mg/dL)")
    egfr_ml_min_173m2 = models.DecimalField(decimal_places=1, max_digits=6, blank=True, null=True, help_text="eGFR (mL/min/1.73m²)")
    bun_mg_dl = models.DecimalField(decimal_places=1, max_digits=5, blank=True, null=True, help_text="Blood Urea Nitrogen (mg/dL)")
    sodium_meq_l = models.DecimalField(decimal_places=1, max_digits=5, blank=True, null=True, help_text="Sodium (mEq/L)")
    potassium_meq_l = models.DecimalField(decimal_places=1, max_digits=5, blank=True, null=True, help_text="Potassium (mEq/L)")
    calcium_mg_dl = models.DecimalField(decimal_places=1, max_digits=5, blank=True, null=True, help_text="Calcium (mg/dL)")
    magnesium_mg_dl = models.DecimalField(decimal_places=1, max_digits=5, blank=True, null=True, help_text="Magnesium (mg/dL)")
    
    # Liver function and other lab tests (simplified names for UI)
    bilirubin_total_mg_dl = models.DecimalField(decimal_places=1, max_digits=5, blank=True, null=True, help_text="Total Bilirubin (mg/dL)")
    alt_u_l = models.IntegerField(blank=True, null=True, help_text="ALT (U/L)")
    ast_u_l = models.IntegerField(blank=True, null=True, help_text="AST (U/L)")
    alkaline_phosphatase_u_l = models.IntegerField(blank=True, null=True, help_text="Alkaline Phosphatase (U/L)")
    albumin_g_dl = models.DecimalField(decimal_places=1, max_digits=5, blank=True, null=True, help_text="Albumin (g/dL)")
    troponin_ng_ml = models.DecimalField(decimal_places=3, max_digits=7, blank=True, null=True, help_text="Troponin (ng/mL)")
    bnp_pg_ml = models.IntegerField(blank=True, null=True, help_text="BNP (pg/mL)")
    glucose_mg_dl = models.IntegerField(blank=True, null=True, help_text="Glucose (mg/dL)")
    hba1c_percent = models.DecimalField(decimal_places=1, max_digits=4, blank=True, null=True, help_text="HbA1c (%)")
    inr = models.DecimalField(decimal_places=2, max_digits=5, blank=True, null=True, help_text="INR")
    pt_seconds = models.DecimalField(decimal_places=1, max_digits=5, blank=True, null=True, help_text="PT (seconds)")
    ptt_seconds = models.DecimalField(decimal_places=1, max_digits=5, blank=True, null=True, help_text="PTT (seconds)")
    cea_ng_ml = models.DecimalField(decimal_places=1, max_digits=8, blank=True, null=True, help_text="CEA (ng/mL)")
    ca19_9_u_ml = models.DecimalField(decimal_places=1, max_digits=8, blank=True, null=True, help_text="CA 19-9 (U/mL)")
    psa_ng_ml = models.DecimalField(decimal_places=2, max_digits=7, blank=True, null=True, help_text="PSA (ng/mL)")
    ldh_u_l = models.IntegerField(blank=True, null=True, help_text="LDH (U/L)")

    # Additional lab values
    bone_lesions = models.TextField(blank=True, null=True)
    meets_crab = models.BooleanField(blank=True, null=True)
    estimated_glomerular_filtration_rate = models.IntegerField(blank=True, null=True)
    renal_adequacy_status = models.BooleanField(blank=True, null=True)
    liver_enzyme_levels_ast = models.IntegerField(blank=True, null=True)
    liver_enzyme_levels_alt = models.IntegerField(blank=True, null=True)
    liver_enzyme_levels_alp = models.IntegerField(blank=True, null=True)
    serum_bilirubin_level_total = models.DecimalField(decimal_places=2, max_digits=10, blank=True, null=True)
    serum_bilirubin_level_total_units = models.CharField(
        max_length=15,
        choices=SerumBilirubinUnits.choices,
        blank=True,
        null=True,
        default='MG/DL'
    )
    serum_bilirubin_level_direct = models.DecimalField(decimal_places=2, max_digits=10, blank=True, null=True)
    serum_bilirubin_level_direct_units = models.CharField(
        max_length=15,
        choices=SerumBilirubinUnits.choices,
        blank=True,
        null=True,
        default='MG/DL'
    )
    albumin_level = models.DecimalField(decimal_places=2, max_digits=10, blank=True, null=True)
    albumin_level_units = models.CharField(
        max_length=15,
        choices=AlbuminUnits.choices,
        blank=True,
        null=True,
        default='G/DL'
    )
    
    # Additional Labs tab fields (Chemistry Panel)
    blood_urea_nitrogen = models.DecimalField(decimal_places=1, max_digits=5, blank=True, null=True, help_text="Blood Urea Nitrogen (mg/dL)")
    egfr = models.DecimalField(decimal_places=1, max_digits=6, blank=True, null=True, help_text="eGFR (mL/min/1.73m²)")
    serum_sodium = models.DecimalField(decimal_places=1, max_digits=5, blank=True, null=True, help_text="Serum Sodium (mEq/L)")
    serum_potassium = models.DecimalField(decimal_places=1, max_digits=5, blank=True, null=True, help_text="Serum Potassium (mEq/L)")
    total_protein = models.DecimalField(decimal_places=1, max_digits=5, blank=True, null=True, help_text="Total Protein (g/dL)")
    
    # Additional Labs tab fields (Other Markers)
    ldh_level = models.IntegerField(blank=True, null=True, help_text="LDH (U/L)")
    beta2_microglobulin = models.DecimalField(decimal_places=2, max_digits=6, blank=True, null=True, help_text="Beta-2 Microglobulin (mg/L)")
    c_reactive_protein = models.DecimalField(decimal_places=2, max_digits=6, blank=True, null=True, help_text="C-Reactive Protein (mg/L)")
    esr = models.IntegerField(blank=True, null=True, help_text="ESR (mm/hr)")
    
    kappa_flc = models.IntegerField(blank=True, null=True)
    lambda_flc = models.IntegerField(blank=True, null=True)
    meets_slim = models.BooleanField(blank=True, null=True)

    # Legacy blood work fields
    liver_enzyme_levels = models.IntegerField(blank=True, null=True)
    serum_bilirubin_level = models.DecimalField(decimal_places=2, max_digits=10, blank=True, null=True)

    # Laboratory results
    monoclonal_protein_serum = models.DecimalField(decimal_places=2, max_digits=10, blank=True, null=True)
    monoclonal_protein_urine = models.DecimalField(decimal_places=2, max_digits=10, blank=True, null=True)
    lactate_dehydrogenase_level = models.IntegerField(blank=True, null=True)
    pulmonary_function_test_result = models.BooleanField(blank=False, null=False, default=False)
    bone_imaging_result = models.BooleanField(blank=False, null=False, default=False)
    clonal_plasma_cells = models.IntegerField(blank=True, null=True)
    ejection_fraction = models.IntegerField(blank=True, null=True)

    # Behavioral and risk factors
    consent_capability = models.BooleanField(help_text="Does the patient have cognitive ability to consent?", blank=False, null=False, default=True)
    caregiver_availability_status = models.BooleanField(help_text="Is there an available caregiver for the patient?", blank=False, null=False, default=False)
    contraceptive_use = models.BooleanField(help_text="Does the patient use contraceptives?", blank=False, null=False, default=False)
    no_pregnancy_or_lactation_status = models.BooleanField(help_text="Does the patient self assess as not pregnant or lactating?", blank=False, null=False, default=True)
    pregnancy_test_result = models.BooleanField(help_text="Does the female patient of childbearing age have a negative test result for pregnancy?", blank=False, null=False, default=False)
    no_mental_health_disorder_status = models.BooleanField(help_text="Does the patient have a mental health disorder?", blank=False, null=False, default=True)
    no_concomitant_medication_status = models.BooleanField(help_text="Does the patient have concomitant medication?", blank=False, null=False, default=True)
    concomitant_medication_details = models.CharField(max_length=255, help_text="Details about the patient's concomitant medications", blank=True, null=True)
    
    # Behavior tab - Lifestyle Factors
    smoking_status = models.CharField(max_length=50, blank=True, null=True, help_text="Smoking Status (Never/Former/Current)")
    pack_years = models.DecimalField(decimal_places=1, max_digits=5, blank=True, null=True, help_text="Pack Years")
    alcohol_use = models.CharField(max_length=50, blank=True, null=True, help_text="Alcohol Use (None/Light/Moderate/Heavy)")
    drinks_per_week = models.IntegerField(blank=True, null=True, help_text="Drinks per Week")
    exercise_frequency = models.CharField(max_length=50, blank=True, null=True, help_text="Exercise Frequency")
    exercise_minutes_per_week = models.IntegerField(blank=True, null=True, help_text="Exercise Minutes per Week")
    diet_type = models.CharField(max_length=100, blank=True, null=True, help_text="Diet Type")
    
    # Behavior tab - Sleep & Wellbeing
    sleep_hours_per_night = models.DecimalField(decimal_places=1, max_digits=4, blank=True, null=True, help_text="Average Sleep Hours per Night")
    sleep_quality = models.CharField(max_length=50, blank=True, null=True, help_text="Sleep Quality")
    stress_level = models.CharField(max_length=50, blank=True, null=True, help_text="Stress Level")
    social_support = models.CharField(max_length=50, blank=True, null=True, help_text="Social Support")
    
    # Behavior tab - Socioeconomic Factors
    employment_status = models.CharField(max_length=50, blank=True, null=True, help_text="Employment Status")
    education_level = models.CharField(max_length=100, blank=True, null=True, help_text="Education Level")
    marital_status = models.CharField(max_length=50, blank=True, null=True, help_text="Marital Status")
    insurance_type = models.CharField(max_length=100, blank=True, null=True, help_text="Insurance Type")
    number_of_dependents = models.IntegerField(blank=True, null=True, help_text="Number of Dependents")
    annual_household_income = models.DecimalField(decimal_places=2, max_digits=12, blank=True, null=True, help_text="Annual Household Income (USD)")

    # Cancer Assessment Fields
    ecog_assessment_date = models.DateField(blank=True, null=True, help_text="ECOG Performance Status Assessment Date")
    test_methodology = models.CharField(max_length=50, blank=True, null=True, help_text="Test Methodology (NGS/IHC/FISH/PCR)")
    test_date = models.DateField(blank=True, null=True, help_text="Test Date")
    test_specimen_type = models.CharField(max_length=50, blank=True, null=True, help_text="Test Specimen Type (Primary/Metastatic Biopsy)")
    report_interpretation = models.CharField(max_length=50, blank=True, null=True, help_text="Report Interpretation (Positive/Negative/Indeterminate/Not Tested)")
    oncotype_dx_score = models.IntegerField(blank=True, null=True, help_text="Oncotype DX Score")
    androgen_receptor_status = models.CharField(max_length=50, blank=True, null=True, help_text="Androgen Receptor Status")
    
    # Treatment Fields
    therapy_intent = models.CharField(max_length=50, blank=True, null=True, help_text="Therapy Intent (Adjuvant/Neoadjuvant/Metastatic)")
    reason_for_discontinuation = models.CharField(max_length=100, blank=True, null=True, help_text="Reason for Discontinuation (Progression/Toxicity/Completion)")
    
    # Additional Lab Values
    ldh = models.IntegerField(blank=True, null=True, help_text="LDH (U/L)")
    alkaline_phosphatase = models.IntegerField(blank=True, null=True, help_text="Alkaline Phosphatase (U/L)")
    magnesium = models.DecimalField(decimal_places=1, max_digits=5, blank=True, null=True, help_text="Magnesium (mg/dL)")
    phosphorus = models.DecimalField(decimal_places=1, max_digits=5, blank=True, null=True, help_text="Phosphorus (mg/dL)")
    
    # Reproductive Health
    pregnancy_test_date = models.DateField(blank=True, null=True, help_text="Pregnancy Test Date")
    pregnancy_test_result_value = models.CharField(max_length=50, blank=True, null=True, help_text="Pregnancy Test Result (Positive/Negative)")

    no_tobacco_use_status = models.BooleanField(help_text="Does the patient use tobacco?", blank=False, null=False, default=True)
    tobacco_use_details = models.CharField(max_length=255, help_text="Details about the patient's tobacco use", blank=True, null=True)
    no_substance_use_status = models.BooleanField(help_text="Does the patient use substances?", blank=False, null=False, default=True)
    substance_use_details = models.CharField(max_length=255, help_text="Details about the patient's substance use", blank=True, null=True)
    no_geographic_exposure_risk = models.BooleanField(help_text="Has the patient had geographic exposure to risk?", blank=False, null=False, default=True)
    geographic_exposure_risk_details = models.CharField(max_length=255, help_text="Details about the patient's geographic exposure risk", blank=True, null=True)

    no_hiv_status = models.BooleanField(help_text="Does the patient has had HIV?", blank=False, null=False, default=True)
    no_hepatitis_b_status = models.BooleanField(help_text="Does the patient has had Hepatitis B (HBV)?", blank=False, null=False, default=True)
    no_hepatitis_c_status = models.BooleanField(help_text="Does the patient has had Hepatitis C (HCV)?", blank=False, null=False, default=True)
    no_active_infection_status = models.BooleanField(help_text="Does the patient has any active infection?", blank=False, null=False, default=True)

    concomitant_medications = models.TextField(blank=True, null=True)
    concomitant_medication_date = models.DateField(blank=True, null=True)

    # Wearable summary fields (derived from OMOP Measurement/Observation, 30-day window)
    wearable_last_sync_at = models.DateTimeField(blank=True, null=True, help_text="Latest wearable sample timestamp")
    wearable_coverage_ratio_30d = models.DecimalField(max_digits=4, decimal_places=2, blank=True, null=True, help_text="Valid wearable days / 30 (data quality indicator)")
    median_daily_steps_30d = models.IntegerField(blank=True, null=True, help_text="Median daily step count over valid days in last 30 days")
    active_minutes_per_day_30d = models.DecimalField(max_digits=5, decimal_places=1, blank=True, null=True, help_text="Mean daily active/exercise minutes over last 30 days")
    activity_trend_30d = models.CharField(max_length=20, blank=True, null=True, help_text="Activity trend: improving, stable, declining, or insufficient_data")
    resting_heart_rate_avg_30d = models.IntegerField(blank=True, null=True, help_text="Mean resting heart rate over last 30 days")
    hrv_sdnn_avg_30d = models.DecimalField(max_digits=6, decimal_places=1, blank=True, null=True, help_text="Mean HRV SDNN over last 30 days (ms)")
    oxygen_saturation_min_30d = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True, help_text="Minimum valid SpO2 reading over last 30 days (%)")
    respiratory_rate_avg_30d = models.DecimalField(max_digits=5, decimal_places=1, blank=True, null=True, help_text="Mean respiratory rate over last 30 days (breaths/min)")
    sleep_duration_hours_avg_30d = models.DecimalField(max_digits=4, decimal_places=1, blank=True, null=True, help_text="Mean nightly sleep duration over last 30 days (hours)")

    # Remission and washout periods
    remission_duration_min = models.TextField(blank=True, null=True)
    washout_period_duration = models.TextField(blank=True, null=True)

    # Viral infection status
    hiv_status = models.BooleanField(blank=True, null=True)
    hepatitis_b_status = models.BooleanField(blank=True, null=True)
    hepatitis_c_status = models.BooleanField(blank=True, null=True)

    # Treatment dates
    last_treatment = models.DateField(help_text="Date and time of the last treatment", blank=True, null=True)

    # Breast cancer specific fields
    bone_only_metastasis_status = models.BooleanField(blank=True, null=True)
    menopausal_status = models.TextField(blank=True, null=True)
    metastatic_status = models.BooleanField(blank=True, null=True)
    toxicity_grade = models.IntegerField(blank=True, null=True)
    planned_therapies = models.TextField(blank=True, null=True)

    # Biopsy results
    histologic_type = models.TextField(blank=True, null=True)
    biopsy_grade_depr = models.TextField(blank=True, null=True)
    biopsy_grade = models.IntegerField(blank=True, null=True)
    measurable_disease_by_recist_status = models.BooleanField(blank=True, null=True)
    estrogen_receptor_status = models.TextField(blank=True, null=True)
    progesterone_receptor_status = models.TextField(blank=True, null=True)
    her2_status = models.TextField(blank=True, null=True)
    tnbc_status = models.BooleanField(blank=True, null=True)
    hrd_status = models.TextField(blank=True, null=True)
    hr_status = models.TextField(blank=True, null=True)
    
    # Tumor characteristics
    tumor_size = models.FloatField(blank=True, null=True, help_text="Tumor size in cm")
    lymph_node_status = models.CharField(max_length=50, blank=True, null=True, help_text="Lymph node status (Positive/Negative/Unknown)")
    metastasis_status = models.CharField(max_length=50, blank=True, null=True, help_text="Metastasis status (Positive/Negative/Unknown)")

    tumor_stage = models.TextField(blank=True, null=True)
    nodes_stage = models.TextField(blank=True, null=True)
    distant_metastasis_stage = models.TextField(blank=True, null=True)
    staging_modalities = models.TextField(blank=True, null=True)

    # Genetic mutations
    genetic_mutations = models.JSONField(blank=True, null=False, default=list)

    # PD-L1 and biomarkers
    pd_l1_tumor_cells = models.IntegerField(blank=True, null=True)
    pd_l1_assay = models.TextField(blank=True, null=True)
    pd_l1_ic_percentage = models.IntegerField(blank=True, null=True)
    pd_l1_combined_positive_score = models.IntegerField(blank=True, null=True)
    ki67_proliferation_index = models.IntegerField(blank=True, null=True)

    # Languages (denormalized from PersonLanguageSkill for API consumption)
    languages_skills = models.TextField(blank=True, null=True)

    # Lymphoma (Follicular Lymphoma)
    gelf_criteria_status = models.TextField(blank=True, null=True)
    flipi_score = models.IntegerField(blank=True, null=True)
    flipi_score_options = models.TextField(blank=True, null=True)
    tumor_grade = models.IntegerField(blank=True, null=True)

    # Measurable disease
    measurable_disease_imwg = models.BooleanField(blank=True, null=True)
    mrd_status = models.CharField(max_length=50, blank=True, null=True)

    # Later therapies (structured list, mirrors cancerbot JSONField)
    later_therapies = models.JSONField(blank=True, null=False, default=list)

    # CLL (Chronic Lymphocytic Leukemia)
    binet_stage = models.TextField(blank=True, null=True)
    protein_expressions = models.TextField(blank=True, null=True)
    richter_transformation = models.TextField(blank=True, null=True)
    tumor_burden = models.TextField(blank=True, null=True)
    lymphocyte_doubling_time = models.IntegerField(blank=True, null=True)
    tp53_disruption = models.BooleanField(blank=True, null=True)
    measurable_disease_iwcll = models.BooleanField(blank=True, null=True)
    hepatomegaly = models.BooleanField(blank=True, null=True)
    autoimmune_cytopenias_refractory_to_steroids = models.BooleanField(blank=True, null=True)
    lymphadenopathy = models.BooleanField(blank=True, null=True)
    largest_lymph_node_size = models.FloatField(blank=True, null=True)
    splenomegaly = models.BooleanField(blank=True, null=True)
    spleen_size = models.FloatField(blank=True, null=True)
    disease_activity = models.TextField(blank=True, null=True)
    btk_inhibitor_refractory = models.BooleanField(blank=True, null=True)
    bcl2_inhibitor_refractory = models.BooleanField(blank=True, null=True)
    absolute_lymphocyte_count = models.FloatField(blank=True, null=True)
    qtcf_value = models.FloatField(blank=True, null=True)
    serum_beta2_microglobulin_level = models.FloatField(blank=True, null=True)
    clonal_b_lymphocyte_count = models.IntegerField(blank=True, null=True)
    clonal_bone_marrow_b_lymphocytes = models.FloatField(blank=True, null=True, help_text="Clonal B lymphocytes in bone marrow biopsy (%)")
    bone_marrow_involvement = models.BooleanField(blank=True, null=True)

    # HealthTree parity fields
    diagnosis_date = models.DateField(blank=True, null=True, help_text="Date of initial diagnosis (from ConditionOccurrence)")
    condition_clinical_status = models.CharField(max_length=50, blank=True, null=True, help_text="Clinical status: active/remission/relapse")
    disease_slug = models.CharField(max_length=100, blank=True, null=True, help_text="Machine-readable disease ID e.g. 'multiple-myeloma'")
    validated = models.BooleanField(blank=True, null=True, help_text="Clinician validation flag")
    validated_by = models.CharField(max_length=100, blank=True, null=True, help_text="Name of clinician who validated")
    validation_date = models.DateField(blank=True, null=True, help_text="Date validated by clinician")
    phone_number = models.CharField(max_length=20, blank=True, null=True, help_text="Patient phone number")
    facility_name = models.CharField(max_length=255, blank=True, null=True, help_text="Treating institution name")
    prior_procedures = models.JSONField(blank=True, null=True, default=list, help_text="List of prior procedures from ProcedureOccurrence")

    # Multi-tenant ownership
    organization = models.ForeignKey(
        Organization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='patients',
        help_text="Owning organization — scopes API access for service clients",
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "patient_info"
        indexes = [
            models.Index(fields=["person"]),
            models.Index(fields=["patient_age"]),
            models.Index(fields=["disease"]),
            models.Index(fields=["stage"]),
            models.Index(fields=["-updated_at"], name="ix_pi_updated_at"),
            models.Index(fields=["organization", "-updated_at"], name="ix_pi_org_updated_at"),
        ]

    def __str__(self):
        return f"PatientInfo for Person {self.person.person_id} (age={self.patient_age}, gender={self.gender})"

    def get_languages(self):
        """Return a dictionary of languages and their skill levels"""
        return self.person.get_language_skills_summary()
    
    def get_primary_language(self):
        """Return the primary language"""
        return self.person.get_primary_language()
    
    def get_languages_display(self):
        """Return a human-readable string of languages and skills like 'English: speak, Spanish: both'"""
        skills = self.get_languages()
        if not skills:
            return "No languages recorded"
        
        display_parts = []
        for language, skill in skills.items():
            display_parts.append(f"{language}: {skill}")
        return ", ".join(display_parts)

    def save(self, *args, **kwargs):
        """Calculate BMI, age, and update therapy-related computed fields when saving"""
        # Compute age from date_of_birth, falling back to Person.year_of_birth
        from datetime import date
        today = date.today()
        dob = self.date_of_birth
        if dob is None and self.person_id:
            p = self.person
            if p.year_of_birth:
                month = p.month_of_birth or 1
                day = p.day_of_birth or 1
                try:
                    dob = date(p.year_of_birth, month, day)
                except ValueError:
                    dob = date(p.year_of_birth, 1, 1)
        if dob:
            self.patient_age = today.year - dob.year - (
                (today.month, today.day) < (dob.month, dob.day)
            )

        if self.weight and self.height:
            # Convert to metric units for calculation
            weight_kg = self.weight
            height_m = self.height
            
            if self.weight_units == 'lb':
                weight_kg = self.weight * 0.453592
            
            if self.height_units == 'in':
                height_m = self.height * 0.0254
            elif self.height_units == 'cm':
                height_m = self.height / 100
            
            self.bmi = round(weight_kg / (height_m ** 2), 2)
        
        # Update therapy-related computed fields
        self._update_therapy_computed_fields()
        
        super().save(*args, **kwargs)
    
    def _update_therapy_computed_fields(self):
        """Update computed fields based on therapy line data"""
        # Count therapy lines from populated text fields.
        # The user can also set therapy_lines_count directly from the UI without
        # filling text fields (e.g. "I had 1 prior line but don't know the name").
        # Take the max so an explicit user choice is never overwritten by the
        # text-field count, but filling in a new text field still bumps the total.
        text_lines = 0
        if self.first_line_therapy:
            text_lines += 1
        if self.second_line_therapy:
            text_lines += 1
        if self.later_therapy:
            text_lines += 1

        self.therapy_lines_count = max(text_lines, self.therapy_lines_count or 0)
        lines_count = self.therapy_lines_count

        # Set prior_therapy using the vocabulary expected by EXACT and CB matchers
        if lines_count == 0:
            self.prior_therapy = 'None'
        elif lines_count == 1:
            self.prior_therapy = 'One line'
        elif lines_count == 2:
            self.prior_therapy = 'Two lines'
        else:
            self.prior_therapy = 'More than two lines of therapy'
        
        # Determine refractory status by counting lines with a negative outcome.
        # Negative outcomes: Stable Disease (SD) or Progressive Disease (PD).
        # Rules (issue #8):
        #   0 negative lines → Not Refractory
        #   1 negative line  → Primary Refractory
        #   2 negative lines → Secondary Refractory
        #   3+ negative lines → Multi-Refractory
        #   No therapy at all → Unknown
        NEGATIVE_OUTCOMES = {
            'Stable Disease', 'Stable Disease (SD)',
            'Progressive Disease', 'Progressive Disease (PD)',
        }
        has_any_therapy = bool(self.first_line_therapy or self.second_line_therapy or self.later_therapy)
        neg_count = 0
        if self.first_line_therapy and self.first_line_outcome in NEGATIVE_OUTCOMES:
            neg_count += 1
        if self.second_line_therapy and self.second_line_outcome in NEGATIVE_OUTCOMES:
            neg_count += 1
        if self.later_therapy and self.later_outcome in NEGATIVE_OUTCOMES:
            neg_count += 1

        if not has_any_therapy:
            self.treatment_refractory_status = 'Unknown'
        elif neg_count == 0:
            self.treatment_refractory_status = 'Not Refractory'
        elif neg_count == 1:
            self.treatment_refractory_status = 'Primary Refractory'
        elif neg_count == 2:
            self.treatment_refractory_status = 'Secondary Refractory'
        else:
            self.treatment_refractory_status = 'Multi-Refractory'
        
        # Calculate expected relapse count based on outcomes
        # Count number of times a successful treatment was followed by a new line
        relapse = 0
        
        success_outcomes = [
            'Complete Response', 'Complete Response (CR)',
            'Stringent Complete Response (sCR)',
            'Very Good Partial Response (VGPR)'
        ]
        
        if self.first_line_outcome in success_outcomes and self.second_line_therapy:
            relapse += 1
        if self.second_line_outcome in success_outcomes and self.later_therapy:
            relapse += 1
            
        computed_relapse_count = relapse
        
        if self.pk:
            try:
                old_instance = PatientInfo.objects.get(pk=self.pk)
                
                # Compute what the prior logical default would have been
                old_relapse = 0
                if old_instance.first_line_outcome in success_outcomes and old_instance.second_line_therapy:
                    old_relapse += 1
                if old_instance.second_line_outcome in success_outcomes and old_instance.later_therapy:
                    old_relapse += 1
                old_computed = old_relapse
                
                # Update if not manually overridden
                # Scenario 1: New explicitly set value by user (self.relapse_count != old_instance.relapse_count). We keep the user's manual change.
                # Scenario 2: Previous manual override (old_instance.relapse_count != old_computed). We don't overwrite their prior override.
                # Scenario 3: Explicit clearing it via UI: (self.relapse_count is None and old_instance.relapse_count is not None). 
                
                if getattr(self, '_cleared_relapse_count', False) or self.relapse_count == '':
                    # if they sent an empty string or explicitly cleared, populate newly
                    self.relapse_count = computed_relapse_count
                elif self.relapse_count == old_instance.relapse_count and old_instance.relapse_count == old_computed:
                    self.relapse_count = computed_relapse_count
                elif self.relapse_count is None:
                     # fallback
                     self.relapse_count = computed_relapse_count
            except PatientInfo.DoesNotExist:
                if self.relapse_count is None:
                    self.relapse_count = computed_relapse_count
        else:
            if self.relapse_count is None:
                self.relapse_count = computed_relapse_count


# =============================================================================
# HealthTree Parity — Document Storage
# =============================================================================

class PatientDocument(models.Model):
    """Scanned/uploaded medical documents. File binary lives in external storage; URL stored here."""
    DOC_TYPE_CHOICES = [
        ('FISH', 'FISH'),
        ('GEP', 'GEP'),
        ('NGS', 'NGS'),
        ('CYTOMETRY', 'Flow Cytometry'),
        ('CYTOGENETICS', 'Cytogenetics'),
        ('LAB_RESULTS', 'Lab Results'),
        ('FULL_MEDICAL_RECORDS', 'Full Medical Records'),
        ('MRD', 'MRD'),
        ('BONE_MARROW', 'Bone Marrow'),
        ('CONSENT', 'Consent'),
        ('IMAGING', 'Imaging'),
        ('OTHER', 'Other'),
    ]
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name='documents')
    doc_type = models.CharField(max_length=50, choices=DOC_TYPE_CHOICES)
    title = models.CharField(max_length=255, blank=True, null=True)
    file_url = models.URLField(blank=True, null=True)
    file_name = models.CharField(max_length=255, blank=True, null=True)
    verified = models.BooleanField(default=False)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'patient_document'

    def __str__(self):
        return f"PatientDocument {self.doc_type} for Person {self.person_id}"


# =============================================================================
# Clinical Trial Enrollment Tracker
# Trial metadata lives in EXACT (https://github.com/cancerbot-org/exact).
# This model tracks only the patient's enrollment status; full trial details
# are fetched on demand from EXACT's API using trial_id as the key.
# =============================================================================

class PatientTrialEnrollment(models.Model):
    """Tracks a patient's participation status in a clinical trial.

    Trial metadata (title, phase, sponsor, eligibility criteria, etc.) is NOT
    stored here — it is retrieved from the EXACT trial-matcher service using
    ``trial_id`` as the lookup key.
    """

    STATUS_INTERESTED = 'interested'
    STATUS_REGISTERED = 'registered'
    STATUS_ENTERED = 'entered'
    STATUS_COMPLETED = 'completed'
    STATUS_WITHDRAWN = 'withdrawn'

    STATUS_CHOICES = [
        (STATUS_INTERESTED, 'Interested'),
        (STATUS_REGISTERED, 'Registered'),
        (STATUS_ENTERED, 'Entered'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_WITHDRAWN, 'Withdrawn'),
    ]

    person = models.ForeignKey(
        Person,
        on_delete=models.CASCADE,
        related_name='trial_enrollments',
        help_text="Patient participating in the trial",
    )
    trial_id = models.CharField(
        max_length=100,
        help_text="EXACT trial identifier — used to fetch trial metadata from EXACT API",
    )
    nct_id = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="ClinicalTrials.gov NCT number, e.g. NCT04567890",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_INTERESTED,
        help_text="Patient's current enrollment status in this trial",
    )
    status_date = models.DateField(
        blank=True,
        null=True,
        help_text="Date the current status was recorded",
    )
    notes = models.TextField(
        blank=True,
        null=True,
        help_text="Free-text notes from coordinating clinician",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'patient_trial_enrollment'
        unique_together = [('person', 'trial_id')]
        ordering = ['-status_date', '-created_at']

    def __str__(self):
        return f"Person {self.person_id} — trial {self.trial_id} ({self.status})"


class Survey(models.Model):
    """Survey definition — mirrors the ~/one EpicForm survey structure stored in Firestore."""
    STATUS_ACTIVE = 'ACTIVE'
    STATUS_DRAFT = 'DRAFT'
    STATUS_ARCHIVED = 'ARCHIVED'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, 'Active'),
        (STATUS_DRAFT, 'Draft'),
        (STATUS_ARCHIVED, 'Archived'),
    ]

    external_id = models.CharField(
        max_length=100, unique=True, blank=True, null=True,
        help_text="Firestore document ID from ~/one (for syncing)",
    )
    name = models.CharField(max_length=200, unique=True)
    title = models.CharField(max_length=500)
    description = models.TextField(blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    disease = models.CharField(max_length=100, blank=True, default='')
    pages = models.JSONField(
        default=list,
        help_text="Array of Form pages; each page has {name, title, inputs[]} matching EpicForm schema",
    )
    estimated_minutes = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'survey'
        ordering = ['name']

    def __str__(self):
        return self.title


class PatientSurveyResponse(models.Model):
    """A patient's responses to one survey, plus completion tracking and consent."""
    person = models.ForeignKey(
        Person, on_delete=models.CASCADE, related_name='survey_responses',
    )
    survey = models.ForeignKey(
        Survey, on_delete=models.CASCADE, related_name='responses',
    )
    values = models.JSONField(
        default=dict,
        help_text="Field-name → answer value map matching the survey's input names",
    )
    values_dates = models.JSONField(
        default=dict,
        help_text="Field-name → ISO timestamp of last update for each answer",
    )
    percent_complete = models.IntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    consent_date = models.DateTimeField(null=True, blank=True)
    consent_signature = models.TextField(blank=True, null=True, default=None)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'patient_survey_response'
        unique_together = [('person', 'survey')]
        ordering = ['-updated_at']

    def __str__(self):
        return f"Person {self.person_id} — {self.survey.name} ({self.percent_complete}%)"


class Institution(models.Model):
    """A SMART-on-FHIR–capable EHR or aggregator a patient can connect to."""

    slug = models.SlugField(
        max_length=64, unique=True,
        help_text="Stable identifier used in URLs and DAG conf (e.g. 'epic_uw', 'cerner').",
    )
    display_name = models.CharField(max_length=200)

    # SMART/FHIR endpoint config
    fhir_base = models.URLField(
        help_text="Base FHIR URL; passed as the `aud` claim during OAuth.",
    )
    smart_config_url = models.URLField(
        help_text=".well-known/smart-configuration; resolves authorize + token endpoints.",
    )
    client_id = models.CharField(max_length=200)
    scopes = models.CharField(
        max_length=500,
        help_text="Space-separated OAuth scopes requested at /authorize.",
    )
    redirect_uri = models.URLField(
        help_text="Must match the redirect URI registered with the vendor.",
    )

    # Asymmetric client auth (private_key_jwt). Null = public client / PKCE only.
    jwks_kid = models.CharField(
        max_length=100, null=True, blank=True,
        help_text="`kid` of the signing key in our JWKS. Empty disables JWT client auth.",
    )

    # Capabilities
    supports_bulk_export = models.BooleanField(
        default=False,
        help_text="True → fhir_extract uses $export; False → paginated fallback.",
    )

    # Retry / backoff parameters — encode per-vendor observed behaviour
    # (HealthTree v1.1 Section 2.3.4). Defaults are conservative.
    base_backoff_seconds = models.IntegerField(default=1)
    max_backoff_seconds = models.IntegerField(default=300)
    max_retry_count = models.IntegerField(default=5)
    respect_retry_after = models.BooleanField(default=True)
    jitter_factor = models.FloatField(default=0.1)
    retryable_status_codes = models.JSONField(
        default=list,
        help_text="HTTP statuses treated as transient (e.g. [429, 502, 503, 504]).",
    )
    daily_quota_reset_utc_hour = models.IntegerField(
        null=True, blank=True,
        help_text="UTC hour at which the vendor's daily quota resets, if applicable.",
    )

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fhir_institution"
        ordering = ["slug"]

    def __str__(self):
        return f"{self.display_name} ({self.slug})"


class FhirConnection(models.Model):
    STATUS_CONNECTED = "connected"
    STATUS_EXPIRING_SOON = "expiring_soon"
    STATUS_NEEDS_REAUTH = "needs_reauth"
    STATUS_REVOKED = "revoked"
    STATUS_DEGRADED = "degraded"
    STATUS_CHOICES = [
        (STATUS_CONNECTED, "Connected"),
        (STATUS_EXPIRING_SOON, "Expiring soon"),
        (STATUS_NEEDS_REAUTH, "Needs re-authentication"),
        (STATUS_REVOKED, "Revoked by patient"),
        (STATUS_DEGRADED, "Degraded (repeated failures)"),
    ]

    person = models.ForeignKey(
        Person, on_delete=models.CASCADE,
        related_name="fhir_connections",
        db_column="person_id",
    )
    institution = models.ForeignKey(
        Institution, on_delete=models.PROTECT,
        related_name="connections",
    )
    organization = models.ForeignKey(
        "Organization", on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="fhir_connections",
        help_text="Owning tenant org, derived from the session that initiated the connect.",
    )

    # Tokens — Fernet ciphertext. Plaintext is never persisted.
    access_token_encrypted = models.TextField()
    refresh_token_encrypted = models.TextField()
    expires_at = models.DateTimeField(help_text="UTC instant at which access_token expires.")
    scope_granted = models.CharField(max_length=500, blank=True, default="")

    # FHIR-side patient identifier returned in the SMART token response. This is
    # institution-scoped — different from `person_id`. Used to address the
    # patient at the EHR's FHIR API (Patient/{fhir_patient_id}/$everything).
    fhir_patient_id = models.CharField(max_length=200, null=True, blank=True)

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_CONNECTED,
    )

    # Sync watermarks — read by Airflow to drive incremental `_lastUpdated` syncs.
    last_successful_sync = models.DateTimeField(null=True, blank=True)
    last_attempted_sync = models.DateTimeField(null=True, blank=True)
    last_token_refresh_at = models.DateTimeField(null=True, blank=True)
    failure_count = models.IntegerField(default=0)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fhir_connection"
        constraints = [
            models.UniqueConstraint(
                fields=["person", "institution"],
                name="fhir_connection_person_institution_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["expires_at"]),
            models.Index(fields=["status"]),
            models.Index(fields=["institution", "status"]),
        ]

    def __str__(self):
        return f"FhirConnection {self.institution_id} for Person {self.person_id}"

    @property
    def is_expired(self) -> bool:
        from django.utils import timezone
        return self.expires_at <= timezone.now()

    def is_expiring_within(self, **timedelta_kwargs) -> bool:
        """e.g. `conn.is_expiring_within(days=14)` for the token-monitor DAG."""
        from datetime import timedelta
        from django.utils import timezone
        return self.expires_at <= timezone.now() + timedelta(**timedelta_kwargs)


class FhirOauthState(models.Model):
    state = models.CharField(
        max_length=64, primary_key=True,
        help_text="The OAuth `state` query param; verified on callback.",
    )
    person = models.ForeignKey(
        Person, on_delete=models.CASCADE,
        related_name="fhir_oauth_states",
        db_column="person_id",
    )
    institution = models.ForeignKey(Institution, on_delete=models.CASCADE)

    code_verifier = models.CharField(max_length=128)
    nonce = models.CharField(max_length=64)

    return_to = models.CharField(
        max_length=500, blank=True, default="",
        help_text="App-internal path to redirect to on successful callback.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fhir_oauth_state"
        indexes = [models.Index(fields=["created_at"])]

    def __str__(self):
        return f"FhirOauthState({self.state[:8]}…, person={self.person_id})"
