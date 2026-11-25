"""Staff sign-in through an OpenID Connect provider (docs/staff-sign-in.md).

A deployment lists its providers in ``STAFF_SIGN_IN_PROVIDERS``: an
organisation's Microsoft Entra ID, Google Workspace, or any provider that
speaks OpenID Connect. Staff then sign in to the admin there, and the
provider applies its own two-step verification; a provider marked
``require_mfa`` must also say, in the token, that it did (``amr`` holds
``mfa``), or the sign-in is refused.

Two kinds of staff account, kept apart on purpose:

- **Role staff**: in a group named in ``STAFF_ROLES`` and *not* ``is_staff``.
  They reach the admin through their role, and only in a session a provider
  signed in (``admin_site_access``), never by a password; there they see only
  what the role's permissions open. Everywhere else, the API included, they are
  ordinary accounts with no patient data: across promop, ``is_staff`` means
  full access.
- **Full staff**: ``is_staff`` (or superuser), as promop has always had them.

An account is the provider's (issuer, subject) pair, never an email address,
so renaming an address at the provider moves nothing. A first sign-in needs a
staff invitation (``StaffInvitation``) for the address the provider vouches
for; it makes the account, with the invitation's role (or full staff, which
only a superuser can invite). An invitation also restores or changes an
existing account's access. After that, the same provider account signs in
directly while it keeps staff access.

The exchange is the authorization-code flow with PKCE, a one-time ``state``
and ``nonce`` kept in the session, and the ID token's signature checked
against the provider's published keys. A provider sign-in lasts
``STAFF_SESSION_HOURS``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

import jwt
import requests
from django.conf import settings
from django.contrib.auth import login
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)

SESSION_KEY = 'staff_sign_in'
# Set on the session of a provider sign-in: which provider, and when it ends
# (a timestamp; StaffSessionMiddleware signs the session out after it). A staff
# session without them began with a password.
PROVIDER_SESSION_KEY = 'staff_sign_in_provider'
STAFF_UNTIL_KEY = 'staff_sign_in_until'
TIMEOUT = 10  # seconds, for every call to the provider
LEEWAY = 60  # seconds of clock difference allowed on the ID token
ATTEMPT_SECONDS = 600  # from start to callback


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    issuer: str
    client_id: str
    client_secret_env: str
    # Claims the ID token must carry with exactly these values: the
    # organisation, e.g. {"tid": "<tenant id>"} or {"hd": "example.org"}.
    claims: dict = field(default_factory=dict)
    require_mfa: bool = False
    # Which claim is the address an invitation is matched on: "email" (only
    # when the provider says it is verified) or "preferred_username"
    # (Microsoft's sign-in name, which the tenant owns; its "email" is not
    # verified and can be edited).
    address_claim: str = 'email'

    @property
    def client_secret(self) -> str:
        return os.environ.get(self.client_secret_env, '')


class SignInRefused(Exception):
    """A sign-in that must not go ahead: ``message`` is shown to the person,
    ``code`` goes to the audit trail (never an address or a claim)."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def providers() -> dict[str, Provider]:
    found = {}
    for entry in getattr(settings, 'STAFF_SIGN_IN_PROVIDERS', []) or []:
        if not isinstance(entry, dict):
            continue  # the system check reports it
        provider = Provider(
            key=entry['key'], label=entry['label'], issuer=entry['issuer'].rstrip('/'),
            client_id=entry['client_id'], client_secret_env=entry['client_secret_env'],
            claims=dict(entry.get('claims') or {}), require_mfa=bool(entry.get('require_mfa')),
            address_claim=entry.get('address_claim') or 'email',
        )
        found[provider.key] = provider
    return found


def staff_role_names() -> list[str]:
    return list(getattr(settings, 'STAFF_ROLES', []) or [])


def is_role_staff(user) -> bool:
    """In a staff role and not full staff: the admin, through the role only."""
    if not getattr(user, 'is_authenticated', False) or user.is_staff or user.is_superuser:
        return False
    return user.groups.filter(name__in=staff_role_names()).exists()


def has_admin_access(user) -> bool:
    """Whether the account may use the admin at all: full staff, or role staff."""
    return bool(getattr(user, 'is_active', False) and (user.is_staff or is_role_staff(user)))


def admin_site_access(request) -> bool:
    """The admin's own test (AdminSite.has_permission), widened to role staff
    in a session a provider signed in. A role account that also holds a
    password (a superuser can add a role to a local account) gets no admin
    from a password sign-in, where no provider asked for two-step."""
    user = request.user
    if not getattr(user, 'is_active', False):
        return False
    if user.is_staff:
        return True
    return bool(request.session.get(PROVIDER_SESSION_KEY)) and is_role_staff(user)


