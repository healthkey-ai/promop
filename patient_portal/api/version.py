"""Which build is running: ``GET /api/v1/version/`` (#2016).

Render injects ``RENDER_GIT_COMMIT`` into every deploy; a developer's checkout
has ``.git``. Either way the answer is a Git commit, validated as one so a
stray environment value is never reflected into the page. Public, because the
logo it sits under is on the login page too, and it names nothing but a
commit of a public repository.
"""
import os
import re
import subprocess
from functools import lru_cache

from django.conf import settings
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

_COMMIT = re.compile(r'[0-9a-fA-F]{7,64}')


def _valid(value):
    value = (value or '').strip()
    return value if _COMMIT.fullmatch(value) else ''


@lru_cache(maxsize=1)
def _checkout_commit():
    """The checkout's HEAD when this is a working tree, else ''."""
    try:
        completed = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=str(settings.BASE_DIR),
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ''
    return _valid(completed.stdout) if completed.returncode == 0 else ''


def deployed_commit():
    """The running build's commit: the deploy's, else the checkout's, else ''."""
    return (_valid(os.environ.get('RENDER_GIT_COMMIT'))
            or _valid(os.environ.get('GIT_COMMIT'))
            or _checkout_commit())


@api_view(['GET'])
@permission_classes([AllowAny])
def version_view(request):
    commit = deployed_commit()
    return Response({'commit': commit, 'short': commit[:8]})
