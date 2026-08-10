"""Resolution of the built-frontend directory.

Two different frontend builds land in two different directories:

| Build             | Command            | Output dir              | Image          |
|-------------------|--------------------|-------------------------|----------------|
| Standalone SPA    | `npm run build`    | `frontend/build`        | `Dockerfile`   |
| Federation remote | `npm run build:remote` | `frontend/dist/remote` | `Dockerfile.gcp` |

Both outputs contain the SPA shell (`index.html`) plus hashed assets, so both
are simultaneously a WhiteNoise document root *and* a Django template dir — the
catch-all route in ``ctomop.urls`` renders ``index.html`` as a template so that
client-side deep links (``/patients/42``) return the shell.

Those two consumers must resolve to the *same* directory. When they disagree,
the failure is confusing: WhiteNoise serves ``/index.html`` and ``/assets/*``
with a 200 while every SPA route 500s with ``TemplateDoesNotExist: index.html``.
"""

from pathlib import Path
from typing import Optional

# Checked in priority order when WHITENOISE_ROOT is unset.
_CANDIDATES = (
    ('frontend', 'dist', 'remote'),
    ('frontend', 'build'),
)


def resolve_frontend_root(base_dir: Path, whitenoise_root: str = '') -> Optional[Path]:
    """Return the directory holding the built frontend, or None if there is none.

    ``whitenoise_root`` is the raw ``WHITENOISE_ROOT`` environment value. When
    set it wins outright and is *not* probed for existence — the Dockerfiles set
    it to a path that exists in the final image, and failing loudly there beats
    silently falling back to a stale directory.
    """
    if whitenoise_root:
        return Path(whitenoise_root)

    for parts in _CANDIDATES:
        candidate = base_dir.joinpath(*parts)
        # is_dir(), not exists(): a stray file at one of these paths must not be
        # handed to the template loader or WhiteNoise as a document root.
        if candidate.is_dir():
            return candidate

    return None
