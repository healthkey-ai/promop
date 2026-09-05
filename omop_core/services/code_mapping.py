"""Compatibility module; new code uses ``omop_core.mapping.code_resolution``."""

import sys

from omop_core.mapping import code_resolution as _implementation

# Preserve every historical export (including underscore-prefixed helpers that
# management commands and extension tests have used) and make monkey-patching
# the old path operate on the canonical module during the transition.
sys.modules[__name__] = _implementation
