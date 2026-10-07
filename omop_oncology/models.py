from django.db import models
from omop_core.models import Person, Concept, VisitOccurrence


class Episode(models.Model):
    """OMOP Oncology Extension Episode table - disease episodes for cancer patients."""
    episode_id = models.BigIntegerField(primary_key=True)
    person = models.ForeignKey(Person, on_delete=models.CASCADE, db_column='person_id')
    episode_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='episodes', db_column='episode_concept_id')
    episode_start_date = models.DateField()
    episode_start_datetime = models.DateTimeField(null=True, blank=True)
    episode_end_date = models.DateField(null=True, blank=True)
    episode_end_datetime = models.DateTimeField(null=True, blank=True)
    # The Disease Episode a Treatment Regimen (line of therapy) belongs to
    # (#1739). BigInteger, like episode_id: ids no longer fit in 32 bits.
    episode_parent_id = models.BigIntegerField(null=True, blank=True)
    episode_number = models.IntegerField(null=True, blank=True)
    episode_object_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='episode_objects', db_column='episode_object_concept_id')
    episode_type_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='episode_types', db_column='episode_type_concept_id')
    episode_source_value = models.CharField(max_length=50, null=True, blank=True)
    episode_source_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='episode_sources', db_column='episode_source_concept_id', null=True, blank=True)

    class Meta:
        db_table = 'episode'

    def __str__(self):
        return f"Episode {self.episode_id} for Person {self.person_id}"


class EpisodeEvent(models.Model):
    """OMOP Oncology Extension Episode Event table - linking clinical events to episodes."""
    episode_id = models.BigIntegerField()
    event_id = models.BigIntegerField()
    episode_event_field_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, db_column='episode_event_field_concept_id')

    class Meta:
        db_table = 'episode_event'
        unique_together = ['episode_id', 'event_id', 'episode_event_field_concept']

    def __str__(self):
        return f"Episode {self.episode_id} Event {self.event_id}"


class CancerModifier(models.Model):
    """OMOP Oncology Extension Cancer Modifier table - cancer-specific modifiers."""
    cancer_modifier_id = models.BigIntegerField(primary_key=True)
    person = models.ForeignKey(Person, on_delete=models.CASCADE, db_column='person_id')
    cancer_modifier_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='cancer_modifiers', db_column='cancer_modifier_concept_id')
    cancer_modifier_source_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='cancer_modifier_sources', db_column='cancer_modifier_source_concept_id', null=True, blank=True)
    cancer_modifier_source_value = models.CharField(max_length=50, null=True, blank=True)

    class Meta:
        db_table = 'cancer_modifier'

    def __str__(self):
        return f"Cancer Modifier {self.cancer_modifier_id} for Person {self.person_id}"


