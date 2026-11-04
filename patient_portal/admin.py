from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import (
    Identity, PatientMessage, PatientConsent, PatientInvitation, AuditEvent, BreakGlassGrant,
    ConsentPurpose, ConsentText, LegalDocument, LegalDocumentVersion, ConsentEvent,
    DocumentAcceptance, ConsentLedgerEntry,
)


@admin.register(Identity)
class IdentityAdmin(UserAdmin):
    list_display = ['email', 'name', 'is_premium', 'is_staff', 'is_active', 'created_at']
    list_filter = ['is_premium', 'is_staff', 'is_active']
    search_fields = ['email', 'name', 'sub']
    ordering = ['email']
    readonly_fields = ['uid', 'issuer', 'sub', 'created_at']
    actions = ['grant_premium', 'revoke_premium', 'reset_password', 'require_password_change']

    fieldsets = (
        (None,          {'fields': ('uid', 'issuer', 'sub', 'email', 'name')}),
        ('Access',      {'fields': ('is_active', 'is_staff', 'is_superuser', 'is_premium')}),
        ('Security',    {'fields': ('must_change_password',)}),
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

    @admin.action(description='Email password reset link')
    def reset_password(self, request, queryset):
        """Administrative password reset (PHR-S FM TI.1.1#08): email each selected
        account a single-use reset link (via the configured Mailgun/anymail backend).
        No password is set or emailed — the user chooses their own via the link."""
        from django.contrib import messages
        from patient_portal.api.password_reset import send_password_reset_email, PasswordResetEmailError
        sent, failed = 0, []
        for identity in queryset:
            try:
                send_password_reset_email(identity)
                sent += 1
            except PasswordResetEmailError:
                failed.append(identity.email or str(identity))
        self.message_user(request, f'Sent {sent} password-reset email(s).')
        if failed:
            self.message_user(request, f'Could not email: {", ".join(failed)}', level=messages.WARNING)

    @admin.action(description='Require password change at next login')
    def require_password_change(self, request, queryset):
        """Force a password change (PHR-S FM TI.1.1#09): set the force-change flag so
        the account is refused every /api/ request except change-password until it sets
        a new password. Enforced by ForcePasswordChangeMiddleware."""
        updated = queryset.update(must_change_password=True)
        self.message_user(request, f'Flagged {updated} account(s) to change password at next login.')

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


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ['timestamp', 'event_type', 'method', 'path', 'status_code', 'user_email', 'ip_address']
    list_filter = ['event_type', 'method', 'timestamp']
    search_fields = ['path', 'user_id', 'user_email', 'client_id', 'resource_id', 'ip_address']
    readonly_fields = [f.name for f in AuditEvent._meta.fields]
    ordering = ['-timestamp']

    def has_add_permission(self, request):
        return False  # audit rows are written only by the middleware

    def has_change_permission(self, request, obj=None):
        return False  # immutable audit trail (TI.2.2.1)

    def has_delete_permission(self, request, obj=None):
        return False  # indelible via admin; deletion only through the audited retention job


@admin.register(BreakGlassGrant)
class BreakGlassGrantAdmin(admin.ModelAdmin):
    """Review emergency-access authorizations (PHR-S FM TI.2.3#04)."""
    list_display = ['created_at', 'identity', 'person_id', 'expires_at', 'reason']
    list_filter = ['created_at']
    search_fields = ['identity__email', 'person_id', 'reason']
    readonly_fields = [f.name for f in BreakGlassGrant._meta.fields]

    def has_add_permission(self, request):
        return False  # grants are created only via the break-glass endpoint

    def has_change_permission(self, request, obj=None):
        return False  # immutable record of the emergency authorization


from .models import ServiceApplication, ServiceAccessToken


@admin.register(ServiceApplication)
class ServiceApplicationAdmin(admin.ModelAdmin):
    list_display = ['name', 'service_id', 'owner_contact', 'is_active', 'updated_at']
    search_fields = ['name', 'service_id', 'owner_contact']
    readonly_fields = ['created_at', 'updated_at']

    def get_readonly_fields(self, request, obj=None):
        return [*self.readonly_fields, *(['service_id'] if obj else [])]

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_staff

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ServiceAccessToken)
class ServiceAccessTokenAdmin(admin.ModelAdmin):
    list_display = ['application', 'label', 'suffix', 'created_at', 'last_used_at', 'revoked_at']
    fields = ['application', 'label', 'suffix', 'created_at', 'created_by', 'expires_at',
              'last_used_at', 'revoked_at', 'revoked_by']
    readonly_fields = fields

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_staff

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


