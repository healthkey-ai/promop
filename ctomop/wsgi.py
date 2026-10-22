"""Compatibility alias for :mod:`promop.wsgi`."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module('promop.wsgi')
