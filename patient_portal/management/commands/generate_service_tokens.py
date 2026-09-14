"""Generate individual service grants without exposing secrets in command output."""
import json
import os
import secrets

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError

from patient_portal.service_tokens import service_credentials


class Command(BaseCommand):
    help = "Generate SERVICE_AUTH_TOKENS JSON in a new private file (does not install it)."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--service", action="append", required=True,
                            help="Service ID=scopes; repeat once per service.")
        parser.add_argument("--output", required=True, help="New secret file; never overwritten.")

    def handle(self, *args, **options):
        grants = {}
        for specification in options["service"]:
            service_id, separator, scope = specification.partition("=")
            if not separator or service_id in grants:
                raise CommandError("Use a unique service ID=scopes for each --service.")
            grants[service_id] = {"token": secrets.token_urlsafe(48), "scopes": scope}
        try:
            service_credentials(grants)
        except ImproperlyConfigured:
            raise CommandError("Invalid service configuration.") from None
        try:
            descriptor = os.open(options["output"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:
            raise CommandError("Cannot create output file; it must not already exist.") from None
        with os.fdopen(descriptor, "w") as output:
            json.dump(grants, output)
            output.write("\n")
        self.stdout.write("Created private service-token configuration. Install it as SERVICE_AUTH_TOKENS.")
