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


def _link_patient(identity, person_id, given=None, family=None):
    from omop_core.models import Person
    from patient_portal.models import PatientUser

    person = Person.objects.create(person_id=person_id, given_name=given, family_name=family)
    PatientUser.objects.create(identity=identity, person=person)
    return person


def test_me_names_a_patient_account_after_its_patient_record(account):
    """phr's header shows /me's name. For a patient that is the name on their
    record (what the Health Profile edits), not the account's display name,
    which can be anything an admin typed (on staging: "Random")."""
    account.name = "Random"
    account.save(update_fields=["name"])
    _link_patient(account, 991_001, given="Dann Alysia", family="Rempel")
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {_login(APIClient()).data['access']}")

    me = client.get(f"{BASE}/me/")
    assert (me.data["first_name"], me.data["last_name"]) == ("Dann Alysia", "Rempel")


def test_me_falls_back_to_the_account_name_when_the_record_has_none(account):
    _link_patient(account, 991_002, given="  ", family=None)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {_login(APIClient()).data['access']}")

    me = client.get(f"{BASE}/me/")
    assert (me.data["first_name"], me.data["last_name"]) == ("Dana", "Scully")


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


def test_a_sibling_listed_in_the_audience_can_check_aud_and_promop_still_accepts(settings, account):
    """Siblings (exact, soc, labs) verify `aud` once FAMILY_AUTH_AUDIENCE names
    them. Adding them must not change what PROMOP itself accepts."""
    settings.FAMILY_AUTH_AUDIENCE = ["promop-api", "exact-api", "soc-api", "labs-api"]
    access = _login(APIClient()).data["access"]
    public = jwt.PyJWK(APIClient().get(f"{BASE}/jwks/").data["keys"][0]).key

    claims = jwt.decode(access, public, algorithms=["RS256"], audience="exact-api", issuer=ISSUER)
    assert claims["aud"] == ["promop-api", "exact-api", "soc-api", "labs-api"]
    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(access, public, algorithms=["RS256"], audience="trials-elsewhere", issuer=ISSUER)

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    assert client.get("/api/v1/user/").status_code == 200


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


def test_deleting_an_org_removes_its_patients_family_sessions():
    """delete_org_patients deletes identities with raw SQL, which a family
    refresh token used to block: Django's foreign keys are deferred, so the
    violation surfaced at COMMIT on staging. The identity now goes through the
    ORM, so every table that points at it cascades: the family session, the
    password history a reset writes, and the patient_user with its consents."""
    from omop_core.models import Organization
    from omop_core.services.organization_cleanup import delete_organization_with_patient_cascade
    from patient_portal.models import PasswordHistory, PatientConsent, PatientUser
    from patient_portal.services import record_password

    org = OrganizationFactory(slug="phr", allows_patient_signup=True)
    client = APIClient()
    assert _signup(client, "phr", email="gone@example.com").status_code == 201
    identity = Identity.objects.get(email="gone@example.com")
    identity.mark_email_verified()
    record_password(identity)
    patient_user = PatientUser.objects.get(identity=identity)
    PatientConsent.objects.create(patient_user=patient_user, consent_type="data_sharing", consent_granted=True)
    assert _login(client, "gone@example.com", "a-long-enough-pass-42").status_code == 200
    assert RefreshToken.objects.filter(identity=identity).exists()

    delete_organization_with_patient_cascade(org)

    assert not Organization.objects.filter(pk=org.pk).exists()
    assert not Identity.objects.filter(pk=identity.pk).exists()
    assert not RefreshToken.objects.filter(identity_id=identity.pk).exists()
    assert not PasswordHistory.objects.filter(identity_id=identity.pk).exists()
    assert not PatientUser.objects.filter(pk=patient_user.pk).exists()
    assert not PatientConsent.objects.filter(patient_user_id=patient_user.pk).exists()


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
    assert message.body.rstrip().endswith("— HealthKey PHR")
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
    assert mailoutbox[-1].body.rstrip().endswith("— The HealthKey team")


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



