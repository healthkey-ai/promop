# PHR record export

The member's own record as a file to keep, from the PHR API. The FLF Patient
Movement app promises it on the account screen ("download my record") and the
Terms commit to it; the Privacy Policy says exports are audited. Issue #2308.

```text
GET /api/v1/phr/record/export/?format=fhir     application/fhir+json
GET /api/v1/phr/record/export/?format=pdf      application/pdf
```

Code: `patient_portal/api/phr/export.py` (`RecordExportView`), routed from
`patient_portal/api/phr/urls.py`. Tests: `tests/test_phr_record_export.py`.

## Who, and whose record

The view is a `PhrView`: the caller is resolved through their own active
`PatientUser` link, so the member never names a person id, and an identity with
no record (a clinician, a service account) gets 404 without anything being
provisioned. Unauthenticated callers get 401/403. The `format` parameter is
required and is one of `fhir` or `pdf`; anything else is 400 with the list.

`?format=` is read by the view itself. DRF's default content negotiator treats
that same parameter as a URL format suffix and answers 404 for a value it has
no renderer for, so the view overrides `perform_content_negotiation` and renders
its errors as JSON regardless.

## FHIR

The same bundle `GET /api/v1/patient-records/{person_id}/export-fhir/` serves:
both go through `patient_portal/api/fhir/integrity.signed_fhir_response`,
which serializes the bundle to canonical bytes and sets `X-Content-SHA256`
and `X-Content-Signature` over exactly the body returned (see
[FHIR export](fhir-export-architecture.md)). The PHR export adds a
`Content-Disposition` filename, making it a download; the person-id export,
an integration surface, does not. Both answer `Cache-Control: no-store`. A
`fhirVersion` other than R4, in the query or the `Accept` header, is refused
with 406 as on the person-id export.

Both headers, and `Content-Disposition`, are now in `CORS_EXPOSE_HEADERS`
(`promop/settings.py`), which closes #1603: the member's app is cross-origin,
and a browser cannot read a response header that is not exposed, so the
integrity headers were unreadable by the one caller they were built for. The
fix applies to the person-id export too.

## PDF

A title page, then one page per section, in the order the app shows them:
about me, diagnoses, lines of therapy, check-ins (outcome surveys),
medications, procedures, lab results. Each section is built by the same
function its PHR endpoint uses (`about_fields`, `diagnoses`, `therapy`,
`outcomes`, `medications`, `procedures`, `labs`), so the PDF says what the
screen says, including each item's source ("entered by you" or "from your
medical record (facility)"). An empty section says "Nothing recorded." rather
than disappearing, so the member can see what the record does not hold.

`?lang=xx` picks the language the way `consent/` does (`language_of`:
query, then `Accept-Language`, then English). Only English text exists today
(`LABELS` in `export.py`); an unknown language falls back to it, and the choice
is recorded in the document's subject metadata. Adding a language is a new
entry in that table.

The PDF is built with `reportlab` (pure Python, no system libraries, unlike
weasyprint) using its built-in Helvetica, which covers Latin scripts only: a
name, drug or facility in another script (Cyrillic, Greek, CJK) renders as
boxes, though the export still succeeds. Embedding a Unicode font (Noto Sans
or DejaVu Sans under `patient_portal/api/phr/fonts/`) is the follow-up, and a
prerequisite for any non-English `lang`.

The whole PDF is built in the request: every section's rows, with no cap.
That is what the app's screens show across seven requests, so it is the cost
of seven reads plus the layout. A heavily loaded record (thousands of
results) may need a row cap per section or a queued build; neither exists yet.

`HEAD` is refused (405): Django would otherwise run the whole export to
discard the body, and the middleware does not audit HEAD.

## Audit

`AuditLogMiddleware` already writes one `record_view` row per API read. The
export view marks its request (`request.audit_detail`) and the middleware
stores that in `AuditEvent.detail`, so an export row reads
`{"export": "fhir"|"pdf", "person_id": N}` and the exports of a record can be
listed from the audit trail alone: filter `record_view` rows on
`detail->>'export'`. The marker is part of the row's HMAC signature. A refused
export (400, 404) is audited as a plain read, without the marker, because
nothing left the system.

Any view can use the same hook: set a small JSON object on the Django request
(`request._request` from a DRF view) and it lands in `detail`. It must be
JSON, under 2 KB serialized, and free of health data: the stdout line repeats
it. Anything else is dropped with an ops warning and the row is written
without it.
