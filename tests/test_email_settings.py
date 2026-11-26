"""promop sends email through SendGrid or Mailgun, chosen from the environment."""
import json
from unittest import mock

import requests
from django.core.mail import EmailMessage, get_connection

from promop.settings import _email_settings

SENDGRID = 'anymail.backends.sendgrid.EmailBackend'
MAILGUN = 'anymail.backends.mailgun.EmailBackend'
CONSOLE = 'django.core.mail.backends.console.EmailBackend'


def test_nothing_configured_writes_to_the_console():
    assert _email_settings({}) == (CONSOLE, {})


def test_mailgun_needs_its_key_and_domain():
    assert _email_settings({'MAILGUN_API_KEY': 'k'})[0] == CONSOLE
    backend, anymail = _email_settings({'MAILGUN_API_KEY': 'k', 'MAILGUN_SENDER_DOMAIN': 'mg.example.org'})
    assert backend == MAILGUN
    assert anymail == {'MAILGUN_API_KEY': 'k', 'MAILGUN_SENDER_DOMAIN': 'mg.example.org'}


def test_sendgrid_with_its_key_and_tracking_off():
    backend, anymail = _email_settings({'SENDGRID_API_KEY': 'SG.k'})
    assert backend == SENDGRID
    assert anymail == {
        'SENDGRID_API_KEY': 'SG.k',
        'SENDGRID_SEND_DEFAULTS': {'track_clicks': False, 'track_opens': False},
    }


def test_sendgrid_takes_a_regional_api():
    _, anymail = _email_settings({'SENDGRID_API_KEY': 'SG.k', 'SENDGRID_API_URL': 'https://api.eu.sendgrid.com/v3/'})
    assert anymail['SENDGRID_API_URL'] == 'https://api.eu.sendgrid.com/v3/'


def test_sendgrid_wins_when_both_are_set():
    env = {'SENDGRID_API_KEY': 'SG.k', 'MAILGUN_API_KEY': 'k', 'MAILGUN_SENDER_DOMAIN': 'mg.example.org'}
    assert _email_settings(env)[0] == SENDGRID


def test_email_backend_overrides_the_choice():
    assert _email_settings({'SENDGRID_API_KEY': 'SG.k', 'EMAIL_BACKEND': CONSOLE})[0] == CONSOLE


def test_a_sendgrid_message_goes_to_the_regional_api_untracked(settings):
    _, settings.ANYMAIL = _email_settings(
        {'SENDGRID_API_KEY': 'SG.k', 'SENDGRID_API_URL': 'https://api.eu.sendgrid.com/v3/'})
    sent = requests.Response()
    sent.status_code, sent._content = 202, b''
    with mock.patch('requests.Session.request', return_value=sent) as request:
        EmailMessage('Reset', 'https://app.example.org/reset#t', 'noreply@example.org', ['a@example.org'],
                     connection=get_connection(SENDGRID)).send()
    call = request.call_args.kwargs
    assert (call['method'], call['url']) == ('POST', 'https://api.eu.sendgrid.com/v3/mail/send')
    assert call['headers']['Authorization'] == 'Bearer SG.k'
    tracking = json.loads(call['data'])['tracking_settings']
    assert tracking == {'click_tracking': {'enable': False}, 'open_tracking': {'enable': False}}
