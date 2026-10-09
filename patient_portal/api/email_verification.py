"""Prove a self-signed-up account receives mail at the address it typed.

Self-signup takes an email and a password and signs the user in; nothing checked
that the address is theirs. Several things then trusted that address -- a domain
trust hands out another organization's patients to anyone "at" the domain, and a
person record is linked by email match. So a local account's address is
self-asserted until the link sent here is followed (`Identity.email_verified_at`).

The link carries a signed value, not a stored token: nothing to clean up, and it
is bound to the account *and* the address, so a link issued for one address
cannot verify another.
"""
import logging

from django.conf import settings
from django.core import signing
from django.core.mail import send_mail
from django.db import transaction
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

from patient_portal.models import Identity

logger = logging.getLogger(__name__)

_SALT = 'patient_portal.email_verification'
MAX_AGE_SECONDS = 3 * 24 * 60 * 60


class VerifyEmailThrottle(AnonRateThrottle):
    scope = 'email_verification'
    rate = '10/minute'


class ResendVerificationThrottle(UserRateThrottle):
    scope = 'email_verification_resend'
    rate = '3/minute'


def make_verification_link(identity) -> str:
    token = signing.dumps({'id': identity.pk, 'email': identity.email.lower()}, salt=_SALT)
    return f"{settings.APP_BASE_URL}/verify-email?token={token}"


def send_verification_email(identity) -> bool:
    """Send the link; return whether the backend accepted it. Never raises.

    Signup must not fail because mail did: the account works without it, only
    email-derived access waits, and the user can ask for the link again.
    """
    if not identity.email or identity.has_verified_email:
        return False
    url = make_verification_link(identity)
    body = (
        "Hi,\n\n"
        "Please confirm this is your email address for PROMOP:\n\n"
        f"  {url}\n\n"
        "Until you do, access that depends on your email address -- such as an "
        "organization that trusts your email domain -- stays switched off.\n\n"
        "The link expires in 3 days. If you did not create a PROMOP account, ignore "
        "this email; the account cannot use your address without this step.\n\n"
        "— The HealthKey team"
    )
    if settings.DEBUG:
        logger.info("Verification email preview\nTo: %s\n\n%s", identity.email, body)
    try:
        return send_mail(
            "Confirm your email address for PROMOP", body,
            settings.DEFAULT_FROM_EMAIL, [identity.email],
        ) == 1
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("Failed to send verification email to identity %s", identity.pk)
        return False


def promote_trusted_domain_grants(identity) -> int:
    """Give a newly verified account what its signup was holding back.

    Signing up at a private organization that trusts the email domain records a
    self-access ('patient') grant, because at that moment the address is only a
    claim. Once it is proved, that grant becomes 'analyst' -- what a trusted
    domain signup is meant to receive (#1454). Returns the number promoted.
    """
    from omop_core.models import GroupAccess, OrgTrust
    domain = identity.verified_email_domain
    if not domain:
        return 0
    trusting = OrgTrust.objects.filter(
        trusted_domain__iexact=domain, granting_org__is_active=True,
    ).values_list('granting_org_id', flat=True)
    return GroupAccess.objects.filter(
        identity=identity, role='patient', pending_email_verification=True,
        org_id__in=list(trusting),
    ).update(role='analyst', pending_email_verification=False)


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([VerifyEmailThrottle])
def verify_email(request):
    """Public: complete verification with the emailed link's `token`."""
    token = request.data.get('token')
    token = token.strip() if isinstance(token, str) else ''
    invalid = Response(
        {'error': 'This verification link is invalid or has expired.'},
        status=status.HTTP_400_BAD_REQUEST,
    )
    if not token:
        return invalid
    try:
        payload = signing.loads(token, salt=_SALT, max_age=MAX_AGE_SECONDS)
        with transaction.atomic():
            identity = Identity.objects.select_for_update().get(pk=payload['id'], is_active=True)
            # Hold the lock while checking the address and recording proof.
            if (identity.email or '').lower() != payload.get('email'):
                return invalid
            identity.mark_email_verified()
            promote_trusted_domain_grants(identity)
    except (signing.BadSignature, KeyError, TypeError, ValueError, Identity.DoesNotExist):
        return invalid
    return Response({'detail': 'Your email address is confirmed.'})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([ResendVerificationThrottle])
def resend_verification_email(request):
    """Signed-in user asks for the link again."""
    if request.user.has_verified_email:
        return Response({'detail': 'Your email address is already confirmed.'})
    sent = send_verification_email(request.user)
    return Response(
        {'detail': 'Verification email sent.' if sent else 'The email could not be sent. Try again later.'},
        status=status.HTTP_200_OK if sent else status.HTTP_503_SERVICE_UNAVAILABLE,
    )
