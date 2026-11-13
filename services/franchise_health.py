"""Small, independent HTTP surface for the dedicated Telegram service.

Status requests never touch SQLAlchemy or Telegram. Readiness remains separate
from HTTP liveness, including when the Telegram event loop stops making progress.
"""
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from string import Template
import threading
import time
from urllib.parse import urlsplit


class HealthServer:
    def __init__(self, port, *, host='0.0.0.0', bot_username='', heartbeat_timeout=60):
        self.ready = False
        self.stage = 'starting'
        self.bot_username = bot_username
        self.started_at = time.monotonic()
        self.heartbeat_at = None
        self.heartbeat_timeout = heartbeat_timeout
        self.server = None
        self.thread = None
        self.port, self.host = port, host
        root = Path(__file__).resolve().parents[1]
        self._template = Template((root / 'templates' / 'franchise_health.html').read_text())
        self._script = (root / 'static' / 'js' / 'franchise_health.js').read_bytes()

    def heartbeat(self):
        self.heartbeat_at = time.monotonic()

    def snapshot(self):
        now = time.monotonic()
        stale = self.ready and self.heartbeat_at is not None and now-self.heartbeat_at > self.heartbeat_timeout
        ready = bool(self.ready and not stale)
        status = 'ready' if ready else 'unresponsive' if stale else self.stage
        return dict(service='franchise-auction-bot', status=status, ready=ready,
                    uptime_seconds=max(0, int(now-self.started_at)))

    def _page(self, data):
        labels = {
            'starting': ('Starting the service', 'The HTTP server is online. The bot is starting.'),
            'database': ('Preparing the database', 'The HTTP server is online. Auction data is being prepared.'),
            'telegram': ('Connecting to Telegram', 'The database is ready. The bot is connecting to Telegram.'),
            'ready': ('Ready for your next pick', 'The auction and draft bot is ready in Telegram.'),
            'unresponsive': ('Bot response delayed', 'The server is online, but the bot heartbeat is overdue. Please retry shortly.'),
            'stopping': ('Service restarting', 'The bot is stopping. Please retry shortly.'),
        }
        title, description = labels.get(data['status'], labels['starting'])
        username = str(self.bot_username or '').lstrip('@')
        if re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
            action = ('<a class="primary" href="https://t.me/'+escape(username, quote=True)+'">Open bot in Telegram '
                      '<svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true">'
                      '<path d="M7 17 17 7M7 7h10v10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
                      '</svg></a>')
        else:
            action = '<p class="hint">Open the auction bot in Telegram and send <code>/auction</code>.</p>'
        return self._template.substitute(
            title=escape(title), description=escape(description),
            state='ready' if data['ready'] else 'starting', action=action,
            uptime=str(data['uptime_seconds']),
        ).encode('utf-8')

    def start(self):
        health = self

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 32

        class Handler(BaseHTTPRequestHandler):
            server_version = 'FranchiseHTTP'
            sys_version = ''
            timeout = 5

            def do_HEAD(self):
                self._respond(head=True)

            def do_GET(self):
                self._respond(head=False)

            def _respond(self, *, head):
                path = urlsplit(self.path).path.rstrip('/') or '/'
                data = health.snapshot()
                status, kind = 200, 'application/json; charset=utf-8'
                if path in ('/', '/status', '/franchise'):
                    body, kind = health._page(data), 'text/html; charset=utf-8'
                elif path in ('/healthz', '/health', '/readyz'):
                    status = 200 if data['ready'] else 503
                    body = json.dumps(data).encode()
                elif path == '/livez':
                    body = json.dumps(dict(service=data['service'], status='alive')).encode()
                elif path == '/franchise-health.js':
                    body, kind = health._script, 'text/javascript; charset=utf-8'
                elif path == '/favicon.ico':
                    status, body = 204, b''
                else:
                    status = 404
                    body = json.dumps(dict(error='not_found', message='Open / for service status or /healthz for bot readiness.')).encode()
                self.send_response(status)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
                if status == 503:
                    self.send_header('Retry-After', '5')
                self.end_headers()
                if not head:
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # Monitors can time out or disconnect during a restart.

            def log_message(self, _format, *_args):
                return

        self.server = Server((self.host, self.port), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.1),
                                       name='franchise-health', daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.ready = False
        self.stage = 'stopping'
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.server = self.thread = None