from .models import LabMarker  # noqa: E402


@admin.register(LabMarker)
class LabMarkerAdmin(admin.ModelAdmin):
    list_display = ('loinc_code', 'label', 'rank', 'panels', 'disease_slugs')
    list_editable = ('rank',)
    search_fields = ('loinc_code', 'label')
    ordering = ('rank',)


# ---------------------------------------------------------------------------
# Consent model v2 — purposes and documents are edited here as data; the
# histories are read-only.
# ---------------------------------------------------------------------------

class _VersionedTextInline(admin.TabularInline):
    extra = 0
    fields = ['version', 'language', 'title', 'published_at']
    show_change_link = True

    def get_readonly_fields(self, request, obj=None):
        # A published version is never edited: a wording change is a new version.
        return []


class ConsentTextInline(_VersionedTextInline):
    model = ConsentText


class LegalDocumentVersionInline(_VersionedTextInline):
    model = LegalDocumentVersion
    fields = ['version', 'language', 'title', 'requires_reacceptance', 'published_at']


@admin.register(ConsentPurpose)
class ConsentPurposeAdmin(admin.ModelAdmin):
    list_display = ['key', 'kind', 'answers', 'asks_frequency', 'stage', 'gated_by_country', 'sort_order', 'is_active']
    list_filter = ['kind', 'is_active', 'gated_by_country']
    search_fields = ['key']
    inlines = [ConsentTextInline]


class _VersionedTextAdmin(admin.ModelAdmin):
    list_filter = ['language']
    search_fields = ['version', 'title']

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.is_published:
            return [f.name for f in obj._meta.fields if f.name != 'published_at']
        return ['created_at']


@admin.register(ConsentText)
class ConsentTextAdmin(_VersionedTextAdmin):
    list_display = ['purpose', 'version', 'language', 'title', 'published_at']
    list_filter = ['purpose', 'language']


@admin.register(LegalDocument)
class LegalDocumentAdmin(admin.ModelAdmin):
    list_display = ['key', 'name', 'sort_order', 'is_active']
    search_fields = ['key', 'name']
    inlines = [LegalDocumentVersionInline]


@admin.register(LegalDocumentVersion)
class LegalDocumentVersionAdmin(_VersionedTextAdmin):
    list_display = ['document', 'version', 'language', 'title', 'requires_reacceptance', 'published_at']
    list_filter = ['document', 'language']


class _AppendOnlyAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ConsentEvent)
class ConsentEventAdmin(_AppendOnlyAdmin):
    list_display = ['recorded_at', 'person', 'purpose', 'decision', 'version', 'language', 'frequency', 'source']
    list_filter = ['purpose', 'decision', 'source']
    search_fields = ['person__person_id']


@admin.register(DocumentAcceptance)
class DocumentAcceptanceAdmin(_AppendOnlyAdmin):
    list_display = ['accepted_at', 'person', 'document', 'version', 'language']
    list_filter = ['document']
    search_fields = ['person__person_id']


@admin.register(ConsentLedgerEntry)
class ConsentLedgerEntryAdmin(_AppendOnlyAdmin):
    list_display = ['recorded_at', 'email', 'kind', 'key', 'decision', 'version', 'frequency']
    list_filter = ['kind', 'key', 'decision']
    search_fields = ['email', 'key']
