"""Single low-volume Render instance; HTTPS terminates at Render."""
import os

port = int(os.environ['PORT'])
if not 1 <= port <= 65535: raise ValueError('invalid port')
bind = f'0.0.0.0:{port}'
workers = 1
worker_class = 'sync'
threads = 1
backlog = 16
timeout = 45
graceful_timeout = 30
umask = 0o077
limit_request_line = 256
limit_request_fields = 32
limit_request_field_size = 4096
accesslog = None
errorlog = '-'
loglevel = 'critical'
capture_output = False
