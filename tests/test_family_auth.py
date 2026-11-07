"""Family auth: PROMOP as identity host for phr installations (phr#43)."""
from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from rest_framework.test import APIClient

from family_auth import tokens
from family_auth.models import RefreshToken
from patient_portal.models import Identity
from tests.factories import OrganizationFactory

pytestmark = pytest.mark.django_db

ISSUER = "https://promop.test"
BASE = "/api/v1/family/auth"


def _pem(key):
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode()


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def _family_settings(settings, rsa_key):
    settings.FAMILY_AUTH_PRIVATE_KEY = _pem(rsa_key)
    settings.FAMILY_AUTH_PUBLIC_KEY = ""
    settings.FAMILY_AUTH_ISSUER = ISSUER
    settings.FAMILY_AUTH_AUDIENCE = ["promop-api"]
    settings.FAMILY_AUTH_ACCESS_SECONDS = 900
    settings.FAMILY_AUTH_REFRESH_SECONDS = 3600
    rates = {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "family_auth": "1000/minute"}
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": rates}


@pytest.fixture
def account():
    identity = Identity.objects.create_user(email="dana@example.com", password="correct-horse-battery-9")
    identity.name = "Dana Scully"
    identity.save(update_fields=["name"])
    identity.mark_email_verified()
    return identity


def _login(client, email="dana@example.com", password="correct-horse-battery-9"):
    return client.post(f"{BASE}/login/", {"email": email, "password": password}, format="json")


def _claims(token):
    return jwt.decode(token, options={"verify_signature": False})


def test_login_issues_a_family_token_pair_for_a_promop_account(account):
    response = _login(APIClient())

    assert response.status_code == 200
    access = _claims(response.data["access"])
    assert access["iss"] == ISSUER
    assert access["aud"] == ["promop-api"]
    assert access["sub"] == account.uid == f"urn:local:{account.sub}"
    assert access["user_id"] == account.pk
    assert access["email"] == "dana@example.com"
    assert access["email_verified"] is True and access["identity_level"] == "ial1"
    assert access["claims"] == {"ADMIN": False, "MEDICAL_RECORDS": False}
    assert access["token_type"] == "access"
    assert _claims(response.data["refresh"])["token_type"] == "refresh"


def test_wrong_or_unknown_credentials_get_the_same_answer(account):
    wrong = _login(APIClient(), password="not-the-password-1")
    unknown = _login(APIClient(), email="nobody@example.com")

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.data == unknown.data


def test_a_self_signup_logs_in_only_after_verifying_its_email():
    org = OrganizationFactory(allows_patient_signup=True)
    client = APIClient()
    signup = client.post(f"/api/v1/orgs/{org.slug}/patient-signup/", {
        "email": "new@example.com", "password": "a-long-enough-pass-42",
        "given_name": "New", "family_name": "Patient",
    }, format="json")
    assert signup.status_code == 201, signup.data

    blocked = _login(client, "new@example.com", "a-long-enough-pass-42")
    assert blocked.status_code == 403
    assert blocked.data["code"] == "email_not_verified"

    Identity.objects.get(email="new@example.com").mark_email_verified()
    allowed = _login(client, "new@example.com", "a-long-enough-pass-42")
    assert allowed.status_code == 200
    # The signup made the patient's record: the token says which.
    person_id = _claims(allowed.data["access"])["person_id"]
    assert person_id is not None

    # And the token reads it like a signed-in patient: role-based access to
    # their own record, not an OAuth token that would need SMART scopes.
    patient = APIClient()
    patient.credentials(HTTP_AUTHORIZATION=f"Bearer {allowed.data['access']}")
    record = patient.get("/api/patient-info/me/")
    assert record.status_code == 200, record.data


def test_me_returns_phr_shaped_user_and_promop_accepts_the_token_as_the_same_identity(account):
    access = _login(APIClient()).data["access"]
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    me = client.get(f"{BASE}/me/")
    assert me.status_code == 200
    assert me.data["id"] == account.pk
    assert (me.data["first_name"], me.data["last_name"]) == ("Dana", "Scully")
    assert me.data["claims"] == {"ADMIN": False, "MEDICAL_RECORDS": False}

    # Any PROMOP endpoint: the same Identity, not a partner copy.
    user = client.get("/api/v1/user/")
    assert user.status_code == 200, user.data
    assert Identity.objects.filter(email="dana@example.com").count() == 1


def test_roles_come_from_staff_and_the_medical_records_group(account):
    from django.contrib.auth.models import Group

    account.is_staff = True
    account.save(update_fields=["is_staff"])
    account.groups.add(Group.objects.create(name="MEDICAL_RECORDS"))

    access = _claims(_login(APIClient()).data["access"])
    assert access["claims"] == {"ADMIN": True, "MEDICAL_RECORDS": True}


