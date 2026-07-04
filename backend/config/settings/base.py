import os
from pathlib import Path
from datetime import timedelta

import dj_database_url
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent.parent.parent / '.env')

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.environ.get('SECRET_KEY', 'insecure-dev-key-change-me')

DEBUG = False

ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Third-party
    'rest_framework',
    'corsheaders',
    'django_filters',
    # Local apps
    'apps.common',
    'apps.accounts',
    'apps.billing',
    'apps.training',
    'apps.learning',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'apps.accounts.middleware.OrgContextMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Database
DATABASES = {
    'default': dj_database_url.config(
        default='postgresql://postgres:password@localhost:5432/behavioros',
        conn_max_age=600,
    )
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

# Media files
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# CORS
FRONTEND_URL = os.environ.get('FRONTEND_URL', 'http://localhost:3000')
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get('CORS_ALLOWED_ORIGINS', FRONTEND_URL).split(',')
    if origin.strip()
]
CORS_ALLOW_CREDENTIALS = True

# CSRF trusted origins needed for admin + any cross-origin form POST once we're
# behind HTTPS on a different domain from the frontend. Defaults to the
# CORS list so a single env var can drive both.
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        'CSRF_TRUSTED_ORIGINS', ','.join(CORS_ALLOWED_ORIGINS)
    ).split(',')
    if origin.strip() and origin.strip().startswith(('http://', 'https://'))
]

# DRF
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_FILTER_BACKENDS': (
        'django_filters.rest_framework.DjangoFilterBackend',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '20/minute',
    },
}

# JWT
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=30),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': False,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'AUTH_COOKIE': 'access_token',
    'AUTH_COOKIE_REFRESH': 'refresh_token',
    'AUTH_COOKIE_HTTP_ONLY': True,
    'AUTH_COOKIE_SECURE': False,  # Override in prod
    'AUTH_COOKIE_SAMESITE': 'Lax',
}

# Celery
CELERY_BROKER_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'UTC'

# Nightly learning job — runs the full BehaviorOS pipeline for every org.
# Time chosen for after upstream systems have settled (LeadBridge /
# Callio / ServiceFlow finish end-of-day writes). Override in an env
# with LEARNING_NIGHTLY_HOUR if needed.
_learning_nightly_hour = int(os.environ.get('LEARNING_NIGHTLY_HOUR', '2'))
_learning_nightly_minute = int(os.environ.get('LEARNING_NIGHTLY_MINUTE', '0'))
from celery.schedules import crontab  # noqa: E402
CELERY_BEAT_SCHEDULE = {
    'behavioros-nightly-learning': {
        'task': 'apps.learning.tasks.run_nightly_learning_job_for_all_orgs',
        'schedule': crontab(hour=_learning_nightly_hour, minute=_learning_nightly_minute),
    },
}

# Stripe
STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
STRIPE_STARTER_PRICE_ID = os.environ.get('STRIPE_STARTER_PRICE_ID', '')
STRIPE_PRO_PRICE_ID = os.environ.get('STRIPE_PRO_PRICE_ID', '')

# ElevenLabs
ELEVENLABS_API_KEY = os.environ.get('ELEVENLABS_API_KEY', '')
ELEVENLABS_VOICE_ID = os.environ.get('ELEVENLABS_VOICE_ID', '21m00Tcm4TlvDq8ikWAM')

# OpenAI
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')

# BehaviorOS Learning Engine
# Model choices are configurable so we can swap providers without code changes.
# Analysis model runs once per piece of evidence (high volume, cheap model).
# Synthesis model runs once per cluster (low volume, high-judgment model).
# Embedding model is reserved for Phase 2 semantic clustering.
LEARNING_ANALYSIS_MODEL = os.environ.get(
    'LEARNING_ANALYSIS_MODEL', 'claude-haiku-4-5-20251001'
)
LEARNING_SYNTHESIS_MODEL = os.environ.get(
    'LEARNING_SYNTHESIS_MODEL', 'claude-opus-4-7'
)
LEARNING_EMBEDDING_MODEL = os.environ.get(
    'LEARNING_EMBEDDING_MODEL', 'voyage-3-lite'
)
# Cost ceiling per nightly job. When exceeded, the job persists progress and
# marks itself PARTIAL — remaining evidence gets picked up on the next run.
LEARNING_JOB_MAX_USD = float(os.environ.get('LEARNING_JOB_MAX_USD', '5.0'))
# Minimum supporting evidence before a cluster is promoted from watchlist to
# a pending suggestion visible in the dashboard.
LEARNING_MIN_SUPPORTING_EVIDENCE = int(
    os.environ.get('LEARNING_MIN_SUPPORTING_EVIDENCE', '3')
)
# Days a rejected-suggestion signature blocks the same cluster from re-surfacing.
LEARNING_REJECTION_TTL_DAYS = int(
    os.environ.get('LEARNING_REJECTION_TTL_DAYS', '90')
)
# Which source adapters to pull from on a scheduled run. Adapters not in this
# list are still registered and can be triggered manually via `run_ingestion
# --source <name>`, but the nightly job ignores them. Registered sources today:
# leadbridge, callio, serviceflow. Future: hirefunnel, proofpix, fixloop, google_reviews.
LEARNING_ENABLED_SOURCES = [
    s.strip() for s in os.environ.get(
        'LEARNING_ENABLED_SOURCES', 'leadbridge,callio,serviceflow'
    ).split(',') if s.strip()
]

# Per-source adapter endpoints. When URL or token is empty, the adapter falls
# back to bundled fixtures so Phase 1 can be developed + tested end-to-end
# before source systems expose their read-only evidence endpoints.
LEADBRIDGE_LEARNING_URL = os.environ.get('LEADBRIDGE_LEARNING_URL', '')
LEADBRIDGE_LEARNING_TOKEN = os.environ.get('LEADBRIDGE_LEARNING_TOKEN', '')
CALLIO_LEARNING_URL = os.environ.get('CALLIO_LEARNING_URL', '')
CALLIO_LEARNING_TOKEN = os.environ.get('CALLIO_LEARNING_TOKEN', '')
SERVICEFLOW_LEARNING_URL = os.environ.get('SERVICEFLOW_LEARNING_URL', '')
SERVICEFLOW_LEARNING_TOKEN = os.environ.get('SERVICEFLOW_LEARNING_TOKEN', '')

# LLM provider credentials. When ANTHROPIC_API_KEY is unset, the learning
# LLM client falls back to a StubProvider that returns canned structured
# JSON — lets analyzers be developed + tested without spending tokens.
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# Per-model pricing in USD per 1M tokens (input / output). BudgetTracker
# reads this to attribute cost per analysis. Override via env-driven JSON
# if pricing changes without a code deploy.
# Rates below are placeholders; verify against current Anthropic pricing.
LEARNING_MODEL_PRICING = {
    'claude-haiku-4-5-20251001': {'input_per_mtok': 1.00, 'output_per_mtok': 5.00},
    'claude-sonnet-4-6': {'input_per_mtok': 3.00, 'output_per_mtok': 15.00},
    'claude-opus-4-7': {'input_per_mtok': 15.00, 'output_per_mtok': 75.00},
}
