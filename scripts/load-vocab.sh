#!/bin/sh
set -e

echo "Loading vocabularies from gs://${VOCAB_BUCKET}/ (streaming)..."
python manage.py load_athena_vocabularies --bucket "$VOCAB_BUCKET" --replace

echo "Done."