class StemTable(models.Model):
    """OMOP Oncology Extension Stem Table - pre-processing staging table."""
    domain_id = models.CharField(max_length=20)
    person = models.ForeignKey(Person, on_delete=models.CASCADE, db_column='person_id')
    visit_occurrence = models.ForeignKey(VisitOccurrence, on_delete=models.SET_NULL, db_column='visit_occurrence_id', null=True, blank=True)
    visit_detail_id = models.IntegerField(null=True, blank=True)
    provider_id = models.IntegerField(null=True, blank=True)
    id = models.BigIntegerField(primary_key=True)
    concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='stem_concepts', db_column='concept_id')
    source_value = models.CharField(max_length=50, null=True, blank=True)
    source_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='stem_source_concepts', db_column='source_concept_id', null=True, blank=True)
    type_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='stem_type_concepts', db_column='type_concept_id')
    start_date = models.DateField()
    start_datetime = models.DateTimeField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    end_datetime = models.DateTimeField(null=True, blank=True)
    verbatim_end_date = models.DateField(null=True, blank=True)
    days_supply = models.IntegerField(null=True, blank=True)
    dose_unit_source_value = models.CharField(max_length=50, null=True, blank=True)
    lot_number = models.CharField(max_length=50, null=True, blank=True)
    modifier_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='stem_modifier_concepts', db_column='modifier_concept_id', null=True, blank=True)
    modifier_source_value = models.CharField(max_length=50, null=True, blank=True)
    operator_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='stem_operator_concepts', db_column='operator_concept_id', null=True, blank=True)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    range_high = models.DecimalField(max_digits=15, decimal_places=5, null=True, blank=True)
    range_low = models.DecimalField(max_digits=15, decimal_places=5, null=True, blank=True)
    refills = models.IntegerField(null=True, blank=True)
    route_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='stem_route_concepts', db_column='route_concept_id', null=True, blank=True)
    route_source_value = models.CharField(max_length=50, null=True, blank=True)
    sig = models.TextField(null=True, blank=True)
    stop_reason = models.CharField(max_length=20, null=True, blank=True)
    unique_device_id = models.CharField(max_length=255, null=True, blank=True)
    unit_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='stem_unit_concepts', db_column='unit_concept_id', null=True, blank=True)
    unit_source_value = models.CharField(max_length=50, null=True, blank=True)
    value_as_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='stem_value_concepts', db_column='value_as_concept_id', null=True, blank=True)
    value_as_number = models.DecimalField(max_digits=15, decimal_places=5, null=True, blank=True)
    value_as_string = models.CharField(max_length=60, null=True, blank=True)
    value_source_value = models.CharField(max_length=50, null=True, blank=True)
    anatomic_site_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='stem_anatomic_site_concepts', db_column='anatomic_site_concept_id', null=True, blank=True)
    disease_status_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='stem_disease_status_concepts', db_column='disease_status_concept_id', null=True, blank=True)
    specimen_source = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='stem_specimen_source_concepts', db_column='specimen_source_id', null=True, blank=True)
    tumor_grade_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='stem_tumor_grade_concepts', db_column='tumor_grade_concept_id', null=True, blank=True)
    tumor_stage_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='stem_tumor_stage_concepts', db_column='tumor_stage_concept_id', null=True, blank=True)

    class Meta:
        db_table = 'stem_table'

    def __str__(self):
        return f"Stem {self.id} for Person {self.person_id}"


class Histology(models.Model):
    """OMOP Oncology Extension Histology table - cancer histology information."""
    histology_id = models.BigIntegerField(primary_key=True)
    person = models.ForeignKey(Person, on_delete=models.CASCADE, db_column='person_id')
    concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='histology_concepts', db_column='concept_id')
    histology_date = models.DateField()
    histology_datetime = models.DateTimeField(null=True, blank=True)
    histology_type_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='histology_types', db_column='histology_type_concept_id')
    modifier_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='histology_modifiers', db_column='modifier_concept_id', null=True, blank=True)
    primary_site_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='histology_primary_sites', db_column='primary_site_concept_id', null=True, blank=True)
    lateral_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='histology_laterals', db_column='lateral_concept_id', null=True, blank=True)
    episode_id = models.BigIntegerField(null=True, blank=True)
    visit_occurrence = models.ForeignKey(VisitOccurrence, on_delete=models.SET_NULL, db_column='visit_occurrence_id', null=True, blank=True)
    histology_source_concept = models.ForeignKey(Concept, on_delete=models.PROTECT, related_name='histology_source_concepts', db_column='histology_source_concept_id', null=True, blank=True)
    histology_source_value = models.CharField(max_length=50, null=True, blank=True)
    modifier_source_value = models.CharField(max_length=50, null=True, blank=True)

    class Meta:
        db_table = 'histology'

    def __str__(self):
        return f"Histology {self.histology_id} for Person {self.person_id}"


