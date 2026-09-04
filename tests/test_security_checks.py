from django.test import SimpleTestCase, override_settings

from patient_portal.checks import production_key_separation_check


class ProductionKeySeparationCheckTest(SimpleTestCase):
    def _check(self, **settings):
        with override_settings(**settings):
            return production_key_separation_check(None)

    def test_development_allows_fallback_keys(self):
        self.assertEqual(self._check(DEBUG=True, SECRET_KEY='django-secret'), [])

    def test_production_requires_both_dedicated_keys(self):
        errors = self._check(DEBUG=False, SECRET_KEY='django-secret')

        self.assertEqual({error.id for error in errors}, {
            'patient_portal.E001', 'patient_portal.E002',
        })

    def test_production_rejects_secret_key_reuse(self):
        errors = self._check(
            DEBUG=False,
            SECRET_KEY='django-secret',
            AUDIT_HMAC_KEY='django-secret',
            EXPORT_SIGNING_KEY='export-secret',
        )

        self.assertEqual([error.id for error in errors], ['patient_portal.E001'])

    def test_production_rejects_shared_audit_and_export_key(self):
        errors = self._check(
            DEBUG=False,
            SECRET_KEY='django-secret',
            AUDIT_HMAC_KEY='shared-secret',
            EXPORT_SIGNING_KEY='shared-secret',
        )

        self.assertEqual([error.id for error in errors], ['patient_portal.E003'])

    def test_production_accepts_separate_dedicated_keys(self):
        errors = self._check(
            DEBUG=False,
            SECRET_KEY='django-secret',
            AUDIT_HMAC_KEY='audit-secret',
            EXPORT_SIGNING_KEY='export-secret',
        )

        self.assertEqual(errors, [])
