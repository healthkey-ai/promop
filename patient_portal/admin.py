from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import Identity, PatientMessage, PatientConsent, PatientInvitation


@admin.register(Identity)
class IdentityAdmin(UserAdmin):
    list_display = ['email', 'name', 'is_premium', 'is_staff', 'is_active', 'created_at']
    list_filter = ['is_premium', 'is_staff', 'is_active']
    search_fields = ['email', 'name', 'sub']
    ordering = ['email']
    readonly_fields = ['uid', 'issuer', 'sub', 'created_at']
    actions = ['grant_premium', 'revoke_premium']

    fieldsets = (
        (None,          {'fields': ('uid', 'issuer', 'sub', 'email', 'name')}),
        ('Access',      {'fields': ('is_active', 'is_staff', 'is_superuser', 'is_premium')}),
        ('Timestamps',  {'fields': ('created_at',)}),
    )
    add_fieldsets = ()

    @admin.action(description='Grant Premium access')
    def grant_premium(self, request, queryset):
        updated = queryset.update(is_premium=True)
        self.message_user(request, f'Granted Premium to {updated} user(s).')

    @admin.action(description='Revoke Premium access')
    def revoke_premium(self, request, queryset):
        updated = queryset.update(is_premium=False)
        self.message_user(request, f'Revoked Premium from {updated} user(s).')

@admin.register(PatientMessage)
class PatientMessageAdmin(admin.ModelAdmin):
    list_display = ['subject', 'patient_user', 'sender_is_patient', 'is_read', 'created_at']
    search_fields = ['subject', 'message', 'patient_user__identity__email']
    list_filter = ['sender_is_patient', 'is_read', 'created_at']
    readonly_fields = ['created_at']
    
    fieldsets = (
        ('Message Details', {
            'fields': ('patient_user', 'subject', 'message')
        }),
        ('Status', {
            'fields': ('sender_is_patient', 'is_read', 'created_at')
        }),
    )

@admin.register(PatientConsent)
class PatientConsentAdmin(admin.ModelAdmin):
    list_display = ['patient_user', 'consent_type', 'consent_date']
    search_fields = ['patient_user__identity__email', 'consent_type']
    list_filter = ['consent_type', 'consent_date']
    readonly_fields = ['consent_date']


@admin.register(PatientInvitation)
class PatientInvitationAdmin(admin.ModelAdmin):
    list_display = ['email', 'person', 'status', 'invited_by', 'created_at', 'expires_at']
    search_fields = ['email', 'person__person_id']
    list_filter = ['created_at']
    readonly_fields = ['token', 'created_at', 'accepted_at']
