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
# NOTE: `makemigrations --noinput` runs here to bootstrap the initial DB
# schema on the very first deploy — no app has migration files checked in
# yet. After the first successful boot, extract migrations from the
# container, commit them to the repo, and remove `makemigrations` from
# this line to avoid ORM/git drift on subsequent redeploys.
CMD ["sh", "-c", "python manage.py makemigrations --noinput && python manage.py migrate --noinput && python manage.py bootstrap_admin && gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 3 --access-logfile - --error-logfile -"]
