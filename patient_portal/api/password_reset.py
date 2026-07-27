"""
Administrator-initiated password reset via an emailed single-use link
(PHR-S FM TI.1.1#08). No password is ever emailed — the link carries Django's
signed reset token (tied to the account's password hash + last_login, so it is
invalidated once the password changes or after PASSWORD_RESET_TIMEOUT). The
email is sent through the project's configured backend (Mailgun/anymail in prod).
"""
import logging

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from patient_portal.models import Identity

logger = logging.getLogger(__name__)


class PasswordResetEmailError(Exception):
    """Raised when a reset email cannot be sent."""


def make_reset_link(identity) -> str:
    uidb64 = urlsafe_base64_encode(force_bytes(identity.pk))
    token = default_token_generator.make_token(identity)
    return f"{settings.APP_BASE_URL}/reset-password?uid={uidb64}&token={token}"


def send_password_reset_email(identity) -> None:
    if not identity.email:
        raise PasswordResetEmailError('Identity has no email address.')
    url = make_reset_link(identity)
    subject = "Reset your PROMOP password"
    body = (
        "Hi,\n\n"
        "An administrator has initiated a password reset for your PROMOP account.\n\n"
        "Click the link below to choose a new password:\n\n"
        f"  {url}\n\n"
        "The link can be used once and expires. If you weren't expecting this, you can "
        "ignore this email — your password stays unchanged.\n\n"
        "— The PROMOP team"
    )
    if settings.DEBUG:
        logger.info("Password reset email preview\nTo: %s\n\n%s", identity.email, body)
    try:
        sent = send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [identity.email])
    except Exception as exc:
        logger.exception("Failed to send password reset email to %s: %s", identity.email, exc)
        raise PasswordResetEmailError from exc
    if sent != 1:
        raise PasswordResetEmailError('Email backend did not report the reset email as sent.')


@api_view(['POST'])
@permission_classes([AllowAny])
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
    return Response({'detail': 'Password has been reset. You can now sign in.'})
