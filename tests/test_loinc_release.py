"""Fetching LOINC from loinc.org, and knowing when our copy is stale.

The archive is built in-process for these tests. Nothing here reaches the real
API: it is licensed, the download endpoint is rate limited, and a suite that
pulls 92MB is a suite nobody runs.
"""
import io
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from omop_core.models import LoincClass, LoincCodeClass, LoincRelease
from omop_core.services import loinc_release as lr

pytestmark = pytest.mark.django_db


PART_CSV = '''"PartNumber","PartTypeName","PartName","PartDisplayName","Status"
"LP7786-9","CLASS","CHEM","Chemistry - non-challenge","ACTIVE"
"LP7796-8","CLASS","HEM/BC","Hematology and Cell counts","ACTIVE"
"LP7757-0","CLASS","RETIRED/X","A retired class","DEPRECATED"
"LP1234-5","COMPONENT","Albumin","Albumin","ACTIVE"
'''

LOINC_CSV = '''"LOINC_NUM","CLASS","EXAMPLE_UNITS","PROPERTY","SCALE_TYP"
"1751-7","CHEM","g/dL","MCnc","Qn"
"718-7","HEM/BC","g/dL","MCnc","Qn"
"26453-1","HEM/BC","10*6/uL","NCnc","Qn"
"9999-9","RETIRED/X","","MCnc","Qn"
'''


