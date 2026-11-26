"""
Administrator-initiated password reset via an emailed single-use link
(PHR-S FM TI.1.1#08). No password is ever emailed — the link carries Django's
signed reset token (tied to the account's password hash + last_login, so it is
invalidated once the password changes or after PASSWORD_RESET_TIMEOUT). The
email is sent through the project's configured backend (Mailgun/anymail in prod).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle


class PasswordResetThrottle(AnonRateThrottle):
    """Asking for a reset link, by address. Its own history: under the
    inherited 'anon' scope it counted every anonymous request from the
    address, so a shared network was refused. The views take no
    authentication, so a signed-in caller is counted too (an AnonRateThrottle
    skips anyone authenticated)."""
    scope = 'password_reset'
    rate = '5/minute'


class PasswordResetConfirmThrottle(PasswordResetThrottle):
    """Completing a reset: a history of its own, so asking for a link does
    not use up the tries at choosing the new password."""
    scope = 'password_reset_confirm'

from patient_portal.models import Identity

if TYPE_CHECKING:
    from family_auth.installations import Installation

logger = logging.getLogger(__name__)


class PasswordResetEmailError(Exception):
    """Raised when a reset email cannot be sent."""


def make_reset_link(identity: Identity, app: Installation | None = None) -> str:
    """The reset page for this account: its installation's when that app has
    one, else PROMOP's (family_auth.installations.reset_app_for)."""
    from family_auth.installations import reset_app_for

    uidb64 = urlsafe_base64_encode(force_bytes(identity.pk))
    token = default_token_generator.make_token(identity)
    return f"{reset_app_for(identity, app).url}/reset-password?uid={uidb64}&token={token}"


def queue_password_reset(identity: Identity, *, by_administrator: bool) -> None:
    """Hand the reset email to a Celery worker, so the request does not wait
    for the mail provider. Requests run in autocommit, so this happens at
    once; inside a transaction it waits for the commit. Only the account's id
    goes to the broker, never its address or the link.

    Nothing here may fail the request, or a failure would tell an address with
    an account from one without: when the broker cannot take the task, or
    there is no broker, the email is sent inline (and that response waits).
    """
    from django.db import transaction

    identity_id = identity.pk

    def send():
        if getattr(settings, 'CELERY_BROKER_URL', ''):
            from patient_portal.tasks import send_password_reset_task

            try:
                send_password_reset_task.delay(identity_id, by_administrator)
                return
            except Exception:  # noqa: BLE001 - any broker error; the email must still go
                logger.warning('Could not queue the password reset for identity %s; sending it inline',
                               identity_id, exc_info=True)
        send_password_reset_to(identity_id, by_administrator=by_administrator)

    transaction.on_commit(send, robust=True)


def send_password_reset_to(identity_id: int, *, by_administrator: bool, raise_on_failure: bool = False) -> None:
    """The reset email for the account ``identity_id``, if it can still have
    one (active, with a password). A failure to send is logged, and raised
    only with ``raise_on_failure`` (the worker, which retries it)."""
    identity = Identity.objects.filter(pk=identity_id, is_active=True).first()
    if identity is None or not identity.has_usable_password():
        return
    try:
        send_password_reset_email(identity, by_administrator=by_administrator)
    except PasswordResetEmailError:
        if raise_on_failure:
            raise  # logged by send_password_reset_email


def send_password_reset_email(identity: Identity, *, by_administrator: bool) -> None:
    """Email the reset link. ``by_administrator`` says who asked: an
    administrator (the admin action), or the account holder themselves
    (``request_password_reset``), and the email says so."""
    from family_auth.installations import app_for

    if not identity.email:
        raise PasswordResetEmailError('Identity has no email address.')
    app = app_for(identity)
    url = make_reset_link(identity, app)
    app_name = app.name
    subject = f"Reset your {app_name} password"
    who = ("An administrator has started a password reset" if by_administrator
           else "We received a request to reset the password")
    body = (
        "Hi,\n\n"
        f"{who} for your {app_name} account.\n\n"
        "Click the link below to choose a new password:\n\n"
        f"  {url}\n\n"
        "The link can be used once and expires. If you weren't expecting this, you can "
        "ignore this email — your password stays unchanged.\n\n"
        f"— {app.sign_off}"
    )
    if settings.DEBUG:
        logger.info("Password reset email preview\nTo: %s\n\n%s", identity.email, body)
    try:
        sent = send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [identity.email])
    except Exception as exc:
        logger.exception("Failed to send a password reset email to identity %s", identity.pk)
        raise PasswordResetEmailError from exc
    if sent != 1:
        raise PasswordResetEmailError('Email backend did not report the reset email as sent.')


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])  # anonymous by design, and so always counted by the throttle
@permission_classes([AllowAny])
@throttle_classes([PasswordResetThrottle])
def request_password_reset(request):
    """Public: patient requests a password reset link by email (TI.1.1#08).

    Always returns 200 regardless of whether the email exists — prevents
    account enumeration.
    """
    email = (request.data.get('email') or '').strip().lower()
    if not email:
        return Response(
            {'error': 'email is required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    success_msg = {'detail': 'If an account exists with that email, a reset link has been sent.'}
    try:
        # Email is not globally unique: a local login and federated identities
        # can legitimately share it. Match the email/password login account.
        identity = Identity.objects.get(email__iexact=email, issuer='urn:local', is_active=True)
    except Identity.DoesNotExist:
        return Response(success_msg)
    except Identity.MultipleObjectsReturned:
        # Never choose an arbitrary account when local identities are ambiguous.
        logger.warning('Password reset skipped: multiple active local identities match.')
        return Response(success_msg)

    if not identity.has_usable_password():
        return Response(success_msg)

    # Sending is the slow part (the mail provider's API), and only an account
    # that exists has it: done here, the response time would say which
    # addresses have accounts. It runs after the response, on a worker.
    queue_password_reset(identity, by_administrator=False)
    return Response(success_msg)


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetConfirmThrottle])
def reset_password(request):
    """Public: complete a reset via the emailed link's `uid` + `token` (TI.1.1#08)."""
    from patient_portal.services import (
        password_reuse_error, password_validation_errors, set_new_password,
    )

    uidb64 = (request.data.get('uid') or '').strip()
    token = (request.data.get('token') or '').strip()
    new = request.data.get('new_password') or ''
    if not (uidb64 and token and new):
        return Response({'error': 'uid, token and new_password are required.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        pk = urlsafe_base64_decode(uidb64).decode()
        identity = Identity.objects.get(pk=pk)
    except (ValueError, TypeError, OverflowError, Identity.DoesNotExist):
        return Response({'error': 'Invalid or expired reset link.'}, status=status.HTTP_400_BAD_REQUEST)

    if not default_token_generator.check_token(identity, token):
        return Response({'error': 'Invalid or expired reset link.'}, status=status.HTTP_400_BAD_REQUEST)

    pw_errors = password_validation_errors(new, email=identity.email)
    if pw_errors:
        return Response({'error': ' '.join(pw_errors)}, status=status.HTTP_400_BAD_REQUEST)

    reuse_error = password_reuse_error(identity, new)
    if reuse_error:
        return Response({'error': reuse_error}, status=status.HTTP_400_BAD_REQUEST)

    set_new_password(identity, new, must_change=False)
    # The reset link only ever goes to the account's own address, so completing
    # it is the same proof a verification link gives.
    identity.mark_email_verified()
    from patient_portal.api.email_verification import promote_trusted_domain_grants
    promote_trusted_domain_grants(identity)
    return Response({'detail': 'Password has been reset. You can now sign in.'})
