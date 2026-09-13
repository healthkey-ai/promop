import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection

from omop_core.management.commands.search_field_value_candidates import Command

pytestmark = pytest.mark.django_db(transaction=True)


def inventory(tmp_path):
    path = tmp_path / 'inventory.json'
    path.write_text(json.dumps({'field_choices': [{'display': 'Alpha'}, {'display': 'Beta'}],
                                'reference_catalogs': {}}))
    return path


def test_search_checkpoints_bounded_batches_and_resumes(tmp_path, monkeypatch):
    source, output = inventory(tmp_path), tmp_path / 'results.json'
    searched = []

    def search(label, standards):
        with connection.cursor() as cursor:
            cursor.execute('SHOW transaction_read_only')
            assert cursor.fetchone()[0] == 'on'
            cursor.execute('SHOW statement_timeout')
            assert cursor.fetchone()[0] == '123ms'
        searched.append(label)
        return {'label': label, 'candidates': [], 'disposition': 'needs_review'}

    monkeypatch.setattr(Command, 'search_label', staticmethod(search))
    kwargs = dict(inventory=str(source), output=str(output), limit=1,
                  statement_timeout_ms=123, stdout=StringIO())
    call_command('search_field_value_candidates', **kwargs)
    assert not json.loads(output.read_text())['complete']
    call_command('search_field_value_candidates', resume=True, **kwargs)
    assert json.loads(output.read_text())['complete']
    call_command('search_field_value_candidates', resume=True, **kwargs)
    assert searched == ['Alpha', 'Beta']
    source.write_text(source.read_text() + ' ')
    with pytest.raises(CommandError, match='inventory differs'):
        call_command('search_field_value_candidates', resume=True, **kwargs)


def test_search_timeout_does_not_lose_prior_results_and_is_retryable(tmp_path, monkeypatch):
    source, output = inventory(tmp_path), tmp_path / 'results.json'

    class Timeout(Exception):
        sqlstate = '57014'

    def search(label, standards):
        if label == 'Beta':
            raise OperationalError('Do not expose database details') from Timeout()
        return {'label': label, 'candidates': [], 'disposition': 'needs_review'}

    monkeypatch.setattr(Command, 'search_label', staticmethod(search))
    kwargs = dict(inventory=str(source), output=str(output), stdout=StringIO())
    call_command('search_field_value_candidates', **kwargs)
    result = json.loads(output.read_text())
    assert result['completed_labels'] == 1
    assert result['labels'][1]['search_error'] == 'statement_timeout'
    assert 'database details' not in output.read_text()
    monkeypatch.setattr(Command, 'search_label', staticmethod(
        lambda label, standards: {'label': label, 'candidates': [], 'disposition': 'needs_review'}))
    call_command('search_field_value_candidates', resume=True, **kwargs)
    assert json.loads(output.read_text())['complete']
