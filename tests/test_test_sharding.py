"""The CI shard split (#2939): every item in exactly one shard, stable, balanced."""

import json
import unittest

import pytest

from promop.test_sharding import load_weights, partition, shard_of, shard_settings


def test_every_name_lands_in_exactly_one_shard():
    names = [f'tests/test_{i}.py' for i in range(57)]
    weights = {n: float(i % 7 + 1) for i, n in enumerate(names)}
    shards = partition(names, weights, 4)
    assert sorted(set().union(*shards)) == sorted(names)
    assert sum(len(s) for s in shards) == len(names)


def test_the_split_does_not_depend_on_input_order():
    names = [f'tests/test_{i}.py' for i in range(40)]
    weights = {n: float(len(n) + i) for i, n in enumerate(names)}
    assert partition(names, weights, 3) == partition(reversed(names), weights, 3)


def test_heavy_items_are_spread_and_loads_are_close():
    weights = {'a': 100.0, 'b': 94.0, 'c': 36.0, 'd': 24.0}
    weights.update({f'small{i}': 5.0 for i in range(60)})
    shards = partition(weights, weights, 4)
    loads = [sum(weights[n] for n in s) for s in shards]
    assert max(loads) - min(loads) < 10
    assert len({next(i for i, s in enumerate(shards) if n in s) for n in 'abcd'}) == 4


def test_an_unweighted_name_gets_the_average_weight_and_is_spread():
    weights = {'known1': 10.0, 'known2': 10.0}
    new = [f'new{i}' for i in range(8)]
    shards = partition([*weights, *new], weights, 2)
    assert abs(len(shards[0]) - len(shards[1])) <= 1


def test_shard_of_is_one_based():
    weights = {'a': 1.0, 'b': 1.0}
    assert shard_of(['a', 'b'], weights, 1, 2) | shard_of(['a', 'b'], weights, 2, 2) == {'a', 'b'}


def test_no_sharding_unless_asked():
    assert shard_settings({}) is None
    assert shard_settings({'TEST_SHARD_COUNT': '1'}) is None
    assert shard_settings({'TEST_SHARD_COUNT': '4', 'TEST_SHARD_INDEX': '2'}) == (2, 4)


@pytest.mark.parametrize('index', ['0', '5', ''])
def test_an_index_outside_the_count_is_an_error(index):
    with pytest.raises(ValueError):
        shard_settings({'TEST_SHARD_COUNT': '4', 'TEST_SHARD_INDEX': index})


def test_a_missing_or_broken_weights_file_is_no_weights(tmp_path):
    assert load_weights(tmp_path / 'absent.json') == {}
    broken = tmp_path / 'broken.json'
    broken.write_text('{not json')
    assert load_weights(broken) == {}
    good = tmp_path / 'good.json'
    good.write_text(json.dumps({'a': 1.5}))
    assert load_weights(good) == {'a': 1.5}


def _ids(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from _ids(t)
        else:
            yield t.id()


def test_the_django_runner_runs_each_class_in_exactly_one_shard(monkeypatch):
    from django.test.runner import DiscoverRunner

    from promop.test_runner import ShardedDiscoverRunner

    def make(name, n):
        body = {f'test_{i}': (lambda self: None) for i in range(n)}
        return type(name, (unittest.TestCase,), body)

    classes = [make('A', 5), make('B', 3), make('C', 3), make('D', 1)]
    suite = unittest.TestSuite(
        unittest.defaultTestLoader.loadTestsFromTestCase(c) for c in classes
    )
    monkeypatch.setattr(DiscoverRunner, 'load_tests_for_label', lambda self, label, kw: suite)

    per_shard = []
    for index in (1, 2):
        monkeypatch.setenv('TEST_SHARD_COUNT', '2')
        monkeypatch.setenv('TEST_SHARD_INDEX', str(index))
        runner = ShardedDiscoverRunner.__new__(ShardedDiscoverRunner)
        per_shard.append(list(_ids(runner.load_tests_for_label('x', {}))))

    assert sorted(per_shard[0] + per_shard[1]) == sorted(_ids(suite))
    classes_of = [{i.rsplit('.', 1)[0] for i in ids} for ids in per_shard]
    assert not classes_of[0] & classes_of[1]
    assert abs(len(per_shard[0]) - len(per_shard[1])) <= 2
