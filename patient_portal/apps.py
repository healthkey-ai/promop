from django.apps import AppConfig


class PatientPortalConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "patient_portal"

    def ready(self):
        import patient_portal.checks  # noqa: F401
