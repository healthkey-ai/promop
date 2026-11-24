from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import (
    Identity, PatientMessage, PatientConsent, PatientInvitation, StaffInvitation, AuditEvent, BreakGlassGrant,
    ConsentPurpose, ConsentText, LegalDocument, LegalDocumentVersion, ConsentEvent,
    DocumentAcceptance, ConsentLedgerEntry,
)


def _is_superuser(request) -> bool:
    return bool(request.user.is_active and request.user.is_superuser)


@admin.register(Identity)
class IdentityAdmin(UserAdmin):
    """Accounts. A superuser changes anything. Other staff see accounts, and
    act only through the actions their permissions allow (docs/staff-sign-in.md):
    member support (``support_member_accounts``) sends password reset links,
    flags a password change and withdraws a permission for a member; a staff
    manager (``manage_staff``) sets or removes another staff member's role.
    Nobody but a superuser changes an address, staff or superuser status, or a
    password, so no staff role can widen its own access or take an account over.
    Role staff are not ``is_staff`` (patient_portal.staff_sign_in)."""
    list_display = ['email', 'name', 'staff_role', 'is_premium', 'is_staff', 'is_active', 'created_at']
    list_filter = ['is_premium', 'is_staff', 'is_active']
    search_fields = ['email', 'name', 'sub']
    ordering = ['email']
    readonly_fields = ['uid', 'issuer', 'sub', 'created_at']
    actions = ['grant_premium', 'revoke_premium', 'reset_password', 'require_password_change',
               'withdraw_permission', 'remove_staff_access']

    fieldsets = (
        (None,          {'fields': ('uid', 'issuer', 'sub', 'email', 'name')}),
        ('Access',      {'fields': ('is_active', 'is_staff', 'is_superuser', 'is_premium')}),
        ('Role',        {'fields': ('groups',)}),
        ('Security',    {'fields': ('must_change_password',)}),
        ('Timestamps',  {'fields': ('created_at',)}),
    )
    add_fieldsets = ()

    def has_support_permission(self, request):
        return request.user.has_perm('patient_portal.support_member_accounts')

    def has_manage_staff_permission(self, request):
        return request.user.has_perm('patient_portal.manage_staff')

    def has_superuser_permission(self, request):
        return _is_superuser(request)

    @admin.display(description='Staff role')
    def staff_role(self, obj):
        from django.conf import settings

        return ', '.join(obj.groups.filter(name__in=settings.STAFF_ROLES).values_list('name', flat=True))

    def get_readonly_fields(self, request, obj=None):
        if _is_superuser(request):
            return self.readonly_fields
        every = [f for fieldset in self.fieldsets for f in fieldset[1]['fields']]
        from .staff_sign_in import is_role_staff

        if obj is not None and obj != request.user and is_role_staff(obj) \
                and self.has_manage_staff_permission(request):
            return [f for f in every if f != 'groups']  # a staff manager sets another's role
        return every

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == 'groups' and not _is_superuser(request):
            from django.contrib.auth.models import Group
            from django.conf import settings

            kwargs['queryset'] = Group.objects.filter(name__in=settings.STAFF_ROLES)
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    def save_related(self, request, form, formsets, change):
        """A staff manager's form lists the staff roles only; saving it must
        not drop the account's other groups, which a superuser gave."""
        from django.conf import settings

        kept = []
        if change and not _is_superuser(request):
            kept = list(form.instance.groups.exclude(name__in=settings.STAFF_ROLES))
        super().save_related(request, form, formsets, change)
        if kept:
            form.instance.groups.add(*kept)

    def user_change_password(self, request, id, form_url=''):
        # UserAdmin lets anyone who may change an account set its password.
        from django.core.exceptions import PermissionDenied

        if not _is_superuser(request):
            raise PermissionDenied
        return super().user_change_password(request, id, form_url)

    @staticmethod
    def _members_only(request, queryset):
        """What member support may act on: members, never staff accounts of
        either kind."""
        from django.conf import settings

        if _is_superuser(request):
            return queryset
        return queryset.filter(is_staff=False, is_superuser=False).exclude(
            groups__name__in=settings.STAFF_ROLES).distinct()

    @admin.action(description='Grant Premium access', permissions=['superuser'])
    def grant_premium(self, request, queryset):
        updated = queryset.update(is_premium=True)
        self.message_user(request, f'Granted Premium to {updated} user(s).')

    @admin.action(description='Revoke Premium access', permissions=['superuser'])
    def revoke_premium(self, request, queryset):
        updated = queryset.update(is_premium=False)
        self.message_user(request, f'Revoked Premium from {updated} user(s).')

    @admin.action(description='Email password reset link', permissions=['support'])
    def reset_password(self, request, queryset):
        """Administrative password reset (PHR-S FM TI.1.1#08): email each selected
        account a single-use reset link (via the configured Mailgun/anymail backend).
        No password is set or emailed — the user chooses their own via the link."""
        from django.contrib import messages
        from patient_portal.api.password_reset import send_password_reset_email, PasswordResetEmailError
        sent, failed = 0, []
        for identity in self._members_only(request, queryset):
            try:
                send_password_reset_email(identity, by_administrator=True)
                sent += 1
            except PasswordResetEmailError:
                failed.append(identity.email or str(identity))
        self.message_user(request, f'Sent {sent} password-reset email(s).')
        if failed:
            self.message_user(request, f'Could not email: {", ".join(failed)}', level=messages.WARNING)

    @admin.action(description='Require password change at next login', permissions=['support'])
    def require_password_change(self, request, queryset):
        """Force a password change (PHR-S FM TI.1.1#09): set the force-change flag so
        the account is refused every /api/ request except change-password until it sets
        a new password. Enforced by ForcePasswordChangeMiddleware."""
        updated = self._members_only(request, queryset).update(must_change_password=True)
        self.message_user(request, f'Flagged {updated} account(s) to change password at next login.')

    @admin.action(description="Withdraw a permission on the member's behalf", permissions=['support'])
    def withdraw_permission(self, request, queryset):
        """One member at a time: asks which permission, then records the
        withdrawal as staff (``record_consent``), with its consequences."""
        from django.contrib import messages
        from django.template.response import TemplateResponse

        from .consent import record_consent
        from .models import ConsentEvent, ConsentPurpose, PatientUser

        members = self._members_only(request, queryset)
        if members.count() != 1:
            self.message_user(request, 'Choose one member.', level=messages.WARNING)
            return None
        identity = members.get()
        account = PatientUser.objects.filter(identity=identity).select_related('person').first()
        if account is None:
            self.message_user(request, f'{identity.email} is not a member.', level=messages.WARNING)
            return None
        latest = (ConsentEvent.objects.filter(person=account.person)
                  .order_by('purpose_id', '-recorded_at', '-id').distinct('purpose_id'))
        granted = ConsentPurpose.objects.filter(
            pk__in=[e.purpose_id for e in latest if e.decision == ConsentEvent.GRANTED], is_active=True,
        ).order_by('sort_order', 'key')
        key = request.POST.get('purpose')
        if request.POST.get('confirm') == 'withdraw' and key:
            purpose = granted.filter(key=key).first()
            if purpose is None:
                self.message_user(request, 'That permission is not granted.', level=messages.WARNING)
                return None
            record_consent(account.person, purpose, ConsentEvent.WITHDRAWN,
                           source=ConsentEvent.STAFF, recorded_by=request.user)
            self.message_user(request, f'Withdrew "{purpose.key}" for {identity.email}.')
            return None
        for purpose in granted:  # named as the member saw it
            text = purpose.texts.filter(published_at__isnull=False).order_by('-published_at').first()
            purpose.title = text.title if text else purpose.key
        return TemplateResponse(request, 'admin/withdraw_permission.html', {
            **self.admin_site.each_context(request), 'title': 'Withdraw a permission',
            'identity': identity, 'purposes': granted, 'action': 'withdraw_permission',
            'selected': identity.pk, 'opts': self.model._meta,
        })

    @admin.action(description='Remove staff access', permissions=['manage_staff'])
    def remove_staff_access(self, request, queryset):
        """No longer staff: no role, no permissions, no admin. A staff manager
        removes role staff; full staff only a superuser. Never a superuser, and
        never the person doing it."""
        from .staff_sign_in import is_role_staff

        removed = 0
        for identity in queryset.filter(is_superuser=False).exclude(pk=request.user.pk):
            if not (is_role_staff(identity) or (identity.is_staff and _is_superuser(request))):
                continue
            identity.groups.clear()
            identity.user_permissions.clear()
            if identity.is_staff:
                identity.is_staff = False
                identity.save(update_fields=['is_staff'])
            self.log_change(request, identity, 'Removed staff access')
            removed += 1
        self.message_user(request, f'Removed staff access from {removed} account(s).')


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
    actions = ['resend']

    def get_exclude(self, request, obj=None):
        # The token is the whole credential for claiming the member's account.
        return None if _is_superuser(request) else ['token']

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields if _is_superuser(request) else ['created_at', 'accepted_at']

    def has_support_permission(self, request):
        return request.user.has_perm('patient_portal.support_member_accounts')

    @admin.action(description='Send the invitation again (a new link)', permissions=['support'])
    def resend(self, request, queryset):
        """As POST /api/v1/patients/<id>/invite/ does: a new link, the old one
        stops working. Only the person's latest invitation, not cancelled, to
        the address their record holds: support never sends a link to an
        address someone replaced or cancelled, nor changes the record's."""
        from django.contrib import messages
        from omop_core.models import PatientRecord

        from .api.patient_invitations import _invitation_refusal, _issue_invitation

        sent, refused, failed = 0, [], []
        for invitation in queryset.filter(accepted_at__isnull=True, cancelled_at__isnull=True).select_related('person'):
            person = invitation.person
            latest = PatientInvitation.objects.filter(person=person).order_by('-created_at', '-pk').first() \
                if person is not None else None
            if (person is None or latest != invitation
                    or invitation.email.lower() != (person.email or '').lower()
                    or _invitation_refusal(person, invitation.email) is not None):
                refused.append(invitation.email)
                continue
            response = _issue_invitation(request, person, PatientRecord.objects.filter(person=person).first(),
                                         invitation.email.lower())
            self.log_change(request, invitation, 'Sent again')
            if response.data.get('email_warning'):
                failed.append(invitation.email)
            else:
                sent += 1
        self.message_user(request, f'Sent {sent} invitation(s) again.')
        if failed:
            self.message_user(request, 'A new link was made, but the email could not be sent: '
                              + ', '.join(failed), level=messages.WARNING)
        if refused:
            self.message_user(request, 'Not sent (not the latest invitation, a changed address, or an account '
                              'exists already): ' + ', '.join(refused), level=messages.WARNING)


