from django import forms
from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.models import Group
from .models import (
    Identity, PatientMessage, PatientConsent, PatientInvitation, StaffInvitation, AuditEvent, BreakGlassGrant,
    ConsentPurpose, ConsentText, LegalDocument, LegalDocumentVersion, ConsentEvent,
    DocumentAcceptance, ConsentLedgerEntry,
)


def _is_superuser(request) -> bool:
    return bool(request.user.is_active and request.user.is_superuser)


def _staff_today():
    """Today in staff's own time zone (STAFF_TIME_ZONE), not the server's."""
    from zoneinfo import ZoneInfo

    from django.conf import settings
    from django.utils import timezone

    return timezone.localdate(timezone=ZoneInfo(settings.STAFF_TIME_ZONE))


def _requested_on(request):
    """The date the member asked, from the withdrawal form: today or earlier."""
    import datetime

    try:
        day = datetime.date.fromisoformat(request.POST.get('requested_on', ''))
    except ValueError:
        return None
    return day if day <= _staff_today() else None


def _in_staff_organizations(request, queryset, person_path):
    """For role staff, only members every one of whose records is in one of
    STAFF_ORGANIZATIONS (``person_path`` leads from the queryset's model to
    the person). Full staff and superusers are not limited."""
    from django.conf import settings
    from omop_core.models import PatientRecord

    if request.user.is_staff or request.user.is_superuser:
        return queryset
    allowed = settings.STAFF_ORGANIZATIONS
    if not allowed:
        return queryset.none()
    elsewhere = PatientRecord.objects.exclude(organization__slug__in=allowed).values('person_id')
    inside = PatientRecord.objects.filter(organization__slug__in=allowed).values('person_id')
    return queryset.filter(**{f'{person_path}__in': inside}).exclude(**{f'{person_path}__in': elsewhere})


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
               'withdraw_permission', 'close_member_account', 'remove_staff_access']

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

    # UserAdmin would let a role holding add_identity or delete_identity make
    # accounts or delete every superuser: accounts come from invitations and
    # sign-ups, and go by closing or erasure.
    def has_add_permission(self, request):
        return _is_superuser(request)

    def has_delete_permission(self, request, obj=None):
        return _is_superuser(request)

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('groups')

    @admin.display(description='Staff role')
    def staff_role(self, obj):
        from django.conf import settings

        return ', '.join(g.name for g in obj.groups.all() if g.name in settings.STAFF_ROLES)

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
    def _members_only(request, queryset, *, even_for_superusers=False):
        """What member support may act on: members, never staff accounts of
        either kind, and for role staff only members of the deployment's
        organisations (STAFF_ORGANIZATIONS)."""
        from django.conf import settings

        if _is_superuser(request) and not even_for_superusers:
            return queryset
        members = queryset.filter(is_staff=False, is_superuser=False).exclude(
            groups__name__in=settings.STAFF_ROLES).distinct()
        return _in_staff_organizations(request, members, 'patient_user__person_id')

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
        account a single-use reset link (via the configured anymail backend:
        SendGrid or Mailgun). No password is set or emailed — the user chooses their own via the link."""
        from django.contrib import messages
        from patient_portal.api.password_reset import send_password_reset_email, PasswordResetEmailError
        sent, failed = 0, []
        for identity in self._members_only(request, queryset):
            try:
                send_password_reset_email(identity, by_administrator=True)
                self.log_change(request, identity, 'Emailed a password reset link')
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
        updated = 0
        for identity in self._members_only(request, queryset):
            identity.must_change_password = True
            identity.save(update_fields=['must_change_password'])
            self.log_change(request, identity, 'Required a password change at next login')
            updated += 1
        self.message_user(request, f'Flagged {updated} account(s) to change password at next login.')

    @admin.action(description="Withdraw a permission on the member's behalf", permissions=['support'])
    def withdraw_permission(self, request, queryset):
        """One member at a time: asks which permission, when the member asked
        and how, then records the withdrawal as staff (``record_consent``),
        with its consequences. The date asked goes into the six-year ledger
        too, as evidence the change was the member's."""
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
        latest = {e.purpose_id: e for e in ConsentEvent.objects.filter(person=account.person)
                  .order_by('purpose_id', '-recorded_at', '-id').distinct('purpose_id')}
        granted = ConsentPurpose.objects.filter(
            pk__in=[pk for pk, e in latest.items() if e.decision == ConsentEvent.GRANTED], is_active=True,
        ).order_by('sort_order', 'key')
        key = request.POST.get('purpose')
        requested_on, note, error = _requested_on(request), request.POST.get('note', '').strip(), ''
        if request.POST.get('confirm') == 'withdraw' and key:
            purpose = granted.filter(key=key).first()
            if purpose is None:
                self.message_user(request, 'That permission is not granted.', level=messages.WARNING)
                return None
            standing = latest[purpose.pk]
            from zoneinfo import ZoneInfo

            from django.conf import settings
            from django.utils import timezone as tz

            granted_on = tz.localtime(standing.recorded_at, ZoneInfo(settings.STAFF_TIME_ZONE)).date()
            if requested_on is None:
                error = 'Give the date the member asked, today or earlier.'
            elif requested_on < granted_on:
                # They gave it again after asking: a request older than the
                # grant must not end it (and, for some, erase their answers).
                error = (f'The member gave this permission on {granted_on.isoformat()}, after the date they asked. '
                         'Check the date, or ask them whether they still want it withdrawn.')
            elif len(note) > 500:
                error = 'Keep the note to 500 characters.'
            else:
                # Under the wording the member granted, as the history keeps it.
                record_consent(account.person, purpose, ConsentEvent.WITHDRAWN, source=ConsentEvent.STAFF,
                               recorded_by=request.user, requested_on=requested_on, note=note,
                               version=standing.version, language=standing.language)
                self.log_change(request, identity, f'Withdrew {purpose.key} on the member\'s behalf')
                self.message_user(request, f'Withdrew "{purpose.key}" for {identity.email}.')
                return None
        for purpose in granted:  # named as the member saw it
            text = purpose.texts.filter(published_at__isnull=False).order_by('-published_at').first()
            purpose.title = text.title if text else purpose.key
        return TemplateResponse(request, 'admin/withdraw_permission.html', {
            **self.admin_site.each_context(request), 'title': 'Withdraw a permission',
            'identity': identity, 'purposes': granted, 'action': 'withdraw_permission',
            'selected': identity.pk, 'opts': self.model._meta, 'error': error,
            'requested_on': request.POST.get('requested_on') or _staff_today().isoformat(),
            'note': note, 'chosen': key,
        })

    def has_close_accounts_permission(self, request):
        return request.user.has_perm('patient_portal.close_member_accounts')

    @admin.action(description="Close the account on the member's behalf", permissions=['close_accounts'])
    def close_member_account(self, request, queryset):
        """One member at a time, confirmed by typing their address: everything
        goes straight away, as when the member closes it themselves
        (``erasure.close_account``), except the consent ledger and the audit
        trail. Never a staff account."""
        from django.contrib import messages
        from django.template.response import TemplateResponse

        from .erasure import close_account, sign_in_to_close
        from .models import PatientUser

        # Never a staff account, whoever asks: this is a member's request.
        members = self._members_only(request, queryset, even_for_superusers=True)
        if members.count() != 1:
            self.message_user(request, 'Choose one member.', level=messages.WARNING)
            return None
        identity = members.get()
        account = PatientUser.objects.filter(identity=identity).select_related('person').first()
        if account is None:
            self.message_user(request, f'{identity.email} is not a member.', level=messages.WARNING)
            return None
        typed = request.POST.get('confirm_email', '').strip().lower()
        if request.POST.get('confirm') == 'close':
            if typed and typed == (identity.email or '').lower():
                person_id = account.person.person_id
                sign_in = sign_in_to_close(account.person)
                # The history keeps who closed it and when; closing then
                # rewrites the name it keeps for the account.
                self.log_change(request, identity, "Closed the account on the member's behalf")
                close_account(account.person, sign_in, actor=request.user)
                if sign_in is None:
                    self.message_user(request, f'Deleted the record of person {person_id}. The sign-in stays: it '
                                      'has other access (a provider role).', level=messages.WARNING)
                else:
                    self.message_user(request, f'Closed the account of person {person_id}: everything is deleted.')
                return None
            error = "That isn't the member's address."
        else:
            error = ''
        return TemplateResponse(request, 'admin/close_member_account.html', {
            **self.admin_site.each_context(request), 'title': "Close a member's account",
            'identity': identity, 'action': 'close_member_account', 'selected': identity.pk,
            'opts': self.model._meta, 'error': error,
        })

    @admin.action(description='Remove staff access', permissions=['manage_staff'])
    def remove_staff_access(self, request, queryset):
        """No longer staff: no staff role and no admin. A staff manager removes
        role staff; full staff only a superuser. Never a superuser, and never
        the person doing it. Groups and permissions a superuser gave outside
        the staff roles stay, as a role change keeps them."""
        from django.conf import settings

        from .staff_sign_in import is_role_staff

        removed = 0
        for identity in queryset.filter(is_superuser=False).exclude(pk=request.user.pk):
            if not (is_role_staff(identity) or (identity.is_staff and _is_superuser(request))):
                continue
            identity.groups.remove(*identity.groups.filter(name__in=settings.STAFF_ROLES))
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

    # The token is the whole credential for claiming the member's account:
    # a superuser's only, in the form and in the read-only view alike.
    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        return fields if _is_superuser(request) else [f for f in fields if f != 'token']

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        return fields if _is_superuser(request) else [f for f in fields if f != 'token']

    def has_change_permission(self, request, obj=None):
        # Support sees invitations and sends them again; changing one (its
        # address, its person) would let them point it anywhere.
        return _is_superuser(request)

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

        if queryset.count() != 1:
            # One at a time: each re-derives the patient's record, which can
            # take many seconds, and a timeout mid-way would leave links
            # replaced without their emails.
            self.message_user(request, 'Choose one invitation.', level=messages.WARNING)
            return
        sent, refused, failed = 0, [], []
        queryset = _in_staff_organizations(request, queryset, 'person_id')
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

    def __init__(self, *args, inviter_is_superuser=False, inviter_email='', **kwargs):
        from django.conf import settings
        from django.contrib.auth.models import Group

        from .staff_sign_in import providers

        super().__init__(*args, **kwargs)
        self.inviter_is_superuser, self.inviter_email = inviter_is_superuser, (inviter_email or '').lower()
        self.fields['provider'] = forms.ChoiceField(
            choices=[(p.key, p.label) for p in providers().values()], label='Signs in with')
        existing = set(Group.objects.filter(name__in=settings.STAFF_ROLES).values_list('name', flat=True))
        choices = [(r, r) for r in settings.STAFF_ROLES if r in existing]
        if inviter_is_superuser:
            choices.append((self.FULL_STAFF, 'Full staff access (is_staff)'))
        self.fields['role'] = forms.ChoiceField(choices=choices)

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if email == self.inviter_email and not self.inviter_is_superuser:
            raise forms.ValidationError('You cannot invite yourself: ask another platform administrator.')
        return email

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
                super().__init__(*args, inviter_is_superuser=_is_superuser(request),
                                 inviter_email=request.user.email, **inner)

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
    """The audit trail, read-only for everyone. The table grows with every
    request, so the list never counts it all and searches match exactly or by
    prefix. IP addresses are for superusers. Reading it is itself recorded,
    with the search (an ``audit_review`` row)."""
    list_display = ['timestamp', 'event_type', 'method', 'path', 'status_code', 'user_email', 'resource_id',
                    'ip_address']
    list_filter = ['event_type', 'method']
    date_hierarchy = 'timestamp'
    show_full_result_count = False
    search_fields = ['=resource_id', '=user_id', '^user_email', '^path', '=client_id', '=ip_address']
    readonly_fields = [f.name for f in AuditEvent._meta.fields]
    ordering = ['-timestamp']

    def get_list_display(self, request):
        return self.list_display if _is_superuser(request) else [f for f in self.list_display if f != 'ip_address']

    def get_search_fields(self, request):
        return self.search_fields if _is_superuser(request) else [f for f in self.search_fields if 'ip_address' not in f]

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        return fields if _is_superuser(request) else [f for f in fields if f != 'ip_address']

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        return fields if _is_superuser(request) else [f for f in fields if f != 'ip_address']

    def changelist_view(self, request, extra_context=None):
        # The review row says what was looked for, not only that the list opened.
        request.audit_detail = {'audit_search': {k: v[:100] for k, v in request.GET.items()}}
        return super().changelist_view(request, extra_context)

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
    list_display = ['recorded_at', 'person', 'purpose', 'decision', 'version', 'language', 'frequency', 'source',
                    'recorded_by', 'requested_on', 'note']
    list_filter = ['purpose', 'decision', 'source']
    search_fields = ['person__person_id']


