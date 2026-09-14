"""Enforce application suites only when the detected change scope needs them."""
import os
import sys


def application_ci_passes(*, changes, docs_only, backend_required, backend, frontend):
    detected = changes == 'success'
    if detected and docs_only == 'true':
        return True
    # Unknown scope or failed detection must retain full application coverage.
    backend_passes = backend == 'success' or (
        detected and backend_required == 'false' and backend == 'skipped'
    )
    return frontend == 'success' and backend_passes


if __name__ == '__main__':
    passed = application_ci_passes(
        changes=os.environ.get('CHANGES_RESULT', ''),
        docs_only=os.environ.get('DOCS_ONLY', ''),
        backend_required=os.environ.get('BACKEND_REQUIRED', ''),
        backend=os.environ.get('BACKEND_RESULT', ''),
        frontend=os.environ.get('FRONTEND_RESULT', ''),
    )
    print('Application CI passed.' if passed else 'Required application suites did not pass.')
    sys.exit(0 if passed else 1)
