"""Narrow production WSGI adapter. Reuses the Feedback V1 domain protocol and store."""
import json
import re
import threading
import time
from http import HTTPStatus
from service import configured_service


class RequestLimit:
    """Global, content/identity-free fixed window; one production worker only."""
    def __init__(self, limit=60, clock=time.monotonic):
        self.limit, self.clock = limit, clock
        self.start, self.count = clock(), 0
        self.lock = threading.Lock()

    def allow(self):
        with self.lock:
            now = self.clock()
            if now - self.start >= 60: self.start, self.count = now, 0
            if self.count >= self.limit: return False
            self.count += 1
            return True


def application_for(service, limiter=None):
    limiter = limiter or RequestLimit()

    def application(environ, start_response):
        def respond(status, value):
            body = json.dumps(value).encode()
            start_response(f'{status} {HTTPStatus(status).phrase}',
                           [('Content-Type', 'application/json'), ('Cache-Control', 'no-store'),
                            ('Content-Length', str(len(body)))])
            return [body]

        try:
            path, method = environ.get('PATH_INFO', ''), environ.get('REQUEST_METHOD', '')
            if path == '/healthz' and method == 'GET' and not environ.get('QUERY_STRING'):
                # No payload, identity, key access or GitHub request in health checks.
                with service.connect() as db: db.execute('SELECT count(*) FROM attempts').fetchone()
                return respond(200, {'status': 'ready'})
            if not limiter.allow(): return respond(429, {'status': 'limited'})
            if environ.get('QUERY_STRING'): return respond(400, {'status': 'invalid_request'})
            if path == '/v1/feedback' and method == 'POST':
                if environ.get('CONTENT_TYPE') != 'application/json' or environ.get('HTTP_TRANSFER_ENCODING'):
                    return respond(400, {'status': 'invalid_request'})
                length = environ.get('CONTENT_LENGTH', '')
                if not re.fullmatch(r'[0-9]{1,8}', length): return respond(413, {'status': 'invalid_size'})
                size = int(length)
                if size <= 0 or size > 24000: return respond(413, {'status': 'invalid_size'})
                def unique(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result: raise ValueError('duplicate field')
                        result[key] = value
                    return result
                data = environ['wsgi.input'].read(size)
                if len(data) != size: return respond(400, {'status': 'invalid_request'})
                try: payload = json.loads(data, object_pairs_hook=unique)
                except (ValueError, UnicodeError): return respond(400, {'status': 'invalid_request'})
                return respond(*service.submit(payload))
            if path.startswith('/v1/feedback/') and method == 'GET':
                return respond(*service.outcome(path[len('/v1/feedback/'):]))
            return respond(404, {'status': 'not_found'})
        except Exception:
            # A store/read failure is conservative. Never log request or exception values.
            return respond(202, {'status': 'uncertain'})
    return application


def create_application():
    return application_for(configured_service())
