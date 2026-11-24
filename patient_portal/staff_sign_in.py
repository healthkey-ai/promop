"""Staff sign-in through an OpenID Connect provider (docs/staff-sign-in.md).

A deployment lists its providers in ``STAFF_SIGN_IN_PROVIDERS``: an
organisation's Microsoft Entra ID, Google Workspace, or any provider that
speaks OpenID Connect. Staff then sign in to the admin there, and the
provider applies its own two-step verification; a provider marked
``require_mfa`` must also say, in the token, that it did (``amr`` holds
``mfa``), or the sign-in is refused.

An account is the provider's (issuer, subject) pair, never an email address,
so renaming an address at the provider moves nothing. The first sign-in
needs a staff invitation (``StaffInvitation``): the link names the provider
and the address, the sign-in must come back with that address, and the
account it makes is staff with the invitation's role (a group listed in
``STAFF_ROLES``) and no password. After that, the same provider account
signs in directly.

The exchange is the authorization-code flow with PKCE, a one-time ``state``
and ``nonce`` kept in the session, and the ID token's signature checked
against the provider's published keys.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass, field
from urllib.parse import urlencode

import jwt
import requests
from django.conf import settings
from django.contrib.auth import login
from django.core.cache import cache
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)

SESSION_KEY = 'staff_sign_in'
TIMEOUT = 10  # seconds, for every call to the provider
LEEWAY = 60  # seconds of clock difference allowed on the ID token


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

    @property
    def client_secret(self) -> str:
        return os.environ.get(self.client_secret_env, '')


class SignInRefused(Exception):
    """A sign-in that must not go ahead; the message is shown to the person."""


def providers() -> dict[str, Provider]:
    found = {}
    for entry in getattr(settings, 'STAFF_SIGN_IN_PROVIDERS', []) or []:
        provider = Provider(
            key=entry['key'], label=entry['label'], issuer=entry['issuer'].rstrip('/'),
            client_id=entry['client_id'], client_secret_env=entry['client_secret_env'],
            claims=dict(entry.get('claims') or {}), require_mfa=bool(entry.get('require_mfa')),
        )
        found[provider.key] = provider
    return found


def discovery(provider: Provider) -> dict:
    """The provider's OpenID configuration, cached for an hour."""
    key = 'staff_sign_in:discovery:' + hashlib.sha256(provider.issuer.encode()).hexdigest()
    config = cache.get(key)
    if config is None:
        response = requests.get(f'{provider.issuer}/.well-known/openid-configuration', timeout=TIMEOUT)
        response.raise_for_status()
        config = response.json()
        if config.get('issuer', '').rstrip('/') != provider.issuer:
            raise SignInRefused('The sign-in provider is not set up correctly.')
        cache.set(key, config, 3600)
    return config


def callback_url() -> str:
    """Where the provider sends the person back: the address registered with it."""
    return settings.APP_BASE_URL + reverse('staff-sign-in-callback')


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


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
        raise SignInRefused('The sign-in could not be completed. Please try again.')
    id_token = response.json().get('id_token')
    if not id_token:
        raise SignInRefused('The sign-in could not be completed. Please try again.')
    key = jwt.PyJWKClient(config['jwks_uri'], timeout=TIMEOUT).get_signing_key_from_jwt(id_token)
    try:
        claims = jwt.decode(
            # Google writes its issuer with or without the scheme.
            id_token, key.key, algorithms=['RS256'], audience=provider.client_id,
            issuer=[provider.issuer, provider.issuer.removeprefix('https://')],
            leeway=LEEWAY, options={'require': ['iss', 'sub', 'aud', 'exp', 'iat']},
        )
    except jwt.PyJWTError as exc:
        logger.warning('staff sign-in: %s ID token refused: %s', provider.key, type(exc).__name__)
        raise SignInRefused('The sign-in could not be completed. Please try again.') from exc
    if not hmac.compare_digest(str(claims.get('nonce', '')), nonce):
        raise SignInRefused('The sign-in could not be completed. Please try again.')
    for name, expected in provider.claims.items():
        if str(claims.get(name, '')) != str(expected):
            raise SignInRefused(f'Only accounts of the organisation set up for {provider.label} can sign in here.')
    if provider.require_mfa and 'mfa' not in (claims.get('amr') or []):
        raise SignInRefused(
            f'{provider.label} did not confirm two-step verification for this sign-in. '
            'Ask your IT team to require it for this app, then try again.'
        )
    return claims


def address_of(claims: dict) -> str:
    """The address the provider vouches for: Google's verified email, or a
    Microsoft account's email or sign-in name (its tenant owns both)."""
    if claims.get('email') and claims.get('email_verified') is not False:
        return claims['email'].strip().lower()
    return (claims.get('preferred_username') or '').strip().lower()


