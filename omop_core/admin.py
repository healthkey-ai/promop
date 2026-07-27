from django.contrib import admin
from .models import (
    Person, PatientRecord, Concept, Vocabulary, Domain, ConceptClass,
    RecordRevision,
)


@admin.register(RecordRevision)
class RecordRevisionAdmin(admin.ModelAdmin):
    """Read-only view of PatientRecord field-level revision history (TI.1.2#04)."""
    list_display = ['patient_record', 'field', 'old_value', 'new_value', 'changed_by', 'changed_at']
    search_fields = ['patient_record__person__person_id', 'field', 'changed_by']
    list_filter = ['field', 'changed_at']
    readonly_fields = ['patient_record', 'field', 'old_value', 'new_value', 'changed_by', 'changed_at']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ['person_id', 'year_of_birth', 'month_of_birth', 'day_of_birth', 'gender_concept', 'ethnicity_concept']
    search_fields = ['person_id']
    list_filter = ['year_of_birth']


@admin.register(PatientRecord)
class PatientRecordAdmin(admin.ModelAdmin):
    list_display = ['person', 'disease', 'updated_at']
    search_fields = ['person__person_id']
    list_filter = ['disease', 'created_at', 'updated_at']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(Concept)
class ConceptAdmin(admin.ModelAdmin):
    list_display = ['concept_id', 'concept_name', 'concept_code', 'vocabulary', 'concept_class']
    search_fields = ['concept_id', 'concept_name', 'concept_code']
    list_filter = ['vocabulary', 'concept_class']


@admin.register(Vocabulary)
class VocabularyAdmin(admin.ModelAdmin):
    list_display = ['vocabulary_id', 'vocabulary_name']
    search_fields = ['vocabulary_id', 'vocabulary_name']


@admin.register(Domain)
class DomainAdmin(admin.ModelAdmin):
    list_display = ['domain_id', 'domain_name']
    search_fields = ['domain_id', 'domain_name']


@admin.register(ConceptClass)
class ConceptClassAdmin(admin.ModelAdmin):
    list_display = ['concept_class_id', 'concept_class_name']
    search_fields = ['concept_class_id', 'concept_class_name']