class AILineOfTherapySummary(models.Model):
    """
    AI-generated line-of-therapy summary linked to an OMOP Episode.

    The OMOP Episode + EpisodeEvent tables store the structured clinical data
    (one DrugExposure per drug, one ProcedureOccurrence per procedure, each
    linked back to the Episode via EpisodeEvent).  This model stores the
    AI-generated metadata that does not have a clean OMOP home: resolved
    ingredient names, outcome text, validation state, prompt version, CAR-T /
    bispecific / transplant flags, and clinical-trial linkage.

    Relationship:
        Episode (1) ←─── AILineOfTherapySummary (1)
        Episode (1) ←─── EpisodeEvent (*) ←─── DrugExposure / ProcedureOccurrence

    episode.episode_number  = line number (1 = 1L, 2 = 2L, …)
    episode.episode_concept = Treatment Regimen concept (OMOP 32531)
    episode.episode_source_value = ai_lines_of_therapy_summary_id from upstream dbt
    """

    # Link to the OMOP Episode that represents this treatment line
    episode = models.OneToOneField(
        Episode,
        on_delete=models.CASCADE,
        related_name='ai_summary',
        help_text="The OMOP Episode representing this line of therapy"
    )

    # Upstream identifier from the dbt / Firestore source
    ai_lines_of_therapy_summary_id = models.CharField(
        max_length=255, null=True, blank=True, db_index=True,
        help_text="Source document ID from ai_lines_of_therapy_summary Firestore collection"
    )

    # Clinical outcome for this line
    outcome = models.TextField(null=True, blank=True,
                               help_text="Line outcome (e.g. PR, PD, CR, SD)")

    # AI-resolved medication names (denormalised comma-separated strings for display)
    active_ingredients = models.TextField(
        null=True, blank=True,
        help_text="All active ingredients, comma-separated (resolved from medicationMappings)"
    )
    active_ingredients_induction = models.TextField(
        null=True, blank=True,
        help_text="Induction-phase ingredients, comma-separated"
    )
    active_ingredients_maintenance = models.TextField(
        null=True, blank=True,
        help_text="Maintenance-phase ingredients, comma-separated"
    )

    # Procedure / drug-class flags (derived during AI extraction)
    has_bispecifics = models.BooleanField(
        default=False,
        help_text="True if the regimen contains a bispecific antibody"
    )
    has_transplant = models.BooleanField(
        default=False,
        help_text="True if a stem-cell transplant procedure is in the line"
    )
    has_cart = models.BooleanField(
        default=False,
        help_text="True if a CAR-T therapy (procedure or medication) is in the line"
    )

    # Resolved procedure names (denormalised for display)
    procedures = models.TextField(
        null=True, blank=True,
        help_text="Procedure names, comma-separated (resolved from procedureMappings)"
    )

    # Clinical trial
    is_clinical_trial = models.BooleanField(default=False)
    clinical_trial_identifier = models.CharField(max_length=255, null=True, blank=True)

    # Censoring date (latest document date for the patient — used in LOT analyses)
    censoring_date = models.DateField(null=True, blank=True)

    # AI / data-quality metadata
    is_validated = models.BooleanField(
        default=False,
        help_text="Manually validated by a clinician or data-ops reviewer"
    )
    prompt_version = models.CharField(
        max_length=50, null=True, blank=True,
        help_text="Version of the AI prompt used to extract this LOT"
    )
    notes = models.TextField(null=True, blank=True)

    # Timestamps
    source_created_at = models.DateTimeField(
        null=True, blank=True,
        help_text="created_at from the upstream Firestore document"
    )
    source_updated_at = models.DateTimeField(
        null=True, blank=True,
        help_text="updated_at from the upstream Firestore document"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ai_line_of_therapy_summary'
        indexes = [
            models.Index(fields=['episode']),
            models.Index(fields=['ai_lines_of_therapy_summary_id']),
        ]

    def __str__(self):
        return (
            f"LOT {self.episode.episode_number} "
            f"for Person {self.episode.person_id} "
            f"({self.active_ingredients or 'no ingredients'})"
        )

    @property
    def line_number(self) -> int | None:
        """Convenience proxy — line number is stored on the OMOP Episode."""
        return self.episode.episode_number

    @property
    def start_date(self):
        return self.episode.episode_start_date

    @property
    def end_date(self):
        return self.episode.episode_end_date

    @property
    def is_ongoing(self) -> bool:
        return self.episode.episode_end_date is None