@admin.register(DocumentAcceptance)
class DocumentAcceptanceAdmin(_AppendOnlyAdmin):
    list_display = ['accepted_at', 'person', 'document', 'version', 'language']
    list_filter = ['document']
    search_fields = ['person__person_id']


@admin.register(ConsentLedgerEntry)
class ConsentLedgerEntryAdmin(_AppendOnlyAdmin):
    list_display = ['recorded_at', 'email', 'kind', 'key', 'decision', 'version', 'frequency', 'requested_on']
    list_filter = ['kind', 'key', 'decision']
    search_fields = ['email', 'key']


# The admin's sign-in page also offers the deployment's staff sign-in
# providers, and no password form where staff passwords are off.
admin.site.login_template = 'admin/staff_login.html'


def _admin_has_permission(request):
    """AdminSite.has_permission, widened to role staff signed in by a provider
    (staff_sign_in.admin_site_access): they reach the admin through their role
    without being ``is_staff``, which across promop's API means full access."""
    from .staff_sign_in import admin_site_access

    return admin_site_access(request)


admin.site.has_permission = _admin_has_permission


class _SuperuserGroupAdmin(GroupAdmin):
    """Groups are the staff roles: only a superuser changes what one holds, so
    no role given ``auth.change_group`` can add permissions to itself."""

    def has_module_permission(self, request):
        return _is_superuser(request)

    def has_view_permission(self, request, obj=None):
        return _is_superuser(request)

    def has_add_permission(self, request):
        return _is_superuser(request)

    def has_change_permission(self, request, obj=None):
        return _is_superuser(request)

    def has_delete_permission(self, request, obj=None):
        return _is_superuser(request)


if admin.site.is_registered(Group):
    admin.site.unregister(Group)
admin.site.register(Group, _SuperuserGroupAdmin)
