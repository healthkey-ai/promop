#!/bin/sh
set -e

VOCAB_DIR="/tmp/vocab"

echo "Downloading vocabulary files from gs://${VOCAB_BUCKET}/..."
mkdir -p "$VOCAB_DIR"
python -c "
from google.cloud import storage
import os

bucket_name = os.environ['VOCAB_BUCKET']
client = storage.Client()
bucket = client.bucket(bucket_name)

for blob in bucket.list_blobs():
    if blob.name.endswith('.csv'):
        dest = f'/tmp/vocab/{blob.name}'
        print(f'  {blob.name} ({blob.size // 1024}KB)')
        blob.download_to_filename(dest)

print('Download complete.')
"

echo "Loading vocabularies into database..."
python manage.py load_athena_vocabularies --path "$VOCAB_DIR" --replace

echo "Done."
