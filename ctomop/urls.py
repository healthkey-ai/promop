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
    1. Import the include() function: from django.conf import settings
from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
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

# The PROlog runner's own SPA, when a deployment mounts a build. Inserted before
# the catch-all above so the runner's routes reach it rather than PRomop's shell;
# its assets sit under their own prefix because PRomop's build owns /assets/.
if getattr(settings, 'RUNNER_DIST', None) and settings.RUNNER_DIST.exists():
    from django.views.static import serve as _serve_static
    from prolog_surveys.views import runner_index

    urlpatterns.insert(-1, re_path(
        r'^prolog-static/(?P<path>.*)$', _serve_static,
        {'document_root': settings.RUNNER_DIST},
    ))
    urlpatterns.insert(-1, re_path(r'^s/', runner_index, name='prolog_runner'))