def test_jwks_publishes_the_key_that_signs_the_tokens(account):
    access = _login(APIClient()).data["access"]
    keys = APIClient().get(f"{BASE}/jwks/").data["keys"]

    assert len(keys) == 1 and keys[0]["alg"] == "RS256"
    assert jwt.get_unverified_header(access)["kid"] == keys[0]["kid"]
    public = jwt.PyJWK(keys[0]).key
    claims = jwt.decode(access, public, algorithms=["RS256"], audience="promop-api", issuer=ISSUER)
    assert claims["user_id"] == account.pk


def test_refresh_rotates_and_a_reused_refresh_token_revokes_the_session(account):
    first = _login(APIClient()).data
    rotated = APIClient().post(f"{BASE}/refresh/", {"refresh": first["refresh"]}, format="json")
    assert rotated.status_code == 200
    assert rotated.data["refresh"] != first["refresh"]

    replay = APIClient().post(f"{BASE}/refresh/", {"refresh": first["refresh"]}, format="json")
    assert replay.status_code == 401
    # The leak is contained: the rotated token is revoked too.
    after = APIClient().post(f"{BASE}/refresh/", {"refresh": rotated.data["refresh"]}, format="json")
    assert after.status_code == 401
    # Reused, revoked and garbage tokens get one answer, with no reason in it.
    garbage = APIClient().post(f"{BASE}/refresh/", {"refresh": "x.y.z"}, format="json")
    assert replay.data == after.data == garbage.data == {
        "detail": "Token is invalid or expired.", "code": "token_not_valid",
    }
    assert not RefreshToken.objects.filter(identity=account, revoked_at__isnull=True).exists()


def test_logout_revokes_the_refresh_token(account):
    pair = _login(APIClient()).data
    assert APIClient().post(f"{BASE}/logout/", {"refresh": pair["refresh"]}, format="json").status_code == 205
    assert APIClient().post(f"{BASE}/refresh/", {"refresh": pair["refresh"]}, format="json").status_code == 401
    # Logging out with garbage still succeeds.
    assert APIClient().post(f"{BASE}/logout/", {"refresh": "nope"}, format="json").status_code == 205


def test_introspect_reflects_the_identity_now(account):
    access = _login(APIClient()).data["access"]
    live = APIClient().post(f"{BASE}/introspect/", {"token": access}, format="json").data
    assert live["active"] is True and live["user_id"] == account.pk and live["aud"] == ["promop-api"]

    account.is_active = False
    account.save(update_fields=["is_active"])
    assert APIClient().post(f"{BASE}/introspect/", {"token": access}, format="json").data == {"active": False}
    assert APIClient().post(f"{BASE}/introspect/", {"token": "x.y.z"}, format="json").data == {"active": False}


def test_wrong_audience_expired_and_refresh_tokens_are_not_access_tokens(account, rsa_key):
    now = int(time.time())
    base = {"iss": ISSUER, "sub": account.uid, "user_id": account.pk, "jti": "j", "token_type": "access"}
    wrong_aud = jwt.encode({**base, "aud": "someone-else", "exp": now + 60}, _pem(rsa_key), algorithm="RS256")
    expired = jwt.encode({**base, "aud": ["promop-api"], "exp": now - 60}, _pem(rsa_key), algorithm="RS256")
    refresh = _login(APIClient()).data["refresh"]

    for token in (wrong_aud, expired, refresh):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        assert client.get(f"{BASE}/me/").status_code == 401


def test_another_issuers_token_is_left_to_the_other_authenticators(account, rsa_key):
    other = jwt.encode({"iss": "healthkey-phr", "sub": "1", "exp": int(time.time()) + 60},
                       _pem(rsa_key), algorithm="RS256")
    assert tokens.is_family_token(other) is False


def test_off_until_configured(settings, account):
    settings.FAMILY_AUTH_PRIVATE_KEY = ""
    assert _login(APIClient()).status_code == 404
    assert APIClient().get(f"{BASE}/jwks/").data == {"keys": []}


# ── ensure_signup_orgs: the installation's sign-up org, kept by every deploy ──

def _ensure(settings, value):
    from io import StringIO

    from django.core.management import call_command

    settings.FAMILY_AUTH_SIGNUP_ORGS = value
    out = StringIO()
    call_command("ensure_signup_orgs", stdout=out, stderr=out)
    return out.getvalue()


def test_ensure_signup_orgs_creates_the_org_and_phr_can_sign_up_into_it(settings):
    from omop_core.models import Organization

    assert "Created sign-up organization phr" in _ensure(settings, ["phr:HealthKey PHR"])
    org = Organization.objects.get(slug="phr")
    assert (org.name, org.allows_patient_signup) == ("HealthKey PHR", True)

    signup = APIClient().post("/api/v1/orgs/phr/patient-signup/", {
        "email": "first@example.com", "password": "a-long-enough-pass-42",
    }, format="json")
    assert signup.status_code == 201, signup.data


