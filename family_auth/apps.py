from django.apps import AppConfig


class FamilyAuthConfig(AppConfig):
    """PROMOP as identity host for the HealthKey family (phr#43).

    Issues the family's RS256 tokens for PROMOP's own identities, so a phr
    installation signs its users in against PROMOP accounts instead of
    holding credentials itself. See docs/family-auth.md.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "family_auth"
    verbose_name = "Family auth"
