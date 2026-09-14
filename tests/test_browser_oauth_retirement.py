"""Retiring browser OAuth must reject already-issued credentials as well."""
import base64
import hashlib
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application, Grant, RefreshToken

from ctomop.oauth import SessionOnlyBrowserValidator
from patient_portal.models import Identity

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner():
    return Identity.objects.create_user(email="browser-oauth@example.com", password="test-password")


def application(owner, client_id):
    return Application.objects.create(
        user=owner, name=client_id, client_id=client_id,
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://app.example.com/auth/callback",
    )


@pytest.mark.parametrize("client_id,allowed", [
    ("ctomop-smart-app", False), ("external-smart-app", True),
])
def test_authorization_code_exchange(client, owner, client_id, allowed):
    app = application(owner, client_id)
    verifier = "a" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    Grant.objects.create(
        user=owner, application=app, code="test-code",
        expires=timezone.now() + timedelta(minutes=5),
        redirect_uri=app.redirect_uris, scope="patient/*.read",
        code_challenge=challenge, code_challenge_method="S256",
    )
    response = client.post("/o/token/", {
        "client_id": client_id, "grant_type": "authorization_code",
        "code": "test-code", "code_verifier": verifier,
        "redirect_uri": app.redirect_uris,
    })
    assert response.status_code == (200 if allowed else 400)
    assert ("access_token" in response.json()) == allowed
    assert ("refresh_token" in response.json()) == allowed
    assert AccessToken.objects.filter(application=app).exists() == allowed


@pytest.mark.parametrize("client_id,allowed", [
    ("ctomop-smart-app", False), ("external-smart-app", True),
])
def test_existing_refresh_token(client, owner, client_id, allowed):
    app = application(owner, client_id)
    token = AccessToken.objects.create(
        user=owner, application=app, token="previous-access",
        expires=timezone.now() + timedelta(hours=1), scope="patient/*.read offline_access",
    )
    RefreshToken.objects.create(user=owner, application=app, token="previous-refresh", access_token=token)
    response = client.post("/o/token/", {
        "client_id": client_id, "grant_type": "refresh_token", "refresh_token": "previous-refresh",
    })
    assert response.status_code == (200 if allowed else 400)
    assert ("access_token" in response.json()) == allowed
    if not allowed:
        assert response.json()["error"] == "unauthorized_client"
        assert AccessToken.objects.filter(application=app).count() == 1


@pytest.mark.parametrize("client_id,allowed", [
    ("ctomop-smart-app", False), ("external-smart-app", True),
])
def test_existing_access_token(client, owner, client_id, allowed):
    app = application(owner, client_id)
    AccessToken.objects.create(
        user=owner, application=app, token="previous-access",
        expires=timezone.now() + timedelta(hours=1), scope="patient/*.read",
    )
    response = client.get("/api/user/", HTTP_AUTHORIZATION="Bearer previous-access")
    assert response.status_code == (200 if allowed else 401)


def test_custom_retired_browser_client(settings, owner):
    settings.OAUTH2_RETIRED_BROWSER_CLIENT_IDS = {"custom-browser"}
    app = application(owner, "custom-browser")
    request = SimpleNamespace(client=app)
    validator = SessionOnlyBrowserValidator()
    assert not validator.validate_grant_type(app.client_id, "refresh_token", app, request)
    assert not validator.validate_response_type(app.client_id, "code", app, request)


def test_session_login_cookie(client, owner):
    response = client.post("/api/auth/login/", {
        "username": owner.email, "password": "test-password",
    }, content_type="application/json")
    assert response.status_code == 200
    cookie = response.cookies[settings.SESSION_COOKIE_NAME]
    assert cookie["httponly"]
    assert cookie["secure"]
    assert cookie["samesite"] == "Lax"
    assert "access_token" not in response.json()
    assert "refresh_token" not in response.json()
    assert client.get("/api/user/").status_code == 200
    assert client.post("/api/auth/logout/").status_code == 200
    assert client.get("/api/user/").status_code == 401
