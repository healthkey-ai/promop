"""Fail an application build if publisher payloads are absent or LFS pointers."""
import hashlib
import json
from pathlib import Path


def verify(root):
    manifests = sorted((root / 'omop_core' / 'data').glob('source_catalog_*/manifest.json'))
    if not manifests:
        raise ValueError('No publisher source catalog manifest found.')
    for path in manifests:
        for catalog in json.loads(path.read_text())['catalogs']:
            payload = path.parent / catalog['filename']
            with payload.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if digest != catalog['sha256']:
                raise ValueError(
                    f'{payload}: missing or corrupt publisher data. Fetch Git LFS objects '
                    'before building (git lfs pull); a pointer is not a vocabulary archive.'
                )


if __name__ == '__main__':
    verify(Path(__file__).resolve().parents[1])
    print('Publisher vocabulary snapshots verified.')
