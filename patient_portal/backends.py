from django.contrib.auth.backends import ModelBackend

from .models import Identity


class EmailBackend(ModelBackend):
    """Authenticate local Identity users by email + password."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None
        try:
            identity = Identity.objects.get(email__iexact=username, issuer="urn:local")
        except Identity.DoesNotExist:
            Identity().set_password(password)
            return None
        if identity.check_password(password) and self.user_can_authenticate(identity):
            return identity
        return None