# ── Password-reset links go there too ─────────────────────────────────────────

def _verified_phr_patient(settings, mailoutbox):
    import re

    OrganizationFactory(slug="phr", name="HealthKey PHR", allows_patient_signup=True)
    settings.FAMILY_AUTH_APP_URLS = ["phr=https://phr.test"]
    _signup(APIClient(), "phr")
    token = re.search(r"token=(\S+)", mailoutbox[-1].body).group(1)
    assert APIClient().post(f"{BASE}/verify-email/", {"token": token}, format="json").status_code == 200
    mailoutbox.clear()


def test_a_phr_patient_who_forgot_their_password_resets_it_on_phr(settings, mailoutbox,
                                                                 django_capture_on_commit_callbacks):
    import re

    from django.core.cache import cache

    _verified_phr_patient(settings, mailoutbox)
    settings.FAMILY_AUTH_RESET_APPS = ["phr"]  # phr serves /reset-password
    cache.clear()  # the reset endpoint's rate limit counts every earlier test's requests

    with django_capture_on_commit_callbacks(execute=True):
        assert APIClient().post("/api/v1/auth/request-reset/", {"email": "pat@example.com"}).status_code == 200

    message = mailoutbox[-1]
    assert message.subject == "Reset your HealthKey PHR password"
    assert "We received a request to reset the password for your HealthKey PHR account." in message.body
    assert "PROMOP" not in message.body and "administrator" not in message.body
    assert message.body.rstrip().endswith("— HealthKey PHR")
    link = re.search(r"https://\S+", message.body).group(0)
    assert link.startswith("https://phr.test/reset-password?uid=")

    # phr's page completes it through PROMOP's API, then the new password signs in.
    uid, token = re.search(r"uid=([^&]+)&token=(\S+)", link).groups()
    reset = APIClient().post("/api/v1/auth/reset-password/",
                             {"uid": uid, "token": token, "new_password": "another-long-pass-77"}, format="json")
    assert reset.status_code == 200, reset.data
    assert _login(APIClient(), "pat@example.com", "another-long-pass-77").status_code == 200


def test_an_administrators_reset_says_so_and_other_accounts_keep_promops_link(settings, mailoutbox):
    from patient_portal.api.password_reset import send_password_reset_email
    from patient_portal.models import Identity

    settings.APP_BASE_URL = "https://promop.test/"
    settings.FAMILY_AUTH_APP_URLS = ["phr=https://phr.test"]
    identity = Identity.objects.create_user(email="staff-made@example.com", password="a-long-enough-pass-42")

    send_password_reset_email(identity, by_administrator=True)

    message = mailoutbox[-1]
    assert message.subject == "Reset your PROMOP password"
    assert "An administrator has started a password reset for your PROMOP account." in message.body
    assert "https://promop.test/reset-password?uid=" in message.body
    assert message.body.rstrip().endswith("— The HealthKey team")


def test_other_anonymous_requests_do_not_use_up_the_reset_limit(settings):
    """The reset limit (5 a minute) counts reset requests only: it used to
    share the general anonymous history, so a few anonymous calls from a
    shared network refused the first reset with 429."""
    from django.core.cache import cache

    cache.clear()
    client = APIClient()
    for _ in range(6):
        client.get("/api/v1/phr/signup/policy/")

    assert client.post("/api/v1/auth/request-reset/", {"email": "someone@example.com"}).status_code == 200
    for _ in range(4):
        client.post("/api/v1/auth/request-reset/", {"email": "someone@example.com"})
    assert client.post("/api/v1/auth/request-reset/", {"email": "someone@example.com"}).status_code == 429



