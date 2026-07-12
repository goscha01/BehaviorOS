from .base import *  # noqa: F401, F403

DEBUG = False

SIMPLE_JWT['AUTH_COOKIE_SECURE'] = True
SIMPLE_JWT['AUTH_COOKIE_SAMESITE'] = 'None'

# Railway (and most PaaS) terminate TLS at the edge and forward as HTTP
# with X-Forwarded-Proto=https. Without this, SECURE_SSL_REDIRECT sees
# `request.scheme == 'http'` and infinite-redirects.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True

SECURE_SSL_REDIRECT = True
# Health check runs container-to-container on plain HTTP, no X-Forwarded-Proto,
# so it would otherwise 301 and Railway would mark the deploy unhealthy.
SECURE_REDIRECT_EXEMPT = [r'^api/health/']

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
