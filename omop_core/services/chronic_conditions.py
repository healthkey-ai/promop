"""Chronic-condition overrides over AHRQ's indicator (#2566, #2512 step 5)."""
from __future__ import annotations

import re

from omop_core.models import ChronicConditionOverride

# A category (E03) or a code (E03.9, M85.80): letter, two digits, then up to four.
_ICD10CM = re.compile(r'[A-Z]\d{2}(?:\.[0-9A-Z]{1,4})?')


def normalize_icd10cm(raw: str | None) -> str | None:
    """An ICD-10-CM code in its dotted form (E039 -> E03.9, ' e03.9 ' -> E03.9),
    or None when the text is not shaped like one."""
    code = (raw or '').strip().strip("'").upper()
    if '.' not in code and len(code) > 3:
        code = f'{code[:3]}.{code[3:]}'
    return code if _ICD10CM.fullmatch(code) else None


def override_for(code: str | None) -> ChronicConditionOverride | None:
    """The override that covers a code: its own, else the nearest ancestor's."""
    code = normalize_icd10cm(code)
    if not code:
        return None
    flat = code.replace('.', '')
    # Every prefix that could be an override, longest first (M85.89, M85.8, M85).
    wanted = {}
    for n in range(len(flat), 2, -1):
        prefix = flat[:n]
        wanted[prefix[:3] + ('.' + prefix[3:] if len(prefix) > 3 else '')] = n
    rows = ChronicConditionOverride.objects.filter(code__in=wanted)
    return max(rows, key=lambda r: wanted[r.code], default=None)
