"""Compatibility alias for :mod:`promop.frontend_paths`."""
import sys
from importlib import import_module

sys.modules[__name__] = import_module('promop.frontend_paths')
