"""Build immutable migration payloads from locally downloaded publisher releases.

This script does not download or import patient data. The versioned output must
be committed with its data migration; never overwrite an already shipped release.
"""
import argparse
import gzip
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from omop_core.services.source_release_files import mesh_records, ncit_records


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_snapshot(output, vocabulary, version, name, records, upstream):
    filename = f'{vocabulary.lower()}-{version}.jsonl.gz'
    count = definitions = retired = 0
    seen = set()
    with (output / filename).open('wb') as raw:
        with gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as stream:
            for record in records:
                if record['code'] in seen:
                    raise ValueError(f'Duplicate {vocabulary} code {record["code"]}')
                seen.add(record['code'])
                stream.write((json.dumps(record, ensure_ascii=False, separators=(',', ':'), sort_keys=True) + '\n').encode())
                count += 1
                definitions += bool(record['definition'])
                retired += record['retired']
    if not count:
        raise ValueError('Refusing empty snapshot')
    return {
        'vocabulary_id': vocabulary, 'name': name, 'release_version': version,
        'filename': filename, 'sha256': sha256(output / filename),
        'term_count': count, 'definition_count': definitions, 'retired_count': retired,
        'source_url': upstream[0]['url'], 'upstream': upstream,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ncit', required=True)
    parser.add_argument('--mesh-descriptors', required=True)
    parser.add_argument('--mesh-supplementary', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    def source(path, url):
        return {'url': url, 'sha256': sha256(path)}
    manifest = {'format_version': 1, 'catalogs': [
        write_snapshot(output, 'NCIt', '26.08e', 'NCI Thesaurus', ncit_records(args.ncit), [
            source(args.ncit, 'https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/Thesaurus_26.08e.FLAT.zip'),
        ]),
        write_snapshot(output, 'MeSH', '2026-09-20', 'Medical Subject Headings',
                       mesh_records(args.mesh_descriptors, args.mesh_supplementary), [
            source(args.mesh_descriptors, 'https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh/desc2026.gz'),
            source(args.mesh_supplementary, 'https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh/supp2026.gz'),
        ]),
    ]}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
