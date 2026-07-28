from django.conf import settings
from django.contrib.auth.backends import ModelBackend
from django.utils import timezone

from .models import Identity


class EmailBackend(ModelBackend):
    """Authenticate local Identity users by email + password, with lockout on
    repeated failures (PHR-S FM TI.1.1#03)."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None
        try:
            identity = Identity.objects.get(email__iexact=username, issuer="urn:local")
        except Identity.DoesNotExist:
            # Equalize timing / resist account enumeration for unknown emails.
            Identity().set_password(password)
            return None

        # Locked accounts are refused regardless of password correctness; the
        # lockout is not revealed here (limited feedback — TI.1.1#10).
        if identity.is_locked:
            return None

        if identity.check_password(password) and self.user_can_authenticate(identity):
            if identity.failed_login_count or identity.locked_until:
                identity.failed_login_count = 0
                identity.locked_until = None
                identity.save(update_fields=['failed_login_count', 'locked_until'])
            return identity

        # Wrong password — count the failure and lock once the threshold is hit.
        identity.failed_login_count = (identity.failed_login_count or 0) + 1
        update_fields = ['failed_login_count']
        if identity.failed_login_count >= settings.AUTH_LOCKOUT_THRESHOLD:
            identity.locked_until = timezone.now() + timezone.timedelta(seconds=settings.AUTH_LOCKOUT_SECONDS)
            identity.failed_login_count = 0
            update_fields.append('locked_until')
        identity.save(update_fields=update_fields)
        return None