def test_an_installation_without_a_reset_page_resets_on_promops(settings, mailoutbox,
                                                               django_capture_on_commit_callbacks):
    """phr serves /verify-email but not /reset-password: its patients' reset
    links stay on PROMOP's page, and the email still names phr."""
    from django.core.cache import cache

    _verified_phr_patient(settings, mailoutbox)
    settings.APP_BASE_URL = "https://promop.test"
    settings.FAMILY_AUTH_RESET_APPS = []
    cache.clear()

    with django_capture_on_commit_callbacks(execute=True):
        APIClient().post("/api/v1/auth/request-reset/", {"email": "pat@example.com"})

    assert mailoutbox[-1].subject == "Reset your HealthKey PHR password"
    assert "https://promop.test/reset-password?uid=" in mailoutbox[-1].body
    assert "phr.test" not in mailoutbox[-1].body


@pytest.mark.parametrize("case", ["staff", "org_admin", "analyst", "expired", "inactive_org"])
def test_who_runs_or_has_left_an_installation_keeps_promops_links(settings, mailoutbox, case):
    from datetime import timedelta

    from django.utils import timezone

    from omop_core.models import GroupAccess
    from patient_portal.api.password_reset import send_password_reset_email
    from patient_portal.models import Identity

    settings.APP_BASE_URL = "https://promop.test"
    settings.FAMILY_AUTH_APP_URLS = ["phr=https://phr.test"]
    settings.FAMILY_AUTH_RESET_APPS = ["phr"]
    org = OrganizationFactory(slug="phr", name="HealthKey PHR", is_active=case != "inactive_org")
    identity = Identity.objects.create_user(email=f"{case}@example.com", password="a-long-enough-pass-42",
                                            is_staff=case == "staff")
    GroupAccess.objects.create(
        identity=identity, org=org, role=case if case in ("org_admin", "analyst") else "patient",
        expires_at=timezone.now() - timedelta(days=1) if case == "expired" else None,
    )

    send_password_reset_email(identity, by_administrator=True)

    assert mailoutbox[-1].subject == "Reset your PROMOP password"
    assert "https://promop.test/reset-password?uid=" in mailoutbox[-1].body


def test_a_signed_in_caller_is_rate_limited_too():
    """The reset views take no authentication: an AnonRateThrottle skips
    anyone signed in, so a signed-in caller could email resets without end."""
    from django.core.cache import cache

    from patient_portal.models import Identity

    cache.clear()
    Identity.objects.create_user(email="signed-in@example.com", password="x-long-pass-123")
    client = APIClient()
    # A real session (force_authenticate would bypass the views' authentication classes).
    assert client.post("/api/v1/auth/login/", {"username": "signed-in@example.com", "password": "x-long-pass-123"},
                       format="json").status_code == 200
    cache.clear()
    statuses = [client.post("/api/v1/auth/request-reset/", {"email": "someone@example.com"}).status_code
                for _ in range(6)]

    assert statuses == [200] * 5 + [429]


def test_asking_for_links_does_not_use_up_the_tries_at_a_new_password():
    from django.core.cache import cache

    cache.clear()
    client = APIClient()
    for _ in range(5):
        client.post("/api/v1/auth/request-reset/", {"email": "someone@example.com"})

    completing = client.post("/api/v1/auth/reset-password/", {"uid": "x", "token": "y", "new_password": "z"})
    assert completing.status_code == 400  # refused for the bad link, not for the rate



# ── The reset email is sent after the response, on a worker ─────────────────

def test_a_reset_request_queues_the_email_and_names_only_the_account(settings, mailoutbox,
                                                                    django_capture_on_commit_callbacks):
    """The request does not wait for the mail provider, which only an existing
    account would make it do: the worker gets the account's id, never its
    address or the link."""
    from unittest import mock

    from django.core.cache import cache

    from patient_portal.models import Identity
    from patient_portal.tasks import send_password_reset_task

    cache.clear()
    settings.CELERY_BROKER_URL = "redis://broker.test:6379/0"
    identity = Identity.objects.create_user(email="queued@example.com", password="a-long-enough-pass-42")

    with mock.patch.object(send_password_reset_task, "delay") as delay, \
            django_capture_on_commit_callbacks(execute=True):
        known = APIClient().post("/api/v1/auth/request-reset/", {"email": "queued@example.com"})
        unknown = APIClient().post("/api/v1/auth/request-reset/", {"email": "nobody@example.com"})

    assert known.data == unknown.data and known.status_code == unknown.status_code == 200
    delay.assert_called_once_with(identity.pk, False)
    assert mailoutbox == []  # nothing sent during either request

    send_password_reset_task.run(identity.pk, False)  # the worker
    assert mailoutbox[-1].to == ["queued@example.com"]
    assert "We received a request to reset the password" in mailoutbox[-1].body


