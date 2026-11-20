from django.apps import AppConfig


class PatientPortalConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "patient_portal"

    def ready(self):
        import patient_portal.checks  # noqa: F401
        from django.db.models.signals import post_save
        from prolog_surveys.models import SurveyResponse

        from patient_portal.survey_record_mapping import on_response_saved

        # A submitted response to a survey version with a record mapping writes
        # what it gives into the patient's record (survey_record_mapping).
        post_save.connect(on_response_saved, sender=SurveyResponse, dispatch_uid='survey_record_mapping')
