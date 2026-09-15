"""Authenticated Render staging smoke on an explicitly provisioned synthetic record.

Provision a new synthetic Person/PatientRecord with custom_fields containing
{"genomics_release_smoke": RUN_ID}; no clinical record or list is replaced.
Credentials are read from environment variables and never written to the receipt.
"""
import argparse
from datetime import date, datetime, timezone
import json
import os
from uuid import uuid4

import requests

BASE_URL = 'https://promop-staging.onrender.com'


def run_smoke(session, base_url, person_id, run_id):
    root = f'{base_url}/api/v1/patient-records/{person_id}/'
    report = {'report_version': 1, 'captured_at': datetime.now(timezone.utc).isoformat(),
              'checks': [], 'passed': False, 'cleanup_passed': False}
    marker = 'genomics-release-smoke:' + str(uuid4())

    def request(method, path='', payload=None, expected=200):
        kwargs = {'timeout': 60}
        if payload is not None:
            kwargs['json'] = payload
        response = session.request(method, root + path, **kwargs)
        if response.status_code != expected:
            raise RuntimeError(f'{method} smoke request returned HTTP {response.status_code}, expected {expected}')
        return response.json() if expected != 204 else None

    record = request('GET')['patient_info']
    if (record.get('custom_fields') or {}).get('genomics_release_smoke') != run_id:
        raise RuntimeError('Record is not provisioned for this synthetic smoke run')
    if request('GET', 'genomics/'):
        raise RuntimeError('Synthetic smoke record must start with no findings')
    catalog = request('GET', 'genomics-catalog/?disease=CLL')
    if not any(m['key'] == 'tp53' and m['writable'] for m in catalog['markers']):
        raise RuntimeError('TP53 parent recipe is not writable')
    report['checks'].append('catalog')
    try:
        item = request('POST', 'genomics/', {'gene': 'TP53', 'variant': 'synthetic source call',
            'variant_description': marker, 'interpretation': 'Pathogenic', 'status': 'present',
            'test_date': date.today().isoformat(), 'allelic_frequency': 23.5,
            'allelic_frequency_unit': '%'}, expected=201)
        item_id = item['id']
        if item.get('provenance') != 'asserted':
            raise RuntimeError('Finding assertion provenance was lost')
        if request('GET')['patient_info']['tp53_disruption'] is not True:
            raise RuntimeError('Positive TP53 did not reach the patient projection')
        report['checks'].append('create_and_positive_readback')
        for status in ('absent', 'indeterminate', 'present'):
            item = request('PATCH', f'genomics/{item_id}/', {'status': status, 'laboratory': 'Synthetic release verification'})
            if item['status'] != status or item.get('laboratory') != 'Synthetic release verification':
                raise RuntimeError('Edited finding did not read back')
            if status == 'absent' and item.get('allelic_frequency') is not None:
                raise RuntimeError('Absence retained an incompatible VAF')
            record = request('GET')['patient_info']
            expected = True if status == 'present' else None
            if record['tp53_disruption'] is not expected:
                raise RuntimeError('Finding state did not reach the TP53 aggregate')
            if not any(v['id'] == item_id and v['status'] == status for v in record['genomics_tp53']):
                raise RuntimeError('Priority projection lost the edited finding')
            report['checks'].append(status + '_readback')
        provenance = request('GET', 'field-provenance/genomics_tp53/')
        if not provenance.get('source_rows'):
            raise RuntimeError('Finding has no readable source provenance')
        report['checks'].append('source_provenance')
        request('DELETE', f'genomics/{item_id}/', expected=204)
        if request('GET', 'genomics/') or request('GET')['patient_info']['tp53_disruption'] is not None:
            raise RuntimeError('Deletion did not clear the active finding and aggregate')
        report['checks'].append('delete_and_unknown_readback')
        report['passed'] = True
    finally:
        # Discover by a unique source marker as well as after normal success:
        # a timed-out POST may have committed before its response was received.
        for finding in request('GET', 'genomics/'):
            if finding.get('variant_description') == marker:
                request('DELETE', f"genomics/{finding['id']}/", expected=204)
        report['cleanup_passed'] = not request('GET', 'genomics/')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--person-id', type=int, required=True)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    session = requests.Session()
    login = session.post(BASE_URL + '/api/auth/login/', json={
        'username': os.environ['GENOMICS_SMOKE_USERNAME'],
        'password': os.environ['GENOMICS_SMOKE_PASSWORD']}, timeout=60)
    if login.status_code != 200:
        raise SystemExit('Smoke login failed (credentials withheld)')
    session.headers.update({'X-CSRFToken': session.cookies.get('csrftoken', ''), 'Referer': BASE_URL + '/'})
    try:
        print(json.dumps(run_smoke(session, BASE_URL, args.person_id, args.run_id), indent=2))
    finally:
        session.post(BASE_URL + '/api/auth/logout/', timeout=30)


if __name__ == '__main__':
    main()
