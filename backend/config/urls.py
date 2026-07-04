from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

from apps.common.health import deep_health, health

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/health/', health, name='health'),
    path('api/health/deep/', deep_health, name='deep-health'),
    path('api/auth/', include('apps.accounts.urls')),
    path('api/billing/', include('apps.billing.urls')),
    path('api/training/', include('apps.training.urls')),
    path('api/learning/', include('apps.learning.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
