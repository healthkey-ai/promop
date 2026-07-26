from datetime import date

from django.contrib import admin
from .models import (
    Person, PatientRecord, Concept, Vocabulary, Domain, ConceptClass,
    VocabularyVersionHistory, record_vocabulary_version_history,
    InterchangeAgreement,
)

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
    list_display = ['vocabulary_id', 'vocabulary_name', 'vocabulary_version',
                    'is_deprecated', 'deprecated_date']
    search_fields = ['vocabulary_id', 'vocabulary_name']
    list_filter = ['is_deprecated']
    actions = ['mark_deprecated', 'clear_deprecated']

    @admin.action(description='Mark selected vocabularies deprecated/retired')
    def mark_deprecated(self, request, queryset):
        today = date.today()
        count = 0
        for vocab in queryset:
            vocab.is_deprecated = True
            vocab.deprecated_date = today
            vocab.save(update_fields=['is_deprecated', 'deprecated_date'])
            record_vocabulary_version_history(
                vocabulary_id=vocab.vocabulary_id,
                version=vocab.vocabulary_version,
                action=VocabularyVersionHistory.ACTION_DEPRECATED,
                cdm_release_date=today,
                note='Deprecated via admin action.',
            )
            count += 1
        self.message_user(request, f'Marked {count} vocabulary(ies) deprecated.')

    @admin.action(description='Clear deprecation state on selected vocabularies')
    def clear_deprecated(self, request, queryset):
        count = queryset.update(
            is_deprecated=False, deprecated_date=None, deprecated_reason=None,
        )
        self.message_user(request, f'Cleared deprecation on {count} vocabulary(ies).')


@admin.register(VocabularyVersionHistory)
class VocabularyVersionHistoryAdmin(admin.ModelAdmin):
    list_display = ['vocabulary_id', 'version', 'action', 'cdm_release_date', 'created_at']
    search_fields = ['vocabulary_id', 'version']
    list_filter = ['action', 'vocabulary_id']
    readonly_fields = ['vocabulary_id', 'version', 'action', 'cdm_release_date',
                       'note', 'created_at']

    def has_add_permission(self, request):
        return False  # append-only, written by loader/commands only

    def has_change_permission(self, request, obj=None):
        return False  # immutable history


@admin.register(Domain)
class DomainAdmin(admin.ModelAdmin):
    list_display = ['domain_id', 'domain_name']
    search_fields = ['domain_id', 'domain_name']


@admin.register(ConceptClass)
class ConceptClassAdmin(admin.ModelAdmin):
    list_display = ['concept_class_id', 'concept_class_name']
    search_fields = ['concept_class_id', 'concept_class_name']


@admin.register(InterchangeAgreement)
class InterchangeAgreementAdmin(admin.ModelAdmin):
    list_display = [
        'partner_name', 'status', 'active',
        'effective_date', 'expiry_date', 'updated_at',
    ]
    search_fields = ['partner_name']
    list_filter = ['status', 'active', 'effective_date']
    readonly_fields = ['created_at', 'updated_at']