class StaffInvitationForm(forms.ModelForm):
    """Choices from the deployment's settings: its sign-in providers, and the
    staff roles an invitation may give. Full staff (``is_staff``, promop's full
    access) is offered to a superuser only."""
    FULL_STAFF = '__full_staff__'

    class Meta:
        model = StaffInvitation
        fields = ['email', 'provider', 'role']

    def __init__(self, *args, inviter_is_superuser=False, **kwargs):
        from django.conf import settings
        from django.contrib.auth.models import Group

        from .staff_sign_in import providers

        super().__init__(*args, **kwargs)
        self.fields['provider'] = forms.ChoiceField(
            choices=[(p.key, p.label) for p in providers().values()], label='Signs in with')
        existing = set(Group.objects.filter(name__in=settings.STAFF_ROLES).values_list('name', flat=True))
        choices = [(r, r) for r in settings.STAFF_ROLES if r in existing]
        if inviter_is_superuser:
            choices.append((self.FULL_STAFF, 'Full staff access (is_staff)'))
        self.fields['role'] = forms.ChoiceField(choices=choices)

    def clean_email(self):
        return self.cleaned_data['email'].strip().lower()

    def save(self, commit=True):
        invitation = super().save(commit=False)
        if invitation.role == self.FULL_STAFF:
            invitation.role, invitation.full_staff = '', True
        if commit:
            invitation.save()
        return invitation


