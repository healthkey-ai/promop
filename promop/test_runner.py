"""Django test runner that runs one shard of the suite (#2939).

``manage.py test --testrunner promop.test_runner.ShardedDiscoverRunner`` with
``TEST_SHARD_COUNT`` and ``TEST_SHARD_INDEX`` set runs a stable subset of the
tests: whole test classes, weighted by how many tests each has, split by
``promop.test_sharding``. With the variables unset it is Django's own runner.
(Django 5.2 has no ``--shard`` of its own.)

The partition is made per test label, so ``test omop_core patient_portal`` gives
each shard about a third of each app. That is as balanced as one global split
for these two apps, and it keeps ``load_tests_for_label`` the only thing
overridden.
"""

import unittest
from collections import Counter

from django.test.runner import DiscoverRunner
from django.test.utils import iter_test_cases

from promop.test_sharding import shard_of, shard_settings


def _class_key(test):
    cls = type(test)
    return f'{cls.__module__}.{cls.__qualname__}'


class ShardedDiscoverRunner(DiscoverRunner):
    def load_tests_for_label(self, label, discover_kwargs):
        tests = super().load_tests_for_label(label, discover_kwargs)
        settings = shard_settings()
        if settings is None:
            return tests
        index, count = settings
        cases = list(iter_test_cases(tests))
        weights = Counter(_class_key(t) for t in cases)
        mine = shard_of(weights, dict(weights), index, count)
        return unittest.TestSuite(t for t in cases if _class_key(t) in mine)
