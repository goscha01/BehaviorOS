#!/bin/sh
# Celery beat entrypoint for behavioros-beat Railway service.
#
# Same sidecar pattern as start-worker.sh — a tiny always-200 HTTP server
# answers the fleet's /api/health/ healthcheck so Railway doesn't kill
# the container. See start-worker.sh for the full explanation of why we
# can't just override healthcheck via railwayConfigFile.
#
# Beat writes its schedule state to /tmp/celerybeat-schedule by default.
# Ephemeral is fine here — beat rebuilds the schedule from
# CELERY_BEAT_SCHEDULE on every startup, and a missed cron tick (rare;
# only happens during redeploys) is acceptable at the pilot cadence.
# Move to a persistent scheduler backend (django-celery-beat) if the
# schedule ever needs to survive per-tick.

set -e

python -c "
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, *args, **kwargs):
        pass  # keep Celery beat's log stream clean
HTTPServer(('0.0.0.0', int(os.environ.get('PORT', 8000))), H).serve_forever()
" &
HC_PID=$!
trap 'kill $HC_PID 2>/dev/null; exit' TERM INT

# --pidfile= disables pidfile entirely — the container is one-shot
# and multiple beat instances would create dupes (each would run the
# schedule). Ops MUST provision exactly one beat replica.
exec celery -A config beat --loglevel=info --pidfile=
