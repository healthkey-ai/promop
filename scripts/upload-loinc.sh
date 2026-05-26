#!/bin/sh
set -e

BUCKET="${1:-hk-labs-staging-loinc}"
LOINC_ZIP="${2:-$HOME/Downloads/Loinc_2.82.zip}"
LOINC_CLASS_CSV="${3:-}"

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

echo "Extracting Loinc.csv from ${LOINC_ZIP}..."
unzip -j -o "$LOINC_ZIP" "*/LoincTable/Loinc.csv" -d "$TMPDIR"

if [ -n "$LOINC_CLASS_CSV" ]; then
  cp "$LOINC_CLASS_CSV" "$TMPDIR/LoincClass.csv"
else
  echo "No --loinc-class-csv provided, looking in hk-labs..."
  HK_LABS_CLASS="$(dirname "$0")/../../hk-labs/loinc-codes-aliases/LoincClass.csv"
  if [ -f "$HK_LABS_CLASS" ]; then
    cp "$HK_LABS_CLASS" "$TMPDIR/LoincClass.csv"
  else
    echo "ERROR: LoincClass.csv not found. Provide path as 3rd argument."
    exit 1
  fi
fi

echo ""
echo "Uploading to gs://${BUCKET}/..."

for file in LoincClass.csv Loinc.csv; do
  size=$(stat -f%z "${TMPDIR}/${file}" 2>/dev/null || stat --printf="%s" "${TMPDIR}/${file}" 2>/dev/null)
  size_mb=$((size / 1048576))
  printf "  %s (%dMB)..." "$file" "$size_mb"
  gcloud storage cp "${TMPDIR}/${file}" "gs://${BUCKET}/${file}" 2>&1 | grep -E "^(Copying|Average)" || true
  echo " done"
done

echo ""
echo "Upload complete."
echo ""
echo "To run the load job:"
echo "  gcloud run jobs execute ctomop-staging-load-loinc --region us-central1 --wait"