@admin.register(StaffInvitation)
class StaffInvitationAdmin(admin.ModelAdmin):
    """Inviting staff (docs/staff-sign-in.md): an emailed link that makes a
    staff account at its first sign-in with the named provider. Needs
    ``manage_staff``; an invitation is never changed, only cancelled."""
    form = StaffInvitationForm
    list_display = ['email', 'role', 'full_staff', 'provider', 'invited_by', 'created_at', 'expires_at', 'status']
    search_fields = ['email']
    readonly_fields = ['email', 'provider', 'role', 'invited_by', 'created_at', 'expires_at', 'accepted_at',
                       'accepted_identity', 'cancelled_at']
    actions = ['cancel']

    @admin.display(description='Status')
    def status(self, obj):
        if obj.accepted_at:
            return 'accepted'
        if obj.cancelled_at:
            return 'cancelled'
        return 'open' if obj.is_open else 'expired'

    def _may_manage(self, request):
        return request.user.has_perm('patient_portal.manage_staff')

    def has_module_permission(self, request):
        return self._may_manage(request)

    def has_view_permission(self, request, obj=None):
        return self._may_manage(request)

    def has_add_permission(self, request):
        from .staff_sign_in import providers

        return self._may_manage(request) and bool(providers())

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_manage_staff_permission(self, request):
        return self._may_manage(request)

    def get_readonly_fields(self, request, obj=None):
        return [*self.readonly_fields, 'full_staff'] if obj is not None else []

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj is not None:
            return form

        class InviterForm(form):
            def __init__(self, *args, **inner):
                super().__init__(*args, inviter_is_superuser=_is_superuser(request), **inner)

        return InviterForm

    def save_model(self, request, obj, form, change):
        import secrets
        from django.conf import settings
        from django.contrib import messages
        from django.core.mail import send_mail
        from django.urls import reverse
        from django.utils import timezone

        from .staff_sign_in import hash_token, providers

        token = secrets.token_urlsafe(32)
        obj.token_hash = hash_token(token)
        obj.invited_by = request.user
        obj.expires_at = timezone.now() + timezone.timedelta(days=settings.STAFF_INVITATION_DAYS)
        super().save_model(request, obj, form, change)
        link = settings.APP_BASE_URL + reverse('staff-sign-in-invitation', args=[token])
        label = providers()[obj.provider].label
        try:
            send_mail(
                'Your staff invitation',
                f'You have been invited to sign in here as staff{f" ({obj.role})" if obj.role else ""}.\n\n'
                f'Open this link and sign in with {label}, using {obj.email}:\n{link}\n\n'
                f'The link works once, until {obj.expires_at:%d %B %Y}.',
                settings.DEFAULT_FROM_EMAIL, [obj.email],
            )
            self.message_user(request, f'Invitation emailed to {obj.email}.')
        except Exception:  # noqa: BLE001 - the link below still lets the inviter pass it on
            import logging

            logging.getLogger(__name__).exception('staff invitation %s: email not sent', obj.pk)
            self.message_user(request, f'The email could not be sent to {obj.email}.', level=messages.WARNING)
        # Shown once, to the inviter only: only the token's hash is stored.
        self.message_user(request, f'Their link, if they need it from you: {link}')

    @admin.action(description='Cancel the invitation', permissions=['manage_staff'])
    def cancel(self, request, queryset):
        from django.utils import timezone

        cancelled = 0
        for invitation in queryset.filter(accepted_at__isnull=True, cancelled_at__isnull=True):
            invitation.cancelled_at = timezone.now()
            invitation.save(update_fields=['cancelled_at'])
            self.log_change(request, invitation, 'Cancelled')
            cancelled += 1
        self.message_user(request, f'Cancelled {cancelled} invitation(s).')


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
    """Lists a purpose's or document's versions. Rows are edited on their own
    page, where a published one is read-only; here they are only listed and
    added, never changed or deleted."""
    extra = 0
    fields = ['version', 'language', 'title', 'published_at']
    show_change_link = True
    can_delete = False

    def has_change_permission(self, request, obj=None):
        return False


