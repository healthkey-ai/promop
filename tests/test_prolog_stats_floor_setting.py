"""promop reads PROLOG_STATS_MIN_COUNT for PROlog (0.8.0, RUN-24; #2788)."""
import pytest
from django.core.exceptions import ImproperlyConfigured

from promop.settings import _stats_min_count


def test_the_floor_is_read_as_a_whole_number():
    assert [_stats_min_count(v) for v in ('0', '20', ' 5 ')] == [0, 20, 5]


@pytest.mark.parametrize('raw', ['-1', 'x', '', '2.5'])
def test_a_bad_floor_stops_startup(raw):
    with pytest.raises(ImproperlyConfigured, match='PROLOG_STATS_MIN_COUNT'):
        _stats_min_count(raw)


def test_prolog_reads_the_setting(settings):
    from prolog_surveys import conf

    settings.PROLOG_STATS_MIN_COUNT = 20
    assert conf.get('PROLOG_STATS_MIN_COUNT') == 20
