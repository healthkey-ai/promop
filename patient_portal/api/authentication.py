from rest_framework.authentication import SessionAuthentication


class CsrfExemptSessionAuthentication(SessionAuthentication):
    """SessionAuthentication without the built-in CSRF enforcement.

    All API views use @csrf_exempt at the Django middleware level.
    DRF's SessionAuthentication.enforce_csrf() would still return 403
    for unauthenticated requests (before the permission check gets a
    chance to return 401).  Overriding it here ensures unauthenticated
    requests receive the correct 401 response from the permission layer.
    """

    def enforce_csrf(self, request):
        return  # CSRF is handled at the Django middleware / decorator level
