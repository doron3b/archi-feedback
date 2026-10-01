"""Undeployed Feedback V1 service. No request-body/access logging and no GitHub retries."""
import contextlib
import datetime
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import threading
import time
import uuid
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KINDS = {'bug', 'blocker', 'improvement'}
PATTERNS = [r'(?i)(https?://|www\.|[\w.+-]+@[\w.-]+\.[a-z]{2,}|/[a-z0-9_.-]+/[a-z0-9_.-]+|~/|[a-z]:\\)',
            r'(?i)(-----BEGIN|gh[pousr]_|github_pat_|sk-[a-z0-9]|xox[baprs]-|AKIA[0-9A-Z]|bearer\s|(?:token|secret|password|api[_ -]?key)\s*[:=])',
            r'(?i)(ARCHI-(?:S|CR)-|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\b[0-9a-f]{8,64}\b|```|\{\s*"|\b(?:sessionId|storyId|repoKey|logicalSessionId)\b)']


def validate(payload):
    if not isinstance(payload, dict) or set(payload) != {'schema_version', 'submission_id', 'title', 'body', 'kind'}:
        raise ValueError('invalid schema')
    if type(payload['schema_version']) is not int or payload['schema_version'] != 1 or payload['kind'] not in KINDS:
        raise ValueError('invalid schema')
    if not isinstance(payload['submission_id'], str) or str(uuid.UUID(payload['submission_id'])) != payload['submission_id'].lower():
        raise ValueError('invalid identifier')
    if uuid.UUID(payload['submission_id']).version != 4:
        raise ValueError('invalid identifier')
    for key, limit in [('title', 200), ('body', 16000)]:
        value = payload[key]
        if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > limit:
            raise ValueError('invalid text')
        if any((ord(c) < 32 and c not in '\t\n\r') or 127 <= ord(c) <= 159 for c in value) or any(re.search(p, value) for p in PATTERNS):
            raise ValueError('private content')


class Publisher:
    """Fixed target and GitHub App installation token; credentials are server-only."""
    def __init__(self, owner, repository, app_id, installation_id, repository_id, key_file):
        for value in (owner, repository):
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', value):
                raise ValueError('invalid destination')
        self.owner, self.repository = owner, repository
        self.app_id, self.installation_id, self.repository_id, self.key_file = app_id, int(installation_id), int(repository_id), key_file

    def request(self, path, body, token):
        request = urllib.request.Request('https://api.github.com' + path,
            data=None if body is None else json.dumps(body).encode(), method='GET' if body is None else 'POST',
            headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                     'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2026-03-10', 'User-Agent': 'Archi-Feedback'})
        # Do not follow redirects with credentials or dump HTTP error bodies.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
            data = response.read(65537)
            if len(data) > 65536: raise ValueError('oversize upstream response')
            return json.loads(data)

    def installation_token(self):
        import jwt  # PyJWT + cryptography, server environment only.
        with open(self.key_file, 'rb') as key:
            now = int(time.time())
            assertion = jwt.encode({'iat': now - 60, 'exp': now + 540, 'iss': self.app_id}, key.read(), algorithm='RS256')
        return self.request(f'/app/installations/{self.installation_id}/access_tokens',
                            {'repository_ids': [self.repository_id], 'permissions': {'issues': 'write'}}, assertion)['token']

    def create(self, payload):
        token = self.installation_token()
        issue = self.request(f'/repos/{self.owner}/{self.repository}/issues',
                             {'title': payload['title'], 'body': payload['body'], 'labels': [payload['kind']]}, token)
        number = issue['number']
        expected = f'https://github.com/{self.owner}/{self.repository}/issues/{number}'
        if type(number) is not int or number <= 0 or issue['html_url'] != expected:
            raise ValueError('invalid upstream receipt')
        return {'issue_number': number, 'issue_url': expected}

    def verified_receipt(self, payload, number, created):
        """Read one operator-identified Issue. Never search, mutate, or publish."""
        if type(number) is not int or number <= 0: raise ValueError('invalid receipt')
        issue = self.request(f'/repos/{self.owner}/{self.repository}/issues/{number}', None, self.installation_token())
        expected = f'https://github.com/{self.owner}/{self.repository}/issues/{number}'
        published = datetime.datetime.fromisoformat(issue['created_at'].replace('Z', '+00:00')).timestamp()
        if (issue.get('pull_request') is not None or issue['number'] != number or issue['html_url'] != expected
                or issue['title'] != payload['title'] or issue['body'] != payload['body']
                or [label['name'] for label in issue['labels']] != [payload['kind']]
                or issue['performed_via_github_app']['id'] != int(self.app_id)
                or issue['user']['type'] != 'Bot'
                or not int(created) <= published <= created + 120):
            raise ValueError('receipt mismatch')
        return {'issue_number': number, 'issue_url': expected}