def discovery(provider: Provider) -> dict:
    """The provider's OpenID configuration, cached for an hour."""
    key = 'staff_sign_in:discovery:' + hashlib.sha256(provider.issuer.encode()).hexdigest()
    config = cache.get(key)
    if config is None:
        response = requests.get(f'{provider.issuer}/.well-known/openid-configuration', timeout=TIMEOUT)
        response.raise_for_status()
        config = response.json()
        if config.get('issuer', '').rstrip('/') != provider.issuer:
            raise SignInRefused('The sign-in provider is not set up correctly.', 'issuer_mismatch')
        for endpoint in ('authorization_endpoint', 'token_endpoint', 'jwks_uri'):
            if not str(config.get(endpoint, '')).startswith('https://'):
                raise SignInRefused('The sign-in provider is not set up correctly.', 'insecure_endpoint')
        cache.set(key, config, 3600)
    return config


_jwk_clients: dict[str, jwt.PyJWKClient] = {}


def _jwk_client(jwks_uri: str) -> jwt.PyJWKClient:
    """One client per key set, so its cache of the provider's keys lasts."""
    if jwks_uri not in _jwk_clients:
        _jwk_clients[jwks_uri] = jwt.PyJWKClient(jwks_uri, timeout=TIMEOUT)
    return _jwk_clients[jwks_uri]


def callback_url() -> str:
    """Where the provider sends the person back: the address registered with it."""
    return settings.APP_BASE_URL + reverse('staff-sign-in-callback')


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def authorization_redirect(request, provider: Provider, *, invitation_id: int | None = None, next_url: str = ''):
    """Start a sign-in: remember this attempt in the session, and send the
    person to the provider."""
    config = discovery(provider)
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    request.session[SESSION_KEY] = {
        'provider': provider.key, 'state': state, 'nonce': nonce, 'verifier': verifier,
        'invitation': invitation_id, 'next': next_url, 'started': timezone.now().timestamp(),
    }
    params = {
        'client_id': provider.client_id, 'response_type': 'code', 'scope': 'openid email profile',
        'redirect_uri': callback_url(), 'state': state, 'nonce': nonce,
        'code_challenge': _b64(hashlib.sha256(verifier.encode()).digest()), 'code_challenge_method': 'S256',
    }
    return redirect(f"{config['authorization_endpoint']}?{urlencode(params)}")


_COULD_NOT = 'The sign-in could not be completed. Please try again.'


def verified_claims(provider: Provider, code: str, *, verifier: str, nonce: str) -> dict:
    """Exchange the code for an ID token, and return its claims once every
    check has passed."""
    config = discovery(provider)
    response = requests.post(config['token_endpoint'], timeout=TIMEOUT, data={
        'grant_type': 'authorization_code', 'code': code, 'redirect_uri': callback_url(),
        'client_id': provider.client_id, 'client_secret': provider.client_secret, 'code_verifier': verifier,
    })
    if response.status_code != 200:
        logger.warning('staff sign-in: %s token endpoint answered %s', provider.key, response.status_code)
        raise SignInRefused(_COULD_NOT, 'token_endpoint')
    id_token = response.json().get('id_token')
    if not id_token:
        raise SignInRefused(_COULD_NOT, 'no_id_token')
    try:
        key = _jwk_client(config['jwks_uri']).get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            # Google writes its issuer with or without the scheme.
            id_token, key.key, algorithms=['RS256'], audience=provider.client_id,
            issuer=[provider.issuer, provider.issuer.removeprefix('https://')],
            leeway=LEEWAY, options={'require': ['iss', 'sub', 'aud', 'exp', 'iat']},
        )
    except jwt.PyJWKClientConnectionError as exc:
        logger.warning('staff sign-in: %s keys could not be fetched', provider.key)
        raise SignInRefused(f'{provider.label} could not be reached. Please try again.', 'keys_unreachable') from exc
    except jwt.PyJWTError as exc:
        logger.warning('staff sign-in: %s ID token refused: %s', provider.key, type(exc).__name__)
        raise SignInRefused(_COULD_NOT, 'invalid_token') from exc
    if not _same(str(claims.get('nonce', '')), nonce):
        raise SignInRefused(_COULD_NOT, 'nonce')
    # OpenID Connect Core 3.1.3.7: with several audiences the token names who
    # it was issued to, and that must be us; one that names anyone else is not.
    azp = claims.get('azp')
    if (isinstance(claims.get('aud'), list) and len(claims['aud']) > 1 and azp is None) or \
            (azp is not None and azp != provider.client_id):
        raise SignInRefused(_COULD_NOT, 'azp')
    for name, expected in provider.claims.items():
        if str(claims.get(name, '')) != str(expected):
            raise SignInRefused(
                f'Only accounts of the organisation set up for {provider.label} can sign in here.', 'organisation')
    if provider.require_mfa and 'mfa' not in (claims.get('amr') or []):
        raise SignInRefused(
            f'{provider.label} did not confirm two-step verification for this sign-in. '
            'Ask your IT team to require it for this app, then try again.', 'no_mfa',
        )
    return claims


