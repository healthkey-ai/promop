"""Sending through Twilio SendGrid's SMTP relay (settings._email_settings).

SMTP rather than SendGrid's HTTP API: the API refuses a part whose type has
parameters, such as ``text/calendar; method=REQUEST`` (what Outlook reads as a
meeting request), while the relay sends the MIME message exactly as Django
builds it.
"""
import json

from django.core.mail.backends.smtp import EmailBackend

# SendGrid's per-message settings (its X-SMTPAPI header): no open, click or
# subscription tracking, whatever the account says. Click tracking rewrites
# every link, so a password-reset or invitation token would pass through
# SendGrid's redirect; subscription tracking adds SendGrid's own unsubscribe
# footer, and the apps that need one write their own List-Unsubscribe.
TRACKING_OFF = json.dumps({'filters': {
    name: {'settings': {'enable': 0}} for name in ('clicktrack', 'opentrack', 'subscriptiontrack')
}})


class SendGridSMTPBackend(EmailBackend):
    """Django's SMTP backend, with tracking switched off on every message."""

    def send_messages(self, email_messages):
        for message in email_messages:
            message.extra_headers['X-SMTPAPI'] = TRACKING_OFF
        return super().send_messages(email_messages)