class Service:
    def __init__(self, database, publisher, quota=100, hourly_limit=20):
        self.database, self.publisher = database, publisher
        self.quota, self.hourly_limit = quota, hourly_limit
        self.enabled = True
        self.pause_file = None
        self.lock = threading.Lock()
        self.initializing = True
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, receipt TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS recoveries (id TEXT PRIMARY KEY REFERENCES attempts(id), issue_number INTEGER UNIQUE NOT NULL, verified REAL NOT NULL)')
        self.initializing = False

    def recover(self, payload, number):
        """Operator-only positive recovery; caller must confirm association independently.

        No HTTP route exposes this method. Stop the publication process before invoking it.
        Matching content is a validation check, not proof of causal association.
        """
        validate(payload)
        if type(number) is not int or number <= 0: raise ValueError('invalid receipt')
        identifier = payload['submission_id'].lower()
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT digest,state,created,receipt FROM attempts WHERE id=?', (identifier,)).fetchone()
            if not row or row[0] != digest: raise ValueError('attempt mismatch')
            if row[1] == 'sent':
                receipt = json.loads(row[3])
                if receipt['issue_number'] != number: raise ValueError('receipt conflict')
                return receipt
            if row[1] != 'uncertain' or self.publisher is None: raise ValueError('invalid attempt')
            receipt = self.publisher.verified_receipt(payload, number, row[2])
            for (saved,) in db.execute('SELECT receipt FROM attempts WHERE receipt IS NOT NULL'):
                if json.loads(saved)['issue_number'] == number: raise ValueError('receipt already assigned')
            db.execute('INSERT INTO recoveries VALUES (?,?,?)', (identifier, number, time.time()))
            db.execute('UPDATE attempts SET state=?,receipt=? WHERE id=?', ('sent', json.dumps(receipt), identifier))
            return receipt

    @contextlib.contextmanager
    def connect(self):
        if self.initializing:
            db = sqlite3.connect(self.database, timeout=5)
        else:
            # Losing the disk during runtime must not create a replacement empty database.
            db = sqlite3.connect(pathlib.Path(self.database).resolve().as_uri() + '?mode=rw', uri=True, timeout=5)
        try:
            with db: yield db
        finally: db.close()

    def submit(self, payload):
        try: validate(payload)
        except (ValueError, TypeError, KeyError, AttributeError): return 422, {'status': 'invalid_report'}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        identifier = payload['submission_id'].lower()
        # Commit uncertain BEFORE calling GitHub. No request can repeat an uncertain attempt.
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT digest,state,receipt FROM attempts WHERE id=?', (identifier,)).fetchone()
            if row:
                if row[0] != digest: return 409, {'status': 'payload_conflict'}
                if row[1] == 'sent': return 200, json.loads(row[2])
                return 202, {'status': 'uncertain'}
            if not self.enabled or self.publisher is None or (self.pause_file and os.path.exists(self.pause_file)):
                return 503, {'status': 'unavailable'}
            count = db.execute('SELECT count(*) FROM attempts').fetchone()[0]
            recent = db.execute('SELECT count(*) FROM attempts WHERE created>?', (time.time() - 3600,)).fetchone()[0]
            if count >= self.quota or recent >= self.hourly_limit: return 429, {'status': 'limited'}
            db.execute('INSERT INTO attempts VALUES (?, ?, ?, ?, NULL)', (identifier, digest, 'uncertain', time.time()))
        try:
            receipt = self.publisher.create(payload)
            with self.connect() as db:
                db.execute('UPDATE attempts SET state=?,receipt=? WHERE id=?', ('sent', json.dumps(receipt), identifier))
            return 201, receipt
        except Exception:
            # Missing credentials, timeout, upstream error, or a lost persistence result: fail closed.
            return 202, {'status': 'uncertain'}

    def outcome(self, identifier):
        try:
            if str(uuid.UUID(identifier)) != identifier.lower(): raise ValueError()
        except (ValueError, AttributeError): return 400, {'status': 'invalid_identifier'}
        with self.connect() as db:
            row = db.execute('SELECT state,receipt FROM attempts WHERE id=?', (identifier.lower(),)).fetchone()
        if row and row[0] == 'sent': return 200, json.loads(row[1])
        return 202, {'status': 'uncertain'}  # Absence is not permission to POST again.


