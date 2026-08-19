FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DJANGO_SETTINGS_MODULE=config.settings.prod

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python manage.py collectstatic --noinput 2>/dev/null || true

EXPOSE 8000

# Railway injects PORT; fall back to 8000 for local `docker run` use.
# migrate runs on every container start — safe because it's idempotent
# and Phase 1 runs single-instance so there's no race. If we ever scale
# horizontally, move this to a Railway pre-deploy command.
#
# Removed 2026-08-19: the boot-time `makemigrations --noinput` safety net.
# Prod's `django_migrations` table was verified via the bootstrap_admin
# diagnostic (commit 4eeae7e) to exactly match the checked-in file set
# (commit 01c8b07 baseline + conversations.0001_initial). Regenerating
# migrations at boot on every deploy was a bootstrap workaround, and
# leaving it in place risks silent drift between what git says vs what
# prod actually applied. `migrate` alone is now sufficient.
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py ensure_schema && python manage.py bootstrap_admin && (if [ \"$RUN_SEED\" = \"1\" ]; then python manage.py seed_learning || true; fi) && gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 3 --timeout ${GUNICORN_TIMEOUT:-300} --access-logfile - --error-logfile -"]