def test_the_worker_sends_nothing_to_an_account_that_closed_or_lost_its_password(mailoutbox):
    from patient_portal.models import Identity
    from patient_portal.tasks import send_password_reset_task

    gone = Identity.objects.create_user(email="gone@example.com", password="a-long-enough-pass-42")
    gone.is_active = False
    gone.save(update_fields=["is_active"])

    send_password_reset_task.run(gone.pk, False)
    send_password_reset_task.run(999_999_999, False)

    assert mailoutbox == []


# Requests run in autocommit, so on_commit runs the hand-off inside the
# request itself: these tests run that way (transaction=True), as production does.

@pytest.mark.django_db(transaction=True)
def test_in_a_request_the_email_is_queued_and_not_sent(settings, mailoutbox):
    from unittest import mock

    from django.core.cache import cache

    from patient_portal.models import Identity
    from patient_portal.tasks import send_password_reset_task

    cache.clear()
    settings.CELERY_BROKER_URL = "redis://broker.test:6379/0"
    identity = Identity.objects.create_user(email="live@example.com", password="a-long-enough-pass-42")

    with mock.patch.object(send_password_reset_task, "delay") as delay:
        assert APIClient().post("/api/v1/auth/request-reset/", {"email": "live@example.com"}).status_code == 200

    delay.assert_called_once_with(identity.pk, False)
    assert mailoutbox == []


@pytest.mark.django_db(transaction=True)
def test_a_broker_that_is_down_fails_nobody_and_the_email_still_goes(settings, mailoutbox):
    """The broker refusing the task must not answer differently for an
    address with an account (a 500 would tell them apart), and the email
    must not be lost: it is sent inline instead."""
    from unittest import mock

    from django.core.cache import cache
    from kombu.exceptions import OperationalError

    from patient_portal.models import Identity
    from patient_portal.tasks import send_password_reset_task

    cache.clear()
    settings.CELERY_BROKER_URL = "redis://broker.test:6379/0"
    Identity.objects.create_user(email="down@example.com", password="a-long-enough-pass-42")
    client = APIClient(raise_request_exception=False)

    with mock.patch.object(send_password_reset_task, "delay", side_effect=OperationalError("broker down")):
        known = client.post("/api/v1/auth/request-reset/", {"email": "down@example.com"})
        unknown = client.post("/api/v1/auth/request-reset/", {"email": "nobody@example.com"})

    assert (known.status_code, known.data) == (unknown.status_code, unknown.data) == (200, unknown.data)
    assert [m.to for m in mailoutbox] == [["down@example.com"]]


def test_the_worker_tries_a_failing_mail_provider_again(mailoutbox):
    from unittest import mock

    from celery.exceptions import Retry

    from patient_portal.models import Identity
    from patient_portal.tasks import send_password_reset_task

    identity = Identity.objects.create_user(email="retry@example.com", password="a-long-enough-pass-42")

    with mock.patch("patient_portal.api.password_reset.send_mail", side_effect=OSError("provider down")), \
            mock.patch.object(send_password_reset_task, "retry", side_effect=Retry()) as retry:
        with pytest.raises(Retry):
            send_password_reset_task.run(identity.pk, False)

    assert retry.call_args.kwargs["countdown"] == 60
    assert send_password_reset_task.max_retries == 3
