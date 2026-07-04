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
from django.contrib import admin
from django.urls import path, include, re_path
from django.views.generic import TemplateView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from patient_portal.api import views

# Versioned API URL patterns (v1) — the canonical contract.
_v1 = [
    path('', include('patient_portal.api.urls')),
    path('lab-results/', include('patient_portal.api.lab_results.urls')),
    path('fhir/', include('patient_portal.api.fhir.urls')),
    path('health/', views.health_check, name='health_check_v1'),
    path('schema/', SpectacularAPIView.as_view(), name='schema'),
    path('docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
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

    # OAuth2 / SMART on FHIR authorization server endpoints
    path('o/', include('oauth2_provider.urls', namespace='oauth2_provider')),
    # SMART on FHIR discovery
    path('.well-known/smart-configuration', views.smart_configuration, name='smart_configuration'),
    # Serve React app for all other routes
    re_path(r'^.*$', TemplateView.as_view(template_name='index.html'), name='home'),
]
