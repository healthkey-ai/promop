from django.urls import path

from patient_portal.api.email_verification import verify_email

from . import views

urlpatterns = [
    path("auth/login/", views.login, name="family-login"),
    path("auth/refresh/", views.refresh, name="family-refresh"),
    path("auth/logout/", views.logout, name="family-logout"),
    path("auth/me/", views.me, name="family-me"),
    path("auth/jwks/", views.jwks, name="family-jwks"),
    path("auth/introspect/", views.introspect, name="family-introspect"),
    # The link a phr installation's patient gets opens that installation's
    # /verify-email page, which completes it here: PROMOP's own endpoint.
    path("auth/verify-email/", verify_email, name="family-verify-email"),
]
