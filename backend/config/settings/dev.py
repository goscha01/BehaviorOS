from .base import *  # noqa: F401, F403

DEBUG = True

SIMPLE_JWT['AUTH_COOKIE_SECURE'] = False

# Allow all origins in dev
CORS_ALLOW_ALL_ORIGINS = True
