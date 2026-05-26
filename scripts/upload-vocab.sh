#!/bin/sh
set -e

BUCKET="${1:-ctomop-staging-vocab}"
VOCAB_DIR="${2:-$HOME/Downloads/vocabulary_download_v5_*}"

FILES="CONCEPT.csv CONCEPT_CLASS.csv CONCEPT_RELATIONSHIP.csv \
       CONCEPT_ANCESTOR.csv DOMAIN.csv RELATIONSHIP.csv VOCABULARY.csv"

human_size() {
  local bytes=$1
  if [ "$bytes" -ge 1073741824 ]; then
    printf "%.1fGB" "$(echo "$bytes / 1073741824" | bc -l)"
  elif [ "$bytes" -ge 1048576 ]; then
    printf "%.1fMB" "$(echo "$bytes / 1048576" | bc -l)"
  elif [ "$bytes" -ge 1024 ]; then
    printf "%.0fKB" "$(echo "$bytes / 1024" | bc -l)"
  else
    printf "%dB" "$bytes"
  fi
}

total_bytes=0
file_count=0
for file in $FILES; do
  if [ -f "${VOCAB_DIR}/${file}" ]; then
    size=$(stat -f%z "${VOCAB_DIR}/${file}" 2>/dev/null || stat --printf="%s" "${VOCAB_DIR}/${file}" 2>/dev/null)
    total_bytes=$((total_bytes + size))
    file_count=$((file_count + 1))
  fi
done

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  OMOP Vocabulary Upload → gs://${BUCKET}/                   "
echo "║  Source: ${VOCAB_DIR}"
echo "║  Files: ${file_count} found, total $(human_size $total_bytes)"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

uploaded_bytes=0
file_num=0
start_time=$(date +%s)

for file in $FILES; do
  src="${VOCAB_DIR}/${file}"
  if [ ! -f "$src" ]; then
    echo "  ⊘ SKIP ${file} (not found)"
    continue
  fi

  file_num=$((file_num + 1))
  file_size=$(stat -f%z "$src" 2>/dev/null || stat --printf="%s" "$src" 2>/dev/null)

  pct=0
  if [ "$total_bytes" -gt 0 ]; then
    pct=$((uploaded_bytes * 100 / total_bytes))
  fi

  printf "\n[%d/%d] %s (%s)\n" "$file_num" "$file_count" "$file" "$(human_size $file_size)"

  bar_width=40
  filled=$((pct * bar_width / 100))
  empty=$((bar_width - filled))
  bar=$(printf '%*s' "$filled" '' | tr ' ' '█')$(printf '%*s' "$empty" '' | tr ' ' '░')
  printf "  Overall: [%s] %3d%%\n" "$bar" "$pct"

  file_start=$(date +%s)
  gcloud storage cp "$src" "gs://${BUCKET}/${file}" 2>&1 | grep -E "^(Copying|Average)" || true
  file_end=$(date +%s)

  uploaded_bytes=$((uploaded_bytes + file_size))
  elapsed=$((file_end - file_start))
  if [ "$elapsed" -gt 0 ]; then
    speed=$((file_size / elapsed))
    printf "  ✓ Done in %ds @ %s/s\n" "$elapsed" "$(human_size $speed)"
  else
    printf "  ✓ Done (<1s)\n"
  fi
done

end_time=$(date +%s)
total_elapsed=$((end_time - start_time))

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
printf "║  Upload complete: %s in %dm %ds" "$(human_size $uploaded_bytes)" "$((total_elapsed / 60))" "$((total_elapsed % 60))"
echo ""
if [ "$total_elapsed" -gt 0 ]; then
  avg_speed=$((uploaded_bytes / total_elapsed))
  printf "║  Average speed: %s/s" "$(human_size $avg_speed)"
  echo ""
fi
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "To run the load job:"
echo "  gcloud run jobs execute ctomop-staging-load-vocab --region us-central1 --wait"
