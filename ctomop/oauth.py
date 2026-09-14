"""Compatibility alias for :mod:`promop.oauth`."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module('promop.oauth')