def test_ensure_signup_orgs_is_idempotent_and_puts_sign_up_back(settings):
    from omop_core.models import Organization

    _ensure(settings, ["phr:HealthKey PHR"])
    Organization.objects.filter(slug="phr").update(allows_patient_signup=False, name="Renamed by an admin")

    assert "Turned on patient sign-up for phr" in _ensure(settings, ["phr:HealthKey PHR"])
    org = Organization.objects.get(slug="phr")
    assert org.allows_patient_signup is True
    assert org.name == "Renamed by an admin"  # an existing org keeps its own settings
    assert "is in place" in _ensure(settings, ["phr:HealthKey PHR"])
    assert Organization.objects.filter(slug="phr").count() == 1


def test_ensure_signup_orgs_reports_a_deactivated_org_without_reactivating_it(settings):
    from omop_core.models import Organization

    OrganizationFactory(slug="phr", name="HealthKey PHR", is_active=False, allows_patient_signup=True)
    assert "deactivated" in _ensure(settings, ["phr:HealthKey PHR"])
    assert Organization.objects.get(slug="phr").is_active is False


def test_ensure_signup_orgs_does_nothing_unset_and_rejects_a_malformed_entry(settings):
    from django.core.management.base import CommandError

    from omop_core.models import Organization

    before = Organization.objects.count()
    assert "nothing to do" in _ensure(settings, [])
    assert Organization.objects.count() == before
    for bad in (["phr"], ["Not A Slug:Name"], ["phr:"]):
        with pytest.raises(CommandError):
            _ensure(settings, bad)


# ── Verification links go to the installation the patient signed up on ──────

def _signup(client, slug, email="pat@example.com"):
    return client.post(f"/api/v1/orgs/{slug}/patient-signup/", {
        "email": email, "password": "a-long-enough-pass-42", "given_name": "Pat", "family_name": "Lee",
    }, format="json")


def test_a_phr_signup_gets_its_verification_link_to_phr(settings, mailoutbox):
    import re

    OrganizationFactory(slug="phr", name="HealthKey PHR", allows_patient_signup=True)
    settings.FAMILY_AUTH_APP_URLS = ["phr=https://phr.test"]
    assert _signup(APIClient(), "phr").status_code == 201

    message = mailoutbox[-1]
    assert message.subject == "Confirm your email address for HealthKey PHR"
    assert "for HealthKey PHR:" in message.body and "PROMOP" not in message.body
    link = re.search(r"https://\S+", message.body).group(0)
    assert link.startswith("https://phr.test/verify-email?token=")

    # phr's page completes it through the family API, then login works.
    token = link.split("token=", 1)[1]
    verified = APIClient().post(f"{BASE}/verify-email/", {"token": token}, format="json")
    assert verified.status_code == 200, verified.data
    assert _login(APIClient(), "pat@example.com", "a-long-enough-pass-42").status_code == 200


def test_the_resent_link_on_a_refused_login_also_goes_to_phr(settings, mailoutbox):
    OrganizationFactory(slug="phr", name="HealthKey PHR", allows_patient_signup=True)
    settings.FAMILY_AUTH_APP_URLS = ["phr=https://phr.test/"]
    _signup(APIClient(), "phr")
    mailoutbox.clear()

    assert _login(APIClient(), "pat@example.com", "a-long-enough-pass-42").status_code == 403
    assert "https://phr.test/verify-email?token=" in mailoutbox[-1].body


def test_other_signups_keep_promops_own_link(settings, mailoutbox):
    settings.APP_BASE_URL = "https://promop.test"
    settings.FAMILY_AUTH_APP_URLS = ["phr=https://phr.test"]
    OrganizationFactory(slug="clinic", name="A clinic", allows_patient_signup=True)
    _signup(APIClient(), "clinic")

    assert "https://promop.test/verify-email?token=" in mailoutbox[-1].body
    assert mailoutbox[-1].subject == "Confirm your email address for PROMOP"


def test_a_malformed_app_url_stops_the_deploy_and_is_ignored_by_mail(settings, mailoutbox):
    from django.core.management.base import CommandError

    for bad in (["phr"], ["phr=ftp://phr.test"], ["phr=https://phr.test/path"], ["Not A Slug=https://x.test"]):
        settings.FAMILY_AUTH_APP_URLS = bad
        with pytest.raises(CommandError):
            _ensure(settings, ["phr:HealthKey PHR"])

    settings.APP_BASE_URL = "https://promop.test"
    settings.FAMILY_AUTH_APP_URLS = ["phr=ftp://nope"]
    OrganizationFactory(slug="phr", name="HealthKey PHR", allows_patient_signup=True)
    _signup(APIClient(), "phr")
    assert "https://promop.test/verify-email?token=" in mailoutbox[-1].body

