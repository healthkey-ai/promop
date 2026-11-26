"""promop sends email through SendGrid's SMTP relay or Mailgun, chosen from the environment."""
import json
from email import message_from_string
from unittest import mock

from django.core.mail import EmailMultiAlternatives, get_connection

from promop.mail import SendGridSMTPBackend
from promop.settings import _email_settings

SENDGRID = 'promop.mail.SendGridSMTPBackend'
MAILGUN = 'anymail.backends.mailgun.EmailBackend'
CONSOLE = 'django.core.mail.backends.console.EmailBackend'


def test_nothing_configured_writes_to_the_console():
    assert _email_settings({}) == {'EMAIL_BACKEND': CONSOLE, 'ANYMAIL': {}}


def test_mailgun_needs_its_key_and_domain():
    assert _email_settings({'MAILGUN_API_KEY': 'k'})['EMAIL_BACKEND'] == CONSOLE
    email = _email_settings({'MAILGUN_API_KEY': 'k', 'MAILGUN_SENDER_DOMAIN': 'mg.example.org'})
    assert email == {'EMAIL_BACKEND': MAILGUN,
                     'ANYMAIL': {'MAILGUN_API_KEY': 'k', 'MAILGUN_SENDER_DOMAIN': 'mg.example.org'}}


def test_sendgrid_signs_in_to_its_relay_with_the_key():
    email = _email_settings({'SENDGRID_API_KEY': 'SG.k', 'SENDGRID_SMTP_HOST': 'smtp.eu.sendgrid.net'})
    assert email['EMAIL_BACKEND'] == SENDGRID
    assert (email['EMAIL_HOST'], email['EMAIL_PORT'], email['EMAIL_USE_TLS']) == ('smtp.eu.sendgrid.net', 587, True)
    assert (email['EMAIL_HOST_USER'], email['EMAIL_HOST_PASSWORD']) == ('apikey', 'SG.k')
    assert email['EMAIL_TIMEOUT'] == 30


def test_sendgrid_defaults_to_its_global_relay():
    assert _email_settings({'SENDGRID_API_KEY': 'SG.k'})['EMAIL_HOST'] == 'smtp.sendgrid.net'


def test_a_pasted_newline_is_stripped():
    email = _email_settings({'SENDGRID_API_KEY': 'SG.k\n', 'SENDGRID_SMTP_HOST': ' smtp.eu.sendgrid.net\n'})
    assert (email['EMAIL_HOST_PASSWORD'], email['EMAIL_HOST']) == ('SG.k', 'smtp.eu.sendgrid.net')
    assert _email_settings({'SENDGRID_API_KEY': '\n'})['EMAIL_BACKEND'] == CONSOLE


def test_sendgrid_wins_when_both_are_set():
    env = {'SENDGRID_API_KEY': 'SG.k', 'MAILGUN_API_KEY': 'k', 'MAILGUN_SENDER_DOMAIN': 'mg.example.org'}
    assert _email_settings(env)['EMAIL_BACKEND'] == SENDGRID


def test_email_backend_overrides_the_choice():
    assert _email_settings({'SENDGRID_API_KEY': 'SG.k', 'EMAIL_BACKEND': CONSOLE})['EMAIL_BACKEND'] == CONSOLE


def test_the_relay_gets_the_message_as_built_with_tracking_off():
    """A calendar part keeps its method (SendGrid's API would refuse it), and
    every message asks SendGrid for no tracking."""
    message = EmailMultiAlternatives('Your check-in', 'https://app.example.org/checkin', 'noreply@example.org',
                                     ['a@example.org'])
    message.attach_alternative('BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nEND:VCALENDAR\r\n',
                               'text/calendar; method=REQUEST; charset=utf-8')
    with mock.patch('smtplib.SMTP') as smtp:
        connection = get_connection(SENDGRID, host='smtp.eu.sendgrid.net', port=587, use_tls=True,
                                    username='apikey', password='SG.k', timeout=30)
        assert isinstance(connection, SendGridSMTPBackend)
        assert connection.send_messages([message]) == 1

    assert (smtp.call_args.args, smtp.call_args.kwargs['timeout']) == (('smtp.eu.sendgrid.net', 587), 30)
    smtp.return_value.starttls.assert_called_once()
    smtp.return_value.login.assert_called_once_with('apikey', 'SG.k')
    sent = message_from_string(smtp.return_value.sendmail.call_args.args[2].decode())
    filters = json.loads(sent['X-SMTPAPI'])['filters']
    assert {name: f['settings']['enable'] for name, f in filters.items()} == {
        'clicktrack': 0, 'opentrack': 0, 'subscriptiontrack': 0}
    [calendar] = [part for part in sent.walk() if part.get_content_type() == 'text/calendar']
    assert calendar.get_param('method') == 'REQUEST'
