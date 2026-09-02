"""
URL configuration for ctomop project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from pathlib import Path

from django.contrib import admin
from django.urls import path, include, re_path
from django.views.generic import TemplateView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.permissions import AllowAny
from patient_portal.api import views
from django.conf import settings

# Versioned API URL patterns (v1) — the canonical contract.
# INVARIANT: every sub-app URL module included here must also appear in the
# legacy block below (and vice versa) so both /api/v1/ and /api/ stay in sync.
_v1_api = [
    path('', include('patient_portal.api.v1_urls')),
    path('lab-results/', include('patient_portal.api.lab_results.v1_urls')),
    path('fhir/', include('patient_portal.api.fhir.v1_urls')),
    path('health/', views.health_check, name='health_check_v1'),
]
_v1_schema_patterns = [path('api/v1/', include(_v1_api))]
_v1 = [
    *_v1_api,
    path('schema/', SpectacularAPIView.as_view(
        patterns=_v1_schema_patterns,
        permission_classes=[AllowAny],
    ), name='schema'),
    path('docs/', SpectacularSwaggerView.as_view(
        url_name='schema',
        permission_classes=[AllowAny],
    ), name='swagger-ui'),
]

urlpatterns = [
    path('admin/', admin.site.urls),

    # Versioned API — stable contract, consumed by external services.
    path('api/v1/', include(_v1)),

    # Legacy unversioned aliases — deprecated, returns Deprecation/Sunset headers.
    # Remove after Sunset date (see DeprecationWarningMiddleware).
    path('api/', include('patient_portal.api.urls')),
    path('api/lab-results/', include('patient_portal.api.lab_results.urls')),
    path('api/fhir/', include('patient_portal.api.fhir.urls')),
    path('api/health/', views.health_check, name='health_check'),

    # PROlog survey runner. Its own URL tree (health + /run/), mounted under the
    # versioned prefix; /api/v1/prolog/run/... is what the runner front end calls.
    path('api/v1/prolog/', include('prolog_surveys.urls')),

    # OAuth2 / SMART on FHIR authorization server endpoints
    path('o/', include('oauth2_provider.urls', namespace='oauth2_provider')),
    # SMART on FHIR discovery
    path('.well-known/smart-configuration', views.smart_configuration, name='smart_configuration'),
    # Serve React app for all other routes
    re_path(r'^.*$', TemplateView.as_view(template_name='index.html'), name='home'),
]

def runner_urlpatterns(dist):
    """The PROlog runner's routes, for a deployment that mounts a build.

    A function rather than a conditional block so that it can be tested: the
    thing that matters is that `/s/...` is matched before the catch-all that
    returns PRomop's shell, and a module-level `if` evaluated once at import
    is not something a test can exercise.

    Only the page: the runner's assets are served by WhiteNoise, which is
    given the same directory in ctomop/whitenoise.py.
    """
    if not dist or not Path(dist).exists():
        return []
    from prolog_surveys.views import runner_index

    return [re_path(r'^s/', runner_index, name='prolog_runner')]


# Inserted before the catch-all above, so the runner's routes reach it rather
# than PRomop's shell.
for _pattern in runner_urlpatterns(getattr(settings, 'RUNNER_DIST', None)):
    urlpatterns.insert(-1, _pattern)
