from django import template
from django.conf import settings

register = template.Library()


@register.simple_tag
def staff_sign_in_options():
    """For the admin's sign-in page: the providers staff sign in with, and
    whether the password form is offered (patient_portal.staff_sign_in)."""
    from patient_portal.staff_sign_in import providers

    return {'providers': list(providers().values()), 'password': settings.STAFF_PASSWORD_SIGN_IN}