class ConsentTextInline(_VersionedTextInline):
    model = ConsentText


class LegalDocumentVersionInline(_VersionedTextInline):
    model = LegalDocumentVersion
    fields = ['version', 'language', 'title', 'requires_reacceptance', 'published_at']


class _KeyedAdmin(admin.ModelAdmin):
    """The key is what events, ledger rows and code refer to, so it is set once."""

    def get_readonly_fields(self, request, obj=None):
        return ['key', 'created_at'] if obj is not None else ['created_at']


@admin.register(ConsentPurpose)
class ConsentPurposeAdmin(_KeyedAdmin):
    list_display = ['key', 'kind', 'answers', 'asks_frequency', 'stage', 'gated_by_country',
                    'erases_record_on_withdrawal', 'sort_order', 'is_active']
    list_filter = ['kind', 'is_active', 'gated_by_country']
    search_fields = ['key']
    inlines = [ConsentTextInline]


class _VersionedTextAdmin(admin.ModelAdmin):
    """A published version is what members agreed to: it is read-only and
    cannot be deleted. Publishing is one-way; a wording change is a new version."""
    list_filter = ['language']
    search_fields = ['version', 'title']

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.is_published:
            return [f.name for f in obj._meta.fields]
        return ['created_at']

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.is_published:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(ConsentText)
class ConsentTextAdmin(_VersionedTextAdmin):
    list_display = ['purpose', 'version', 'language', 'title', 'published_at']
    list_filter = ['purpose', 'language']


