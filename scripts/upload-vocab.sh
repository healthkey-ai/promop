#!/bin/sh
set -e

BUCKET="${1:-ctomop-staging-vocab}"
VOCAB_DIR="${2:-$HOME/Downloads/vocabulary_download_v5_*}"

echo "Uploading vocabulary files to gs://${BUCKET}/..."
echo "Source: ${VOCAB_DIR}"

for file in CONCEPT.csv CONCEPT_CLASS.csv CONCEPT_RELATIONSHIP.csv \
            CONCEPT_ANCESTOR.csv DOMAIN.csv RELATIONSHIP.csv VOCABULARY.csv; do
  if [ -f "${VOCAB_DIR}/${file}" ]; then
    echo "  ${file}..."
    gcloud storage cp "${VOCAB_DIR}/${file}" "gs://${BUCKET}/${file}"
  else
    echo "  SKIP ${file} (not found)"
  fi
done

echo "Upload complete."
echo ""
echo "To run the load job:"
echo "  gcloud run jobs execute ctomop-staging-load-vocab --region us-central1 --wait"
