#!/bin/sh
# Celery worker entrypoint for behavioros-worker Railway service.
#
# Runs Celery in the foreground alongside a tiny always-200 HTTP server
# that answers /api/health/ so Railway's fleet healthcheck (defined in
# railway.json for behavioros-web) doesn't kill this container. The
# healthcheck path is deliberately not made configurable — matches the
# web contract, no drift risk.
#
# Attempted the cleaner alternative first: setting `railwayConfigFile:
# railway.worker.json` on the service instance to override the fleet
# config's healthcheck. Turned out to silently deadlock Railway's build
# scheduler (deploys stuck at "scheduling build on Metal builder ...",
# no further activity, no error surfaced). Reverting to fleet config
# and answering the healthcheck locally sidesteps that entirely.

set -e

python -c "
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, *args, **kwargs):
        pass  # keep Celery's log stream clean
HTTPServer(('0.0.0.0', int(os.environ.get('PORT', 8000))), H).serve_forever()
" &
HC_PID=$!
trap 'kill $HC_PID 2>/dev/null; exit' TERM INT

exec celery -A config worker --loglevel=info --concurrency=1
