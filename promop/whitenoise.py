"""WhiteNoise, extended to serve the PROlog runner's build.

The runner is a second SPA, and its build lives outside this project's tree:
`PROLOG_RUNNER_DIST` names it at runtime, so it cannot be nested inside
`WHITENOISE_ROOT` when the image is built the way `frontend/dist/remote` is.

Serving it with `django.views.static.serve` instead — which is what this
replaces — means no `Cache-Control`, no `ETag`, no precompressed variants, and
a blocking file read on a gunicorn worker for every asset request, on files
that are content-hashed and could be cached for a year. Registering the
directory with the WhiteNoise instance the project already runs gives it the
same treatment as PRomop's own build.
"""

from django.conf import settings
from whitenoise.middleware import WhiteNoiseMiddleware

#: Where the runner's assets are published, matching the `--base` its build
#: is compiled with. PRomop's own build owns `/assets/`.
RUNNER_PREFIX = 'prolog-static/'


class PromopWhiteNoise(WhiteNoiseMiddleware):
    """WhiteNoise plus the runner's dist, when a deployment mounts one."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        dist = getattr(settings, 'RUNNER_DIST', None)
        if dist and dist.exists():
            self.add_files(str(dist), prefix=RUNNER_PREFIX)

    def immutable_file_test(self, path, url):
        # Vite content-hashes everything under assets/ (`name-CvVEhs4B.js`), so
        # those are safe to cache forever; index.html and anything beside it is
        # not. WhiteNoise's own test only recognises Django's manifest storage,
        # which the runner's build does not go through.
        if url.startswith(f'/{RUNNER_PREFIX}'):
            return url.startswith(f'/{RUNNER_PREFIX}assets/')
        return super().immutable_file_test(path, url)
