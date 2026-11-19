"""Behaviour held constant while CodeQL alerts were fixed: the linear-time
replacements for backtracking regexes, and the export command's file mode."""
import base64
import hashlib
import os
import stat
import time
from types import SimpleNamespace

from omop_core.management.commands.backfill_drug_concepts import _strip_trailing_parenthetical
from omop_core.services.wearable_mapping_export import xml_safe
from patient_portal.api.fhir.integrity import _META_HTTP_RFC3230, _asserted_digest


def test_strip_trailing_parenthetical():
    assert _strip_trailing_parenthetical('Lenalidomide (Revlimid)') == 'Lenalidomide'
    assert _strip_trailing_parenthetical('Lenalidomide (Revlimid)  ') == 'Lenalidomide'
    assert _strip_trailing_parenthetical('Lenalidomide') == 'Lenalidomide'
    assert _strip_trailing_parenthetical('a (b) c') == 'a (b) c'
    assert _strip_trailing_parenthetical('a ()') == 'a ()'
    assert _strip_trailing_parenthetical('a (b (c)') == 'a'


def test_strip_trailing_parenthetical_is_linear():
    started = time.monotonic()
    _strip_trailing_parenthetical('(' + ' ' * 200_000 + 'x')
    _strip_trailing_parenthetical(' ' * 200_000 + '(x')
    assert time.monotonic() - started < 2


def test_xml_safe_drops_only_illegal_characters():
    assert xml_safe('a\x00b\x0bc\x1fd￾e￿') == 'abcde'
    assert xml_safe('tab\tnewline\ncr\r ok') == 'tab\tnewline\ncr\r ok'
    assert xml_safe(None) == ''


def _request(value):
    return SimpleNamespace(META={_META_HTTP_RFC3230: value})


def test_rfc3230_digest_forms():
    data = b'payload'
    hexed = hashlib.sha256(data).hexdigest()
    b64 = base64.b64encode(hashlib.sha256(data).digest()).decode()
    assert _asserted_digest(_request(f'sha-256={b64}')) == hexed
    assert _asserted_digest(_request(f'SHA-256 = {hexed.upper()}')) == hexed
    assert _asserted_digest(_request(f'md5=zzz, sha-256={b64}')) == hexed
    assert _asserted_digest(_request('md5=zzz')) is None
    assert _asserted_digest(_request('sha-256=')) is None


def test_export_file_is_owner_only(tmp_path, db):
    from django.core.management import call_command
    from omop_core.models import Person
    person = Person.objects.create(person_id=991234)
    out = tmp_path / 'bundle.json'
    call_command('export_fhir_bundle', '--person-id', str(person.person_id), '--output', str(out))
    assert out.exists()
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
