# Archi Feedback service

This directory is a reviewed source snapshot for the small Archi Feedback V1 service. It accepts only the exact public report reviewed and explicitly sent by an Archi user. Users do not need a GitHub account, login or token.

Destination: doron3b/archi-feedback. The server uses a dedicated GitHub App installed on this repository only, with Issues read/write and required Metadata read-only. No client credentials, private source access, hidden context, diagnostics, attachments or telemetry are part of the protocol.

POST /v1/feedback accepts only schema_version, random submission_id, title, body and kind. GET /v1/feedback/<submission_id> checks the existing outcome. Health checks use GET /healthz. The allowed kinds are bug, blocker and improvement. Existing label names are preserved.

Run as one Render native Python web service in Frankfurt, with one 512 MB instance and a 1 GB persistent disk mounted at /var/data. Use Render-managed HTTPS. Build: `pip install -r requirements.txt`. Start: `gunicorn --config gunicorn.conf.py 'web:create_application()'`. The runtime binds 0.0.0.0 at Render's PORT. Set PYTHON_VERSION=3.14.7.

Set FEEDBACK_OWNER=doron3b, FEEDBACK_REPOSITORY=archi-feedback, FEEDBACK_DATABASE=/var/data/attempts.sqlite3, FEEDBACK_PAUSE_FILE=/var/data/PAUSED, FEEDBACK_QUOTA=100, FEEDBACK_HOURLY_LIMIT=20 and FEEDBACK_ENABLED=0 initially. Initial empty database creation requires explicit FEEDBACK_INITIALIZE_DATABASE=1 while disabled; remove that flag after bootstrap and before publication. An absent database never authorizes automatic reset.

After separately approved credential provisioning, server-only identifiers are FEEDBACK_APP_ID, FEEDBACK_INSTALLATION_ID and FEEDBACK_REPOSITORY_ID. A secret file named feedback-app.pem is mounted at /etc/secrets/feedback-app.pem; FEEDBACK_KEY_FILE points there. Keep key material out of source, logs and client artifacts. Do not create or enable credentials as part of building this source snapshot. No user OAuth or personal token is used.

The service commits an uncertain attempt before one GitHub Issue-create call. Duplicate sent requests return the saved receipt; uncertain requests do not publish again. A lost receipt requires independent operator confirmation of the exact attempt-to-Issue association, then read-only verification through recover.py. It does not search, infer an association from matching text, or retry publication. Ambiguous outcomes remain unresolved; there is no exactly-once guarantee.

Publication can be paused immediately by creating /var/data/PAUSED; receipt reads remain available. Preserve the authoritative database and use consistent SQLite backups. Disable publication after disk loss or snapshot restoration until all potentially lost attempts are reconciled. Do not discard idempotency history to make capacity available.

Application access logs are disabled, errors are sanitized, and the runtime bounds requests, headers and worker execution. Provider-side logging and abuse protection still require operational verification. Manual deployments use a reviewed public commit; no GitHub provider integration or automatic deployment is required. Public source publication, service provisioning, secrets, deployment and client enablement require their respective owner approvals.