def identity_for(provider: Provider, claims: dict, invitation_id: int | None):
    """The staff account for this sign-in: the one already tied to this
    provider account, or a new one from a valid invitation for its address."""
    from .models import Identity, StaffInvitation

    identity = Identity.objects.filter(issuer=provider.issuer, sub=claims['sub']).first()
    if identity is not None:
        if not (identity.is_active and identity.is_staff):
            raise SignInRefused('This account has no staff access. Ask a platform administrator.')
        return identity
    if invitation_id is None:
        raise SignInRefused(
            'There is no staff account for this sign-in yet. Ask a platform administrator for an invitation.'
        )
    with transaction.atomic():
        invitation = StaffInvitation.objects.select_for_update().filter(pk=invitation_id).first()
        if invitation is None or not invitation.is_open or invitation.provider != provider.key:
            raise SignInRefused('This invitation has expired or has been used. Ask for a new one.')
        if address_of(claims) != invitation.email.lower():
            raise SignInRefused(
                f'This invitation is for {invitation.email}. Sign in to {provider.label} with that account.'
            )
        identity = Identity(
            issuer=provider.issuer, sub=claims['sub'], email=invitation.email,
            name=(claims.get('name') or '')[:255], is_staff=True,
        )
        identity.set_unusable_password()
        identity.save()
        if invitation.role:
            from django.contrib.auth.models import Group

            group = Group.objects.filter(name=invitation.role).first()
            if group is None:  # renamed or removed since: staff with no role, for a manager to set
                logger.warning('staff sign-in: invitation %s names no existing role', invitation.pk)
            else:
                identity.groups.add(group)
        invitation.accepted_at = timezone.now()
        invitation.accepted_identity = identity
        invitation.save(update_fields=['accepted_at', 'accepted_identity'])
    return identity


def _audit(request, identity, provider: Provider, outcome: str, **detail):
    from .models import AuditEvent

    AuditEvent.objects.create(
        event_type=AuditEvent.EVENT_AUTH, method=request.method, path=request.path,
        user_id=str(identity.pk) if identity is not None else None,
        detail={'staff_sign_in': outcome, 'provider': provider.key, **detail},
    )


def _refused(request, message: str, status: int = 403):
    return render(request, 'admin/staff_sign_in_refused.html', {'message': message}, status=status)


@require_GET
def start(request, key: str):
    """GET /admin/sign-in/<provider>/: sign in with that provider."""
    provider = providers().get(key)
    if provider is None:
        return _refused(request, 'This sign-in is not available.', status=404)
    next_url = request.GET.get('next', '')
    try:
        return authorization_redirect(request, provider, next_url=next_url)
    except (requests.RequestException, SignInRefused):
        logger.exception('staff sign-in: %s could not start', key)
        return _refused(request, f'{provider.label} could not be reached. Please try again.', status=502)


@require_GET
def accept_invitation(request, token: str):
    """GET /admin/sign-in/invitation/<token>/: a staff invitation's link."""
    from .models import StaffInvitation

    invitation = StaffInvitation.objects.filter(token_hash=hash_token(token)).first()
    provider = providers().get(invitation.provider) if invitation else None
    if invitation is None or not invitation.is_open or provider is None:
        return _refused(request, 'This invitation has expired or has been used. Ask for a new one.', status=404)
    try:
        return authorization_redirect(request, provider, invitation_id=invitation.pk)
    except (requests.RequestException, SignInRefused):
        logger.exception('staff sign-in: %s could not start', provider.key)
        return _refused(request, f'{provider.label} could not be reached. Please try again.', status=502)


@require_GET
def callback(request):
    """GET /admin/sign-in/callback/: the provider sends the person back here."""
    attempt = request.session.pop(SESSION_KEY, None)
    state = request.GET.get('state', '')
    if not attempt or not state or not hmac.compare_digest(attempt['state'], state):
        return _refused(request, 'This sign-in has expired. Please start again.', status=400)
    provider = providers().get(attempt['provider'])
    if provider is None:
        return _refused(request, 'This sign-in is not available.', status=404)
    if timezone.now().timestamp() - attempt['started'] > 600:
        return _refused(request, 'This sign-in took too long. Please start again.', status=400)
    if request.GET.get('error') or not request.GET.get('code'):
        _audit(request, None, provider, 'cancelled', error=request.GET.get('error', '')[:64])
        return _refused(request, f'{provider.label} did not complete the sign-in.', status=400)
    try:
        claims = verified_claims(provider, request.GET['code'], verifier=attempt['verifier'], nonce=attempt['nonce'])
        identity = identity_for(provider, claims, attempt.get('invitation'))
    except SignInRefused as refusal:
        _audit(request, None, provider, 'refused', reason=str(refusal)[:200])
        return _refused(request, str(refusal))
    except requests.RequestException:
        logger.exception('staff sign-in: %s could not be reached', provider.key)
        return _refused(request, f'{provider.label} could not be reached. Please try again.', status=502)
    login(request, identity, backend='patient_portal.backends.EmailBackend')
    _audit(request, identity, provider, 'signed_in', mfa='mfa' in (claims.get('amr') or []))
    next_url = attempt.get('next') or reverse('admin:index')
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()},
                                           require_https=request.is_secure()):
        next_url = reverse('admin:index')
    return redirect(next_url)
