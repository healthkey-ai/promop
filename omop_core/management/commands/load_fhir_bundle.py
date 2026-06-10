"""
Load a FHIR R4 Bundle JSON file directly into the database.

Reuses the upload_fhir view logic via a mocked DRF request — no HTTP needed.

Signal suppression (deferred PatientInfo refresh) is handled inside upload_fhir
via suppress_patient_info_refresh(). Each patient gets exactly one refresh call
at the end of its processing block, not one per OMOP row written.

Usage:
    python manage.py load_fhir_bundle data/mm_patients_400.json
    python manage.py load_fhir_bundle data/batches/mm_batch_01.json
"""
import io
import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Load a FHIR R4 Bundle JSON file into the database"

    def add_arguments(self, parser):
        parser.add_argument("bundle_path", type=str, help="Path to FHIR bundle JSON file")
        parser.add_argument(
            "--batch-size",
            type=int,
            default=50,
            help="Process N patients per batch (default: 50)",
        )

    def handle(self, *args, **options):
        bundle_path = Path(options["bundle_path"])
        if not bundle_path.exists():
            raise CommandError(f"File not found: {bundle_path}")

        self.stdout.write(f"Loading {bundle_path} ...")
        with bundle_path.open() as f:
            bundle = json.load(f)

        if bundle.get("resourceType") != "Bundle":
            raise CommandError("File is not a FHIR Bundle")

        entries = bundle.get("entry", [])
        patients = [e for e in entries if e["resource"]["resourceType"] == "Patient"]
        self.stdout.write(f"  {len(patients)} patients, {len(entries)} total resources")

        User = get_user_model()
        user = User.objects.filter(is_superuser=True).first()
        if not user:
            raise CommandError("No superuser found.")
        self.stdout.write(f"  Authenticating as: {getattr(user, User.USERNAME_FIELD, user.pk)}")

        batch_size = options["batch_size"]
        patient_ids = [e["resource"]["id"] for e in patients]
        total_batches = (len(patient_ids) + batch_size - 1) // batch_size

        total_created = 0
        total_errors = []

        for batch_num, batch_start in enumerate(range(0, len(patient_ids), batch_size), 1):
            batch_pids = set(patient_ids[batch_start: batch_start + batch_size])
            batch_entries = [
                e for e in entries
                if (e["resource"]["resourceType"] == "Patient" and e["resource"]["id"] in batch_pids)
                or e["resource"].get("subject", {}).get("reference", "").replace("Patient/", "") in batch_pids
            ]

            batch_json = json.dumps(
                {"resourceType": "Bundle", "type": "collection", "entry": batch_entries}
            ).encode()

            self.stdout.write(
                f"  Batch {batch_num}/{total_batches}: {len(batch_pids)} patients ...",
                ending="",
            )
            self.stdout.flush()

            result = self._run_batch(batch_json, user)
            created = result.get("created_count", 0)
            errors = result.get("errors", [])
            total_created += created
            total_errors.extend(errors)
            self.stdout.write(f" created={created} errors={len(errors)}")

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Created: {total_created}, errors: {len(total_errors)}"
        ))
        if total_errors:
            self.stdout.write(self.style.WARNING("Errors (first 20):"))
            for e in total_errors[:20]:
                self.stdout.write(f"  {e}")

    def _run_batch(self, batch_json_bytes, user):
        from django.core.files.uploadedfile import InMemoryUploadedFile
        from rest_framework.test import APIRequestFactory
        from patient_portal.api.views import PatientInfoViewSet

        factory = APIRequestFactory()
        file_obj = InMemoryUploadedFile(
            file=io.BytesIO(batch_json_bytes),
            field_name="file",
            name="bundle.json",
            content_type="application/json",
            size=len(batch_json_bytes),
            charset=None,
        )

        request = factory.post(
            "/api/patient-info/upload_fhir/",
            data={"file": file_obj},
            format="multipart",
        )
        request.user = user
        request.auth = None

        view = PatientInfoViewSet.as_view({"post": "upload_fhir"})
        response = view(request)

        if hasattr(response, "data"):
            return response.data
        return {"created_count": 0, "errors": [f"HTTP {response.status_code}"]}