def archive(members=None):
    """A release archive, laid out as loinc.org lays one out."""
    members = members if members is not None else {
        lr.PART_FILE_MEMBER: PART_CSV,
        lr.LOINC_TABLE_MEMBER: LOINC_CSV,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    return [buffer.getvalue()]


def published(version='2.83'):
    return {
        'version': version,
        'download_url': f'{lr.API_BASE}/Loinc/Download?version={version}',
        'md5': '057ddf203164705d5a4c3604257060a4',
        'release_date': '2026-08-19',
        'loinc_count': 112405,
    }


# --- reading the archive ----------------------------------------------------

def test_both_tables_come_from_one_archive():
    """The reason an API-only source is possible: Loinc.csv and the class
    table ship together, the latter as CLASS entries in the Part file."""
    classes, codes = lr.load_from_archive(archive())

    assert classes == 3 and codes == 4
    assert LoincClass.objects.get(code='CHEM').display_name == 'Chemistry - non-challenge'
    row = LoincCodeClass.objects.get(loinc_num='1751-7')
    assert (row.loinc_class_id, row.property, row.scale_type, row.example_units) == (
        'CHEM', 'MCnc', 'Qn', 'g/dL')


def test_property_and_scale_type_are_populated():
    """The columns migration 0249 added and no load had ever filled: 0 of
    112,371 rows on staging, which is what made the staleness invisible."""
    lr.load_from_archive(archive())

    assert not LoincCodeClass.objects.filter(property='').exists()
    assert not LoincCodeClass.objects.filter(scale_type='').exists()


def test_classes_load_even_when_the_part_file_comes_last():
    """stream_unzip yields members in archive order, and a code whose class is
    unknown is dropped -- so the loader cannot assume Part.csv comes first."""
    reversed_order = {lr.LOINC_TABLE_MEMBER: LOINC_CSV, lr.PART_FILE_MEMBER: PART_CSV}
    classes, codes = lr.load_from_archive(archive(reversed_order))

    assert (classes, codes) == (3, 4)
    assert LoincCodeClass.objects.count() == 4


def test_a_retired_class_is_kept():
    """Existing codes still reference it; dropping the class drops them."""
    lr.load_from_archive(archive())

    assert LoincClass.objects.filter(code='RETIRED/X').exists()
    assert LoincCodeClass.objects.filter(loinc_num='9999-9').exists()


def test_only_class_parts_become_classes():
    lr.load_from_archive(archive())

    assert not LoincClass.objects.filter(code='Albumin').exists()
    assert LoincClass.objects.count() == 3


def test_a_code_whose_class_is_missing_is_reported_not_silently_dropped():
    """Silence is how the stale load went unnoticed for a release cycle."""
    orphan = LOINC_CSV + '"5555-5","NOSUCHCLASS","","MCnc","Qn"\n'
    said = []
    _, codes = lr.load_from_archive(
        archive({lr.PART_FILE_MEMBER: PART_CSV, lr.LOINC_TABLE_MEMBER: orphan}),
        stdout=mock.Mock(write=said.append))

    assert codes == 4
    assert not LoincCodeClass.objects.filter(loinc_num='5555-5').exists()
    assert any('NOSUCHCLASS' in line and 'WARNING' in line for line in said)


@pytest.mark.parametrize('missing', [lr.PART_FILE_MEMBER, lr.LOINC_TABLE_MEMBER])
def test_an_archive_missing_either_member_is_an_error(missing):
    members = {lr.PART_FILE_MEMBER: PART_CSV, lr.LOINC_TABLE_MEMBER: LOINC_CSV}
    del members[missing]
    with pytest.raises(lr.LoincReleaseUnavailable, match='has no'):
        lr.load_from_archive(archive(members))


def test_reloading_updates_rather_than_duplicating():
    lr.load_from_archive(archive())
    changed = LOINC_CSV.replace('"1751-7","CHEM","g/dL"', '"1751-7","CHEM","mg/dL"')
    lr.load_from_archive(archive({lr.PART_FILE_MEMBER: PART_CSV, lr.LOINC_TABLE_MEMBER: changed}))

    assert LoincCodeClass.objects.count() == 4
    assert LoincCodeClass.objects.get(loinc_num='1751-7').example_units == 'mg/dL'


# --- deciding whether to download -------------------------------------------

def test_nothing_loaded_means_out_of_date():
    with mock.patch.object(lr, 'current_release', return_value=published()):
        stale, _ = lr.is_out_of_date()
    assert stale is True


def test_a_matching_version_is_not_out_of_date():
    LoincRelease.objects.create(release_version='2.83', release_url='x')
    with mock.patch.object(lr, 'current_release', return_value=published()):
        stale, _ = lr.is_out_of_date()
    assert stale is False


def test_an_older_loaded_version_is_out_of_date():
    LoincRelease.objects.create(release_version='2.82', release_url='x')
    with mock.patch.object(lr, 'current_release', return_value=published('2.83')):
        stale, _ = lr.is_out_of_date()
    assert stale is True


def test_sync_records_the_release_it_loaded():
    with mock.patch.object(lr, 'current_release', return_value=published()), \
         mock.patch.object(lr, 'stream_archive', return_value=archive()):
        assert lr.sync_release() == '2.83'

    row = LoincRelease.objects.get()
    assert row.release_version == '2.83'
    assert row.archive_md5 == '057ddf203164705d5a4c3604257060a4'
    assert row.loinc_count == 112405
    assert str(row.release_date) == '2026-08-19'


def test_sync_does_not_download_when_already_current():
    LoincRelease.objects.create(release_version='2.83', release_url='x')
    with mock.patch.object(lr, 'current_release', return_value=published()), \
         mock.patch.object(lr, 'stream_archive') as download:
        assert lr.sync_release() is None
    # The whole point: the check is cheap, the 92MB archive is not.
    download.assert_not_called()


def test_force_downloads_even_when_current():
    LoincRelease.objects.create(release_version='2.83', release_url='x')
    with mock.patch.object(lr, 'current_release', return_value=published()), \
         mock.patch.object(lr, 'stream_archive', return_value=archive()) as download:
        assert lr.sync_release(force=True) == '2.83'
    download.assert_called_once()


# --- the boot-time check must never stop a deploy ---------------------------

def test_missing_credentials_do_not_raise(settings):
    settings.LOINC_USER = ''
    settings.LOINC_PASSWORD = ''
    assert lr.dispatch_release_sync() is False


def test_an_unreachable_api_does_not_raise():
    with mock.patch.object(lr, 'current_release',
                           side_effect=lr.LoincReleaseUnavailable('401')):
        assert lr.dispatch_release_sync() is False


def test_an_unreachable_api_leaves_a_loaded_release_alone():
    LoincRelease.objects.create(release_version='2.82', release_url='x')
    with mock.patch.object(lr, 'current_release',
                           side_effect=lr.LoincReleaseUnavailable('401')):
        lr.dispatch_release_sync()
    assert lr.loaded_version() == '2.82'


def test_a_stale_release_goes_to_celery_when_a_broker_is_configured(settings):
    settings.CELERY_BROKER_URL = 'redis://localhost:6379/0'
    with mock.patch.object(lr, 'current_release', return_value=published()), \
         mock.patch('omop_core.tasks.sync_loinc_release_task.delay') as delay:
        assert lr.dispatch_release_sync() is True
    # ~92MB and bulk work: a web boot queues it rather than doing it.
    delay.assert_called_once()


def test_without_a_broker_the_boot_path_refuses_to_download(settings):
    """The boot path must never fetch 92MB in the web process.

    prepare-deployment.sh runs inside start.sh on Render, and render.yaml
    leaves CELERY_BROKER_URL dashboard-managed (sync: false) on the production
    web service -- so a deployment that has not pasted the Redis URL in has no
    broker. Downloading inline there means the archive lands before gunicorn
    binds its port, and Render kills an instance that does not bind in time.
    """
    settings.CELERY_BROKER_URL = ''
    with mock.patch.object(lr, 'current_release', return_value=published()), \
         mock.patch.object(lr, 'stream_archive') as download:
        assert lr.dispatch_release_sync() is False
    download.assert_not_called()
    assert lr.loaded_version() is None


def test_an_explicit_caller_may_still_run_inline(settings):
    settings.CELERY_BROKER_URL = ''
    with mock.patch.object(lr, 'current_release', return_value=published()), \
         mock.patch.object(lr, 'stream_archive', return_value=archive()):
        assert lr.dispatch_release_sync(allow_inline=True) is True
    assert lr.loaded_version() == '2.83'


def test_an_unreachable_broker_does_not_raise(settings):
    """kombu raises OperationalError when Redis is down. That escaping here
    fails check_loinc_release, which fails prepare-deployment.sh under
    `set -euo pipefail`, which stops the web service booting."""
    settings.CELERY_BROKER_URL = 'redis://localhost:6379/0'
    with mock.patch.object(lr, 'current_release', return_value=published()), \
         mock.patch('omop_core.tasks.sync_loinc_release_task.delay',
                    side_effect=RuntimeError('broker unreachable')):
        assert lr.dispatch_release_sync() is False


def test_an_inline_load_failure_does_not_raise(settings):
    settings.CELERY_BROKER_URL = ''
    with mock.patch.object(lr, 'current_release', return_value=published()), \
         mock.patch.object(lr, 'stream_archive',
                           side_effect=lr.LoincReleaseUnavailable('401')):
        assert lr.dispatch_release_sync(allow_inline=True) is False


def test_an_unexpected_error_in_the_version_check_does_not_raise():
    with mock.patch.object(lr, 'current_release', side_effect=ValueError('bad json')):
        assert lr.dispatch_release_sync() is False


def test_the_version_row_and_the_tables_land_together():
    """Recording the version separately would let the tables update while the
    row failed, after which every check re-spends the 92MB download forever."""
    from omop_core.models import LoincRelease as LR

    with mock.patch.object(lr, 'current_release', return_value=published()), \
         mock.patch.object(lr, 'stream_archive', return_value=archive()), \
         mock.patch.object(LR.objects, 'update_or_create',
                           side_effect=RuntimeError('version write failed')):
        with pytest.raises(RuntimeError):
            lr.sync_release()

    assert LoincCodeClass.objects.count() == 0
    assert lr.loaded_version() is None


def test_a_worker_load_is_logged_even_with_no_stdout(caplog):
    """The Celery task passes no stdout, and that is the only automated path.
    A load leaving no record is the failure this whole change exists to stop."""
    import logging as _logging

    with caplog.at_level(_logging.INFO, logger='omop_core.services.loinc_release'):
        lr.load_from_archive(archive())

    logged = ' '.join(r.message for r in caplog.records)
    assert 'CLASS parts' in logged and 'codes from' in logged


def test_a_dropped_code_is_logged_as_a_warning(caplog):
    import logging as _logging

    orphan = LOINC_CSV + '"5555-5","NOSUCHCLASS","","MCnc","Qn"\n'
    with caplog.at_level(_logging.WARNING, logger='omop_core.services.loinc_release'):
        lr.load_from_archive(
            archive({lr.PART_FILE_MEMBER: PART_CSV, lr.LOINC_TABLE_MEMBER: orphan}))

    assert any('NOSUCHCLASS' in r.message for r in caplog.records
               if r.levelno >= _logging.WARNING)


def test_credentials_are_required_and_say_so(settings):
    settings.LOINC_USER = ''
    settings.LOINC_PASSWORD = ''
    with pytest.raises(lr.LoincCredentialsMissing, match='LOINC_USER'):
        lr.credentials()


# --- against a real release archive, when one is on this machine ------------

REAL_ARCHIVE = Path.home() / 'Downloads' / 'Loinc_2.83.zip'


@pytest.mark.skipif(not REAL_ARCHIVE.exists(),
                    reason=f'no local release archive at {REAL_ARCHIVE}')
def test_a_real_release_archive_loads_completely():
    """The check the fixtures cannot make: a genuine ~92MB release, read the
    way the loader will read it in production.

    Skipped when the archive is absent, which is every CI run -- LOINC is
    licensed, so it cannot be committed or downloaded in CI.
    """
    def chunks(path, size=4 * 1024 * 1024):
        with open(path, 'rb') as handle:
            while True:
                block = handle.read(size)
                if not block:
                    return
                yield block

    said = []
    classes, codes = lr.load_from_archive(
        chunks(REAL_ARCHIVE), stdout=mock.Mock(write=said.append))

    # 2.83: 476 CLASS parts, 112,405 codes, every CLASS in use accounted for.
    assert classes == 476
    assert codes > 112_000
    assert not any('WARNING' in line for line in said), said

    # The columns migration 0249 added and no load had ever filled.
    assert LoincCodeClass.objects.exclude(property='').count() == codes
    assert LoincCodeClass.objects.exclude(scale_type='').count() > 112_000

    assert LoincClass.objects.get(code='CHEM').display_name == 'Chemistry - non-challenge'
    albumin = LoincCodeClass.objects.get(loinc_num='1751-7')
    assert (albumin.loinc_class_id, albumin.property, albumin.scale_type) == (
        'CHEM', 'MCnc', 'Qn')


def test_a_second_loader_backs_off_rather_than_downloading_too():
    """prepare-deployment.sh runs on every instance boot, so three instances
    would otherwise mean three concurrent 92MB downloads and three concurrent
    upserts over the same 112k rows.

    The lock is session-level and re-entrant, so a rival is simulated rather
    than taken on this connection -- which would succeed.
    """
    with mock.patch.object(lr, '_try_lock', return_value=False), \
         mock.patch.object(lr, 'current_release', return_value=published()), \
         mock.patch.object(lr, 'stream_archive') as download:
        assert lr.sync_release() is None
    download.assert_not_called()


def test_the_lock_is_released_after_a_failed_load():
    with mock.patch.object(lr, 'current_release', return_value=published()), \
         mock.patch.object(lr, 'stream_archive',
                           side_effect=lr.LoincReleaseUnavailable('boom')):
        with pytest.raises(lr.LoincReleaseUnavailable):
            lr.sync_release()

    # A held lock would make every later attempt skip, permanently.
    with mock.patch.object(lr, 'current_release', return_value=published()), \
         mock.patch.object(lr, 'stream_archive', return_value=archive()):
        assert lr.sync_release() == '2.83'


def test_an_empty_unit_mapping_is_not_cached_forever():
    """The tables load after the web process is up, so a worker caching {} at
    boot would serve it until the next restart."""
    from omop_core.services import concept_unit_info

    concept_unit_info.get_loinc_example_units.cache_clear()
    assert concept_unit_info.get_loinc_example_units() == {}

    lr.load_from_archive(archive())
    # No cache_clear() here: this is the other process's view.
    assert concept_unit_info.get_loinc_example_units() != {}