def handler(service):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def respond(self, status, value):
            body = json.dumps(value).encode()
            self.send_response(status); self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store'); self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
        def do_POST(self):
            if self.path != '/v1/feedback': return self.respond(404, {'status': 'not_found'})
            if self.headers.get('Content-Type') != 'application/json' or self.headers.get('Transfer-Encoding'):
                return self.respond(400, {'status': 'invalid_request'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size <= 0 or size > 24000: return self.respond(413, {'status': 'invalid_size'})
                self.connection.settimeout(10)
                def unique(pairs):
                    result = {}
                    for k, v in pairs:
                        if k in result: raise ValueError()
                        result[k] = v
                    return result
                payload = json.loads(self.rfile.read(size), object_pairs_hook=unique)
                status, result = service.submit(payload)
                self.respond(status, result)
            except (ValueError, UnicodeError): self.respond(400, {'status': 'invalid_request'})
            except Exception: self.respond(202, {'status': 'uncertain'})
        def do_GET(self):
            prefix = '/v1/feedback/'
            if not self.path.startswith(prefix): return self.respond(404, {'status': 'not_found'})
            try: self.respond(*service.outcome(self.path[len(prefix):]))
            except Exception: self.respond(202, {'status': 'uncertain'})
    return Handler


def configured_service():
    # Production destination is fixed server-side as well as absent from the request schema.
    if (os.environ['FEEDBACK_OWNER'], os.environ['FEEDBACK_REPOSITORY']) != ('doron3b', 'archi-feedback'):
        raise ValueError('unapproved destination')
    enabled = os.environ.get('FEEDBACK_ENABLED') == '1'
    database = os.environ['FEEDBACK_DATABASE']
    if not os.path.isfile(database):
        # Initial creation is deliberate and disabled. Missing production state cannot silently reset idempotency.
        if enabled or os.environ.get('FEEDBACK_INITIALIZE_DATABASE') != '1':
            raise ValueError('authoritative database required')
    names = ('FEEDBACK_OWNER', 'FEEDBACK_REPOSITORY', 'FEEDBACK_APP_ID',
             'FEEDBACK_INSTALLATION_ID', 'FEEDBACK_REPOSITORY_ID', 'FEEDBACK_KEY_FILE')
    publisher = Publisher(*(os.environ[k] for k in names)) if all(os.environ.get(k) for k in names) else None
    if enabled and publisher is None: raise ValueError('server configuration required')
    service = Service(os.environ['FEEDBACK_DATABASE'], publisher, quota=int(os.environ.get('FEEDBACK_QUOTA', '100')),
                      hourly_limit=int(os.environ.get('FEEDBACK_HOURLY_LIMIT', '20')))
    service.enabled = enabled
    service.pause_file = os.environ.get('FEEDBACK_PAUSE_FILE')
    return service


if __name__ == '__main__':
    # Local development only. Production uses Gunicorn and web:create_application().
    service = configured_service()
    ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('FEEDBACK_PORT', '8080'))), handler(service)).serve_forever()
