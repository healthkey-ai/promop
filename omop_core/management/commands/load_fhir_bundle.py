"""
Load a FHIR R4 Bundle JSON file directly into the database.

Reuses the upload_fhir view logic via a mocked DRF request — no HTTP needed.

Key optimisation: the upload_fhir view calls refresh_patient_info once per
patient, AND post_save signals on every OMOP table call it again for every
row written (~60 calls per patient over a remote DB = very slow).

This command suppresses all those mid-load refreshes and does a single batch
refresh at the end, cutting the per-patient overhead from ~60 round-trips to 1.

Usage:
    python manage.py load_fhir_bundle data/mm_patients_400.json
    python manage.py load_fhir_bundle data/batches/mm_batch_01.json
"""
import io
import json
from contextlib import contextmanager
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


@contextmanager
def _suppress_patient_info_refresh():
    """
    Temporarily replace refresh_patient_info with a no-op in views.py.
    The signals already honour _skip_patient_info_refresh; we patch the
    explicit call in the view here.
    """
    import patient_portal.api.views as views_module
    from omop_core.services.patient_info_service import refresh_patient_info as real_refresh

    _collected_persons = []

    def _noop_refresh(person):
        _collected_persons.append(person)
        return None  # view ignores the return value after the patch block

    views_module.refresh_patient_info = _noop_refresh
    try:
        yield _collected_persons
    finally:
        views_module.refresh_patient_info = real_refresh


class Command(BaseCommand):
    help = "Load a FHIR R4 Bundle JSON file into the database (deferred refresh)"

    def add_arguments(self, parser):
        parser.add_argument("bundle_path", type=str, help="Path to FHIR bundle JSON file")
        parser.add_argument(
            "--batch-size",
            type=int,
            default=50,
            help="Process N patients per batch (default: 50)",
        )

    def handle(self, *args, **options):
        # Patch _skip_patient_info_refresh onto saves via monkey-patching Model.save
        # is complex; instead we rely on the no-op patch above for the explicit call
        # and set _skip_patient_info_refresh on OMOP instances via signal suppression.
        self._setup_signal_suppression()

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
        all_persons = []

        with _suppress_patient_info_refresh() as collected_persons:
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

            all_persons = list({p.person_id: p for p in collected_persons}.values())

        # Single batch refresh — one call per unique person
        self.stdout.write(f"\nRefreshing PatientInfo for {len(all_persons)} persons ...")
        from omop_core.services.patient_info_service import refresh_patient_info
        from omop_core.services.lot_service import infer_lot_for_person
        for i, person in enumerate(all_persons, 1):
            refresh_patient_info(person)
            infer_lot_for_person(person)
            if i % 25 == 0:
                self.stdout.write(f"  {i}/{len(all_persons)} refreshed ...")

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Created: {total_created}, errors: {len(total_errors)}"
        ))
        if total_errors:
            self.stdout.write(self.style.WARNING("Errors (first 20):"))
            for e in total_errors[:20]:
                self.stdout.write(f"  {e}")

    def _setup_signal_suppression(self):
        """Monkey-patch Model.save to set _skip_patient_info_refresh on OMOP instances."""
        from omop_core.models import (
            ConditionOccurrence, DrugExposure, Measurement,
            Observation, ProcedureOccurrence,
        )
        for model in (ConditionOccurrence, DrugExposure, Measurement, Observation, ProcedureOccurrence):
            original_save = model.save

            def _patched_save(self, *args, _orig=original_save, **kwargs):
                self._skip_patient_info_refresh = True
                return _orig(self, *args, **kwargs)

            model.save = _patched_save

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
