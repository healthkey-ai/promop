#!/bin/sh
set -e

echo "Loading LOINC classes from gs://${VOCAB_BUCKET}/..."
python manage.py load_loinc_classes --bucket "$VOCAB_BUCKET" --replace

echo "Done."