def address_of(provider: Provider, claims: dict) -> str:
    """The address the provider vouches for, in the claim its settings name:
    ``email`` only when the provider says it is verified, never a fallback."""
    if provider.address_claim == 'email':
        verified = claims.get('email_verified')
        if verified is True or str(verified).lower() == 'true':
            return (claims.get('email') or '').strip().lower()
        return ''
    return (claims.get(provider.address_claim) or '').strip().lower()


def _give_access(identity, invitation) -> None:
    """What an invitation gives: full staff, or exactly its staff role (any
    other staff role is replaced; groups outside STAFF_ROLES are kept)."""
    from django.contrib.auth.models import Group

    if invitation.full_staff:
        identity.is_staff = True
        identity.save(update_fields=['is_staff'])
        return
    identity.groups.remove(*identity.groups.filter(name__in=staff_role_names()))
    group = Group.objects.filter(name=invitation.role, name__in=staff_role_names()).first()
    if group is None:  # renamed or removed since it was sent: the account has no role yet
        logger.warning('staff sign-in: invitation %s names no current staff role', invitation.pk)
    else:
        identity.groups.add(group)


def identity_for(provider: Provider, claims: dict, invitation_id: int | None):
    """The staff account for this sign-in. With an open invitation for this
    provider and the address it vouches for: that account (made, or the one
    already tied to this provider account) with the invitation's access.
    Without: the account already tied to it, while it keeps admin access."""
    from .models import Identity, StaffInvitation

    if invitation_id is None:
        identity = Identity.objects.filter(issuer=provider.issuer, sub=claims['sub']).first()
        if identity is None:
            raise SignInRefused(
                'There is no staff account for this sign-in yet. Ask a platform administrator for an invitation.',
                'no_account',
            )
        if not has_admin_access(identity):
            raise SignInRefused('This account has no staff access. Ask a platform administrator.', 'no_access')
        return identity
    try:
        with transaction.atomic():
            invitation = StaffInvitation.objects.select_for_update().filter(pk=invitation_id).first()
            if invitation is None or not invitation.is_open or invitation.provider != provider.key:
                raise SignInRefused('This invitation has expired or has been used. Ask for a new one.', 'invitation_closed')
            if address_of(provider, claims) != invitation.email.lower():
                raise SignInRefused(
                    f'This invitation is for {invitation.email}. Sign in to {provider.label} with that account.',
                    'address_mismatch',
                )
            identity = Identity.objects.select_for_update().filter(issuer=provider.issuer, sub=claims['sub']).first()
            if identity is None:
                identity = Identity(issuer=provider.issuer, sub=claims['sub'], email=invitation.email,
                                    name=(claims.get('name') or '')[:255])
                identity.set_unusable_password()
                identity.save()
            elif not identity.is_active:
                raise SignInRefused('This account has been switched off. Ask a superuser.', 'inactive')
            elif invitation.invited_by_id == identity.pk:
                # Nobody changes their own access by inviting themselves.
                raise SignInRefused('An invitation you sent yourself cannot change your own access.', 'self_invitation')
            _give_access(identity, invitation)
            invitation.accepted_at = timezone.now()
            invitation.accepted_identity = identity
            invitation.save(update_fields=['accepted_at', 'accepted_identity'])
    except IntegrityError as exc:  # the same provider account, in two first sign-ins at once
        raise SignInRefused(_COULD_NOT, 'concurrent') from exc
    return identity


def _audit(request, identity, provider: Provider, outcome: str, **detail):
    """One audit row per outcome; an audit failure never fails the sign-in."""
    from .models import AuditEvent

    try:
        AuditEvent.objects.create(
            event_type=AuditEvent.EVENT_AUTH, method=request.method, path=reverse('staff-sign-in-callback'),
            user_id=str(identity.pk) if identity is not None else None,
            user_email=(identity.email or None) if identity is not None else None,
            detail={'staff_sign_in': outcome, 'provider': provider.key, **detail},
        )
    except Exception:  # noqa: BLE001 - the audit trail's own failure is logged, not raised
        logger.exception('staff sign-in: audit event not written')


