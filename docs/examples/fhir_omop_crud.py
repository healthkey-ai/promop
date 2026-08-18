"""Small FHIR -> OMOP CRUD walkthrough.

This intentionally mirrors the *shape* of the production FHIR importer, not
its batching and vocabulary-resolution machinery.  Run against a disposable
organization and a service token.  Every clinical write goes to an OMOP
endpoint; PatientRecord is refreshed by the API's signal chain.

    PROMOP_URL=http://localhost:8000/api \
    PROMOP_TOKEN=... \
    python docs/examples/fhir_omop_crud.py

The concept ids below are examples: resolve them from the vocabulary endpoint
for the deployment under test rather than copying ids between environments.
"""

import os
from datetime import date

import requests


BASE = os.environ.get("PROMOP_URL", "http://localhost:8000/api").rstrip("/")
HEADERS = {
    "Authorization": f"Bearer {os.environ['PROMOP_TOKEN']}",
    "X-Provenance-Source": os.environ.get("PROMOP_PROVENANCE_SOURCE", "FHIR_IMPORT"),
    "X-Provenance-User-ID": os.environ.get("PROMOP_PROVENANCE_USER_ID", "fhir-crud-example"),
}
PERSON_ID = int(os.environ["PROMOP_PERSON_ID"])

# Resolve these from /concepts/lookup in a real client.  They are kept in one
# place to make the required OMOP vocabulary dependencies obvious.
CONCEPT = {
    "condition": int(os.environ["OMOP_CONDITION_CONCEPT_ID"]),
    "drug": int(os.environ["OMOP_DRUG_CONCEPT_ID"]),
    "measurement": int(os.environ["OMOP_MEASUREMENT_CONCEPT_ID"]),
    "type": int(os.environ["OMOP_LAB_TYPE_CONCEPT_ID"]),
    "observation": int(os.environ["OMOP_OBSERVATION_CONCEPT_ID"]),
    "procedure": int(os.environ["OMOP_PROCEDURE_CONCEPT_ID"]),
    "procedure_type": int(os.environ["OMOP_PROCEDURE_TYPE_CONCEPT_ID"]),
    "drug_type": int(os.environ["OMOP_DRUG_TYPE_CONCEPT_ID"]),
    "condition_type": int(os.environ["OMOP_CONDITION_TYPE_CONCEPT_ID"]),
}


def call(method, path, **kwargs):
    response = requests.request(method, f"{BASE}{path}", headers=HEADERS, **kwargs)
    response.raise_for_status()
    return response.json() if response.content else None


def crud(path, payload, patch):
    created = call("POST", path, json=payload)
    row_id = next(value for key, value in created.items() if key.endswith("_id"))
    call("GET", f"{path}{row_id}/")
    call("PATCH", f"{path}{row_id}/", json=patch)
    call("DELETE", f"{path}{row_id}/")


def main():
    event_date = date.today().isoformat()
    # A minimal FHIR bundle is parsed into complete OMOP facts.  The examples
    # show the same source values a bundle adapter should preserve.
    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {"resource": {"resourceType": "Patient", "id": str(PERSON_ID)}},
            {"resource": {"resourceType": "Observation", "code": {"coding": [{"system": "http://loinc.org", "code": "718-7"}]}, "valueQuantity": {"value": 13.2, "unit": "g/dL"}, "effectiveDateTime": event_date}},
            {"resource": {"resourceType": "Condition", "code": {"coding": [{"system": "http://snomed.info/sct", "code": "254837009"}]}, "onsetDateTime": event_date}},
            {"resource": {"resourceType": "Procedure", "code": {"coding": [{"system": "http://snomed.info/sct", "code": "387713003"}]}, "performedDateTime": event_date}},
        ],
    }
    print(f"Parsed {len(bundle['entry'])} FHIR resources for person {PERSON_ID}")

    crud("/conditions/", {
        "person": PERSON_ID, "condition_concept": CONCEPT["condition"],
        "condition_start_date": event_date, "condition_type_concept": CONCEPT["condition_type"],
        "condition_source_value": "254837009",
    }, {"condition_status_source_value": "active"})
    crud("/drug-exposures/", {
        "person": PERSON_ID, "drug_concept": CONCEPT["drug"],
        "drug_exposure_start_date": event_date, "drug_type_concept": CONCEPT["drug_type"],
        "drug_source_value": "FHIR-example-drug",
    }, {"stop_reason": "example"})
    crud("/measurements/", {
        "person": PERSON_ID, "measurement_concept": CONCEPT["measurement"],
        "measurement_date": event_date, "measurement_type_concept": CONCEPT["type"],
        "value_as_number": 13.2, "unit_source_value": "g/dL",
        "measurement_source_value": "718-7",
    }, {"value_as_number": 13.3})
    crud("/observations/", {
        "person": PERSON_ID, "observation_concept": CONCEPT["observation"],
        "observation_date": event_date, "observation_type_concept": CONCEPT["observation"],
        "value_as_string": "positive", "observation_source_value": "FHIR-example-assertion",
    }, {"value_as_string": "negative"})
    crud("/procedures/", {
        "person": PERSON_ID, "procedure_concept": CONCEPT["procedure"],
        "procedure_date": event_date, "procedure_type_concept": CONCEPT["procedure_type"],
        "procedure_source_value": "387713003",
    }, {"quantity": 1})

    print("CRUD complete: each OMOP write triggers PatientRecord rederivation.")


if __name__ == "__main__":
    main()
