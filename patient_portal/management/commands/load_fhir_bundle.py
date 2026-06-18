"""
load_fhir_bundle — management command to load a FHIR bundle JSON file into the DB.

Usage:
    python manage.py load_fhir_bundle <file> --org <slug>

Examples:
    python manage.py load_fhir_bundle /tmp/abc_bc.json --org abc-foundation
    python manage.py load_fhir_bundle data/bundle.json --org my-org --batch-size 5
"""
import io
import json
from unittest.mock import PropertyMock, patch

from django.core.files.uploadedfile import InMemoryUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

import patient_portal.api.views as views_module
from omop_core.models import Organization
from patient_portal.api.views import PatientInfoViewSet
from patient_portal.models import Identity


class Command(BaseCommand):
    help = "Load a FHIR bundle JSON file and assign all patients to an organization"

    def add_arguments(self, parser):
        parser.add_argument("file", help="Path to FHIR bundle JSON file")
        parser.add_argument("--org", required=True, help="Organization slug to assign patients to")
        parser.add_argument(
            "--batch-size",
            type=int,
            default=10,
            help="Number of patients per batch (default: 10)",
        )

    def handle(self, *args, **options):
        file_path = options["file"]
        org_slug = options["org"]
        batch_size = options["batch_size"]

        try:
            org = Organization.objects.get(slug=org_slug)
        except Organization.DoesNotExist:
            raise CommandError(f"Organization with slug '{org_slug}' does not exist.")

        identity = Identity.objects.filter(is_superuser=True).first()
        if not identity:
            raise CommandError("No superuser found. Create one first.")

        self.stdout.write(f"Loading {file_path} → org={org.name} (as {identity.email})")

        try:
            with open(file_path, "rb") as f:
                bundle = json.load(f)
        except FileNotFoundError:
            raise CommandError(f"File not found: {file_path}")
        except json.JSONDecodeError as e:
            raise CommandError(f"Invalid JSON: {e}")

        if bundle.get("resourceType") != "Bundle":
            raise CommandError("File must be a FHIR Bundle (resourceType: Bundle)")

        # Group entries by patient ID
        patient_entries: dict[str, list] = {}
        for entry in bundle.get("entry", []):
            res = entry.get("resource", {})
            if res.get("resourceType") == "Patient":
                pid = res["id"]
                patient_entries.setdefault(pid, []).insert(0, entry)
            else:
                subj = res.get("subject", {}).get("reference", "")
                if subj.startswith("Patient/"):
                    pid = subj.replace("Patient/", "")
                    patient_entries.setdefault(pid, []).append(entry)

        patient_ids = list(patient_entries.keys())
        total_batches = (len(patient_ids) + batch_size - 1) // batch_size
        self.stdout.write(f"Found {len(patient_ids)} patients → {total_batches} batches of {batch_size}")

        factory = APIRequestFactory()
        parsers = [JSONParser(), MultiPartParser(), FormParser()]
        total_created = total_updated = total_errors = 0

        original_gro = views_module.get_request_org
        views_module.get_request_org = lambda req: org

        try:
            for i in range(0, len(patient_ids), batch_size):
                batch_pids = patient_ids[i : i + batch_size]
                batch_entries = [e for pid in batch_pids for e in patient_entries[pid]]
                batch_bundle = {
                    "resourceType": "Bundle",
                    "type": "collection",
                    "entry": batch_entries,
                }
                content = json.dumps(batch_bundle).encode()

                fake_file = InMemoryUploadedFile(
                    io.BytesIO(content), "file", "batch.json",
                    "application/json", len(content), None,
                )

                raw_request = factory.post(
                    "/api/patient-info/upload_fhir/", {}, format="json"
                )
                raw_request.user = identity
                drf_req = Request(raw_request, parsers=parsers)
                drf_req.user = identity

                # Reset DB connection before each batch to avoid aborted-transaction
                # state bleeding over from a prior failed patient.
                connection.close()

                viewset = PatientInfoViewSet()
                # Patch FILES and data on the Request class so DRF skips multipart parsing
                with patch.object(type(drf_req), "FILES", new_callable=PropertyMock,
                                  return_value={"file": fake_file}), \
                     patch.object(type(drf_req), "data", new_callable=PropertyMock,
                                  return_value={}):
                    response = viewset.upload_fhir(drf_req)

                data = response.data if hasattr(response, "data") else {}
                created = data.get("created_count", 0)
                updated = data.get("updated_count", 0)
                errors = data.get("errors", [])
                total_created += created
                total_updated += updated
                total_errors += len(errors)

                batch_num = i // batch_size + 1
                self.stdout.write(
                    f"  [{batch_num}/{total_batches}] created={created} updated={updated} errors={len(errors)}"
                )
                if response.status_code not in (200, 201):
                    self.stderr.write(
                        f"    RESPONSE {response.status_code}: {json.dumps(data, default=str)[:400]}"
                    )
                for err in errors[:3]:
                    self.stderr.write(f"    ERR: {err}")
        finally:
            views_module.get_request_org = original_gro

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. created={total_created} updated={total_updated} errors={total_errors}"
            )
        )