def _refused(request, message: str, status: int = 403):
    from django.contrib import admin

    # The admin's own context, so the page carries the deployment's site header.
    return render(request, 'admin/staff_sign_in_refused.html',
                  {**admin.site.each_context(request), 'message': message}, status=status)


def _on_own_host(request, path: str):
    """A sign-in starts on the host the provider returns to, or its session
    would be on another host's cookie: send it to ``path`` there first. The
    target is checked to be on APP_BASE_URL's own host before redirecting."""
    base = urlsplit(settings.APP_BASE_URL)
    if not base.netloc or request.get_host() == base.netloc:
        return None
    target = f'{settings.APP_BASE_URL}{path}'
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={base.netloc}, require_https=base.scheme == 'https'):
        return _refused(request, 'This sign-in is not available.', status=400)
    return redirect(target)


def _start(request, provider: Provider, **kwargs):
    try:
        return authorization_redirect(request, provider, **kwargs)
    except (requests.RequestException, ValueError, KeyError, SignInRefused):
        logger.exception('staff sign-in: %s could not start', provider.key)
        return _refused(request, f'{provider.label} could not be reached. Please try again.', status=502)


@require_GET
def start(request, key: str):
    """GET /admin/sign-in/<provider>/: sign in with that provider."""
    provider = providers().get(key)
    if provider is None:
        return _refused(request, 'This sign-in is not available.', status=404)
    next_url = request.GET.get('next', '')
    here = reverse('staff-sign-in', args=[provider.key]) + (f'?{urlencode({"next": next_url})}' if next_url else '')
    return _on_own_host(request, here) or _start(request, provider, next_url=next_url)


@require_GET
def accept_invitation(request, token: str):
    """GET /admin/sign-in/invitation/<token>/: a staff invitation's link."""
    from .models import StaffInvitation

    invitation = StaffInvitation.objects.filter(token_hash=hash_token(token)).first()
    if invitation is not None:
        elsewhere = _on_own_host(request, reverse('staff-sign-in-invitation', args=[token]))
        if elsewhere:
            return elsewhere
    provider = providers().get(invitation.provider) if invitation else None
    if invitation is None or not invitation.is_open or provider is None:
        return _refused(request, 'This invitation has expired or has been used. Ask for a new one.', status=404)
    return _start(request, provider, invitation_id=invitation.pk)


@require_GET
def callback(request):
    """GET /admin/sign-in/callback/: the provider sends the person back here."""
    attempt = request.session.pop(SESSION_KEY, None)
    state = request.GET.get('state', '')
    if not attempt or not state or not _same(attempt['state'], state):
        return _refused(request, 'This sign-in has expired. Please start again.', status=400)
    provider = providers().get(attempt['provider'])
    if provider is None:
        return _refused(request, 'This sign-in is not available.', status=404)
    if timezone.now().timestamp() - attempt['started'] > ATTEMPT_SECONDS:
        return _refused(request, 'This sign-in took too long. Please start again.', status=400)
    if request.GET.get('error') or not request.GET.get('code'):
        _audit(request, None, provider, 'cancelled', error=request.GET.get('error', '')[:64])
        return _refused(request, f'{provider.label} did not complete the sign-in.', status=400)
    try:
        claims = verified_claims(provider, request.GET['code'], verifier=attempt['verifier'], nonce=attempt['nonce'])
        identity = identity_for(provider, claims, attempt.get('invitation'))
    except SignInRefused as refusal:
        _audit(request, None, provider, 'refused', reason=refusal.code)
        return _refused(request, str(refusal))
    except (requests.RequestException, ValueError, KeyError):
        logger.exception('staff sign-in: %s could not be completed', provider.key)
        return _refused(request, f'{provider.label} could not be reached. Please try again.', status=502)
    login(request, identity, backend='patient_portal.backends.EmailBackend')
    # A working day from now, not Django's two weeks: someone whose provider
    # account is switched off is out by the end of it. The deadline is kept
    # in the session too, because the cookie's expiry moves with every write.
    seconds = int(settings.STAFF_SESSION_HOURS * 3600)
    request.session.set_expiry(seconds)
    request.session[PROVIDER_SESSION_KEY] = provider.key
    request.session[STAFF_UNTIL_KEY] = timezone.now().timestamp() + seconds
    _audit(request, identity, provider, 'signed_in', mfa='mfa' in (claims.get('amr') or []))
    next_url = attempt.get('next') or reverse('admin:index')
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()},
                                           require_https=request.is_secure()):
        next_url = reverse('admin:index')
    return redirect(next_url)
