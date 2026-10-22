"""Finding state without deriving clinical absence or aggregate results."""
STATUSES = frozenset({'present', 'absent', 'indeterminate'})
ASSESSMENT_STATUS = {
    'present': 'present', 'absent': 'absent', 'indeterminate': 'indeterminate',
    'not_tested': 'indeterminate', 'no_call': 'indeterminate',
}


def effective_status(finding):
    """An explicit state wins; legacy no-call evidence never means detected.

    Keep the original assessment in the projection. Unknown nonempty source
    states remain indeterminate. Only an entirely unstated legacy state keeps
    the historical present default; an empty finding list remains unknown.
    """
    if finding.get('status'):
        return finding['status'] if finding['status'] in STATUSES else 'indeterminate'
    assessment = finding.get('assessment')
    return ASSESSMENT_STATUS.get(assessment, 'indeterminate') if assessment else 'present'
