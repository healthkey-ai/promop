"""Wire-form normalisation for list-valued PatientRecord fields (#2574).

One rule for every write path: accept a JSON list or the legacy comma string,
trim, deduplicate in order, fold the retired spellings into the curated ones,
and refuse only a *new* answer that is neither configured nor already stored.
"""
import re

#: The list-valued direct fields whose wire form this module owns. A field
#: added here must also get its descriptor entry (`write_descriptor`); the
#: option source stays the caller's (curated descriptor options or FieldChoice).
FIELDS = frozenset({'protein_expressions'})

#: Spellings older clients and the seeded ProteinExpression vocabulary still
#: serve, folded into the curated FieldChoice displays.
_ALIASES = {
    'Kappa (κ) light chain': 'Kappa light chain',
    'Lambda (λ) light chain': 'Lambda light chain',
    'Kappa (k) light chain': 'Kappa light chain',
    'Lambda (l) light chain': 'Lambda light chain',
}

#: ``CD5: positive`` / ``CD5: negative`` from the pre-v32 extractor.
_POLARITY = {
    'positive': '+ve',
    'pos': '+ve',
    'present': '+ve',
    'negative': '-ve',
    'neg': '-ve',
    'absent': '-ve',
}
_IMPORTED_FORM = re.compile(r'^(?P<label>[^:]+):\s*(?P<answer>.+)$')


def _answer(part):
    """The curated spelling of one answer, or None when it is empty."""
    text = str(part).strip()
    if not text:
        return None
    for old, new in _ALIASES.items():
        if text.startswith(old):
            text = new + text[len(old):]
            break
    match = _IMPORTED_FORM.match(text)
    if match:
        label = match.group('label').strip()
        token = match.group('answer').strip().casefold()
        if token in _POLARITY:
            text = f'{label} {_POLARITY[token]}'
    return text


def normalise(value, *, options=(), stored=''):
    """The comma-joined stored form of *value*, or None for a clear.

    ``options`` is the configured answer set; empty means the caller has no
    vocabulary to validate against, so the answers are accepted as supplied.
    """
    if value is None:
        return None
    parts = value if isinstance(value, (list, tuple)) else str(value).split(',')
    answers = []
    for part in parts:
        text = _answer(part)
        if text and text not in answers:
            answers.append(text)
    if not answers:
        return None
    known = {str(option) for option in (options or ()) if str(option)}
    stored_set = {part.strip() for part in str(stored or '').split(',') if part.strip()}
    if known:
        unknown = [answer for answer in answers if answer not in known and answer not in stored_set]
        if unknown:
            raise ValueError(unknown[0])
    return ', '.join(answers)


def option_values(entry_options):
    """The bare values of a descriptor's option list (dicts or strings)."""
    return [str(option.get('value') if isinstance(option, dict) else option)
            for option in (entry_options or ())]
