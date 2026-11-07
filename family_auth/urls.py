from django.urls import path

from . import views

urlpatterns = [
    path("auth/login/", views.login, name="family-login"),
    path("auth/refresh/", views.refresh, name="family-refresh"),
    path("auth/logout/", views.logout, name="family-logout"),
    path("auth/me/", views.me, name="family-me"),
    path("auth/jwks/", views.jwks, name="family-jwks"),
    path("auth/introspect/", views.introspect, name="family-introspect"),
]
