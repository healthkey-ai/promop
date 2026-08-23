"""Regression tests for built-frontend directory resolution.

Guards the staging outage where WHITENOISE_ROOT pointed at
``frontend/dist/remote`` (the federation-remote build) while the Django template
loader was hardcoded to ``frontend/build``. WhiteNoise served ``/index.html``
and ``/assets/*`` with a 200 while every SPA route 500'd with
``TemplateDoesNotExist: index.html``.
"""

from pathlib import Path

import pytest
from django.test import Client, override_settings

from ctomop.frontend_paths import resolve_frontend_root


class TestResolveFrontendRoot:
    def test_env_value_wins_over_existing_directories(self, tmp_path):
        """WHITENOISE_ROOT is authoritative — the Dockerfiles set it deliberately."""
        (tmp_path / 'frontend' / 'build').mkdir(parents=True)
        (tmp_path / 'frontend' / 'dist' / 'remote').mkdir(parents=True)

        resolved = resolve_frontend_root(tmp_path, '/app/frontend/dist/remote')

        assert resolved == Path('/app/frontend/dist/remote')

    def test_env_value_is_not_probed_for_existence(self, tmp_path):
        """Fail loudly on a bad WHITENOISE_ROOT rather than silently falling back."""
        (tmp_path / 'frontend' / 'build').mkdir(parents=True)

        resolved = resolve_frontend_root(tmp_path, '/nonexistent/path')

        assert resolved == Path('/nonexistent/path')

    def test_prefers_remote_build_when_both_exist(self, tmp_path):
        (tmp_path / 'frontend' / 'build').mkdir(parents=True)
        (tmp_path / 'frontend' / 'dist' / 'remote').mkdir(parents=True)

        assert resolve_frontend_root(tmp_path) == tmp_path / 'frontend' / 'dist' / 'remote'

    def test_falls_back_to_standalone_build(self, tmp_path):
        """The Render deploy runs `npm run build`, which only produces frontend/build."""
        (tmp_path / 'frontend' / 'build').mkdir(parents=True)

        assert resolve_frontend_root(tmp_path) == tmp_path / 'frontend' / 'build'

    def test_returns_none_when_frontend_is_not_built(self, tmp_path):
        """A backend-only checkout must still import settings without blowing up."""
        assert resolve_frontend_root(tmp_path) is None

    def test_ignores_a_file_sitting_at_a_candidate_path(self, tmp_path):
        """Only directories are valid document roots."""
        (tmp_path / 'frontend').mkdir()
        (tmp_path / 'frontend' / 'build').write_text('not a directory')

        assert resolve_frontend_root(tmp_path) is None


class TestTemplateAndStaticRootsAgree:
    def test_settings_serve_templates_from_the_whitenoise_root(self, settings):
        """The template dir and the static root must be the same directory.

        Holds in both environments: a built checkout (CI images, Render, Cloud
        Run) and a backend-only one where the frontend was never built.
        """
        if settings.FRONTEND_ROOT is None:
            assert settings.TEMPLATES[0]['DIRS'] == []
            assert not hasattr(settings, 'WHITENOISE_ROOT')
            return

        assert settings.TEMPLATES[0]['DIRS'] == [settings.FRONTEND_ROOT]
        assert settings.WHITENOISE_ROOT == settings.FRONTEND_ROOT

    @pytest.mark.parametrize('path', ['/', '/patients/42', '/settings'])
    def test_spa_catch_all_renders_index_html(self, tmp_path, path):
        """Deep links must return the SPA shell, not TemplateDoesNotExist."""
        (tmp_path / 'index.html').write_text('<!doctype html><div id="root"></div>')

        templates = [{
            'BACKEND': 'django.template.backends.django.DjangoTemplates',
            'DIRS': [tmp_path],
            'APP_DIRS': True,
            'OPTIONS': {'context_processors': []},
        }]

        with override_settings(TEMPLATES=templates):
            response = Client().get(path)

        assert response.status_code == 200
        assert b'<div id="root">' in response.content

    def test_spa_catch_all_500s_without_a_template_dir(self):
        """Pins the actual failure mode, so the fix cannot silently regress."""
        from django.template.exceptions import TemplateDoesNotExist

        templates = [{
            'BACKEND': 'django.template.backends.django.DjangoTemplates',
            'DIRS': [],
            'APP_DIRS': True,
            'OPTIONS': {'context_processors': []},
        }]

        with override_settings(TEMPLATES=templates):
            with pytest.raises(TemplateDoesNotExist):
                Client().get('/')