@admin.register(LegalDocument)
class LegalDocumentAdmin(_KeyedAdmin):
    list_display = ['key', 'name', 'sort_order', 'is_active']
    search_fields = ['key', 'name']
    inlines = [LegalDocumentVersionInline]


@admin.register(LegalDocumentVersion)
class LegalDocumentVersionAdmin(_VersionedTextAdmin):
    list_display = ['document', 'version', 'language', 'title', 'requires_reacceptance', 'published_at']
    list_filter = ['document', 'language']


class _AppendOnlyAdmin(admin.ModelAdmin):
    """Read-only. Rows are never added, changed or deleted on their own
    pages; they go only when the person they belong to is deleted, which
    the person's own delete page is allowed to cascade."""

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        match = getattr(request, 'resolver_match', None)
        url_name = getattr(match, 'url_name', '') or ''
        own = f'{self.opts.app_label}_{self.opts.model_name}_'
        if not url_name or url_name.startswith(own):
            return False
        return super().has_delete_permission(request, obj)


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


# The admin's sign-in page also offers the deployment's staff sign-in
# providers, and no password form where staff passwords are off.
admin.site.login_template = 'admin/staff_login.html'


def _admin_has_permission(request):
    """AdminSite.has_permission, widened to role staff (staff_sign_in): they
    reach the admin through their role without being ``is_staff``, which
    across promop's API means full access."""
    from .staff_sign_in import has_admin_access

    return has_admin_access(request.user)


admin.site.has_permission = _admin_has_permission
