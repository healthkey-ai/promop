"""Compatibility alias for :mod:`promop.settings`."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module('promop.settings')
