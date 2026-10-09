#!/usr/bin/env bash
# Validate only: this script never opens a database connection or invokes ARTEMIS.
set -euo pipefail

required=(ARTEMIS_DBMS ARTEMIS_DB_SERVER ARTEMIS_CDM_SCHEMA ARTEMIS_WRITE_SCHEMA ARTEMIS_COHORT_JSON)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required environment variable: ${name}" >&2
    exit 2
  fi
done

if [[ "${ARTEMIS_DBMS}" != "postgresql" ]]; then
  echo "ARTEMIS_DBMS must be postgresql for PRomop." >&2
  exit 2
fi
if [[ "${ARTEMIS_CDM_SCHEMA}" == "${ARTEMIS_WRITE_SCHEMA}" ]]; then
  echo "ARTEMIS_CDM_SCHEMA and ARTEMIS_WRITE_SCHEMA must be different." >&2
  exit 2
fi
if [[ ! "${ARTEMIS_CDM_SCHEMA}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || [[ ! "${ARTEMIS_WRITE_SCHEMA}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
  echo "Schema names must be simple PostgreSQL identifiers; do not pass SQL." >&2
  exit 2
fi
if [[ ! -r "${ARTEMIS_COHORT_JSON}" ]]; then
  echo "ARTEMIS_COHORT_JSON is not a readable file: ${ARTEMIS_COHORT_JSON}" >&2
  exit 2
fi
if ! grep -q '"Expression"\|"expression"\|"ConceptSets"\|"conceptSets"' "${ARTEMIS_COHORT_JSON}"; then
  echo "ARTEMIS_COHORT_JSON does not appear to be an OHDSI cohort-definition JSON file." >&2
  exit 2
fi

echo "Configuration is structurally valid. No database connection was made."
