from io import StringIO

from django.core.management import call_command, CommandError
import pytest

from patient_portal.models import Identity


pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_env(monkeypatch):
    monkeypatch.setenv('ADMIN_EMAIL', 'admin@example.invalid')
    monkeypatch.setenv('ADMIN_PASSWORD', 'new-admin-password')


@pytest.mark.parametrize('issuer', ['healthkey-phr', 'urn:service'])
def test_shared_email_updates_only_local_password(admin_env, issuer):
    # Existing privileges must stay intact, even if this is a staff-only account.
    local = Identity.objects.create_user(
        email='Admin@example.invalid', password='old-password', is_staff=True,
    )
    external = Identity.objects.create(
        issuer=issuer, sub='external-admin', email='admin@example.invalid',
    )
    external.set_unusable_password()
    external.save()
    before = dict(Identity.objects.values().get(pk=external.pk))

    call_command('setup_admin', stdout=StringIO())

    local.refresh_from_db()
    assert local.check_password('new-admin-password')
    assert local.is_staff and not local.is_superuser
    assert Identity.objects.values().get(pk=external.pk) == before
    assert Identity.objects.count() == 2


def test_external_identity_does_not_prevent_local_admin_creation(admin_env):
    external = Identity.objects.create(
        issuer='healthkey-phr', sub='external', email='admin@example.invalid',
    )
    before = dict(Identity.objects.values().get(pk=external.pk))
    call_command('setup_admin', stdout=StringIO())
    local = Identity.objects.get(issuer='urn:local')
    assert local.is_staff and local.is_superuser
    assert local.check_password('new-admin-password')
    assert Identity.objects.values().get(pk=external.pk) == before


def test_new_admin_setup_is_repeatable(admin_env):
    call_command('setup_admin', stdout=StringIO())
    call_command('setup_admin', stdout=StringIO())
    local = Identity.objects.get()
    assert local.is_staff and local.is_superuser
    assert local.check_password('new-admin-password')


def test_duplicate_local_accounts_are_not_arbitrarily_reset(admin_env):
    for email in ['admin@example.invalid', 'Admin@example.invalid']:
        Identity.objects.create_user(email=email, password='original-password')
    before = list(Identity.objects.order_by('pk').values())
    with pytest.raises(CommandError, match='multiple local identities'):
        call_command('setup_admin')
    assert list(Identity.objects.order_by('pk').values()) == before


def test_missing_password_does_not_change_accounts(admin_env, monkeypatch):
    monkeypatch.delenv('ADMIN_PASSWORD')
    with pytest.raises(CommandError, match='ADMIN_PASSWORD'):
        call_command('setup_admin')
    assert not Identity.objects.exists()
