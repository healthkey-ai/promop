"""What a sign-up form needs before there is a member: public, names nobody."""
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from patient_portal.signup import signup_policy

from .consent import language_of


class SignupPolicyView(APIView):
    """``GET /api/v1/phr/signup/policy/?lang=en``: the countries offered with
    their minimum ages, the sign-up declarations with their current wording
    and version, and the documents to accept. The form records the versions
    it shows, so a wording change republished between the two requests is a
    409 at acceptance, never a silent agreement to text the member did not see."""
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        return Response(signup_policy(language_of(request)))
