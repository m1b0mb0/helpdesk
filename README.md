# Helpdesk

Django backend for an AI-assisted support workflow. Incoming events are stored idempotently in PostgreSQL. Celery generates drafts using a bilingual knowledge base and Groq HTTP API, with a shared Redis rate limiter and crash recovery. Operators approve or reject drafts in Django Admin. Reply delivery is not implemented yet.

## Requirements

- Docker Engine or Docker Desktop with Docker Compose.
- Internet access for the initial image build.
- PostgreSQL 17 or newer for the webhook transaction timeout; Compose provides PostgreSQL 17.

## Start locally

From the repository root, using PowerShell:

```powershell
Copy-Item .env.example .env
```

Set `DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD` in `.env` before starting. Keep this file local; it is ignored by Git. The example contains configuration placeholders, not production credentials.

If `.env` already exists, keep its current values instead of copying the example over it.

```powershell
docker compose up --build
```

The web container waits for PostgreSQL, runs migrations, seeds eight knowledge articles, collects static files, and starts Gunicorn. The worker waits for healthy PostgreSQL, Redis, and web services, so database migrations have finished before it starts consuming tasks. Beat starts after PostgreSQL and web are ready.

- Health check: `http://localhost:8000/health` returns HTTP `200` with body `OK`.
- Django Admin: `http://localhost:8000/admin/`.
- Incoming events: `POST http://localhost:8000/webhooks/helpdesk`.
- Read-only knowledge base: `http://localhost:8000/knowledge/`.

`/health` checks that the web process can respond. It does not check PostgreSQL or Redis availability after startup. Compose checks those services separately; the worker health check uses Celery ping.

Create an administrator in another terminal:

```powershell
docker compose exec web python manage.py createsuperuser
```

The web port defaults to `8000` and can be changed with `WEB_PORT`. It is published on `127.0.0.1` only. PostgreSQL and Redis are accessible inside the Compose network and do not publish host ports.

## Services

| Service | Purpose |
| --- | --- |
| `db` | PostgreSQL 17 with a persistent volume. |
| `redis` | Redis 7.4 as the Celery broker, with a persistent volume, AOF enabled, `appendfsync everysec`, and `noeviction`. |
| `web` | Python 3.12 / Django 5.2, served by Gunicorn with two workers. WhiteNoise serves collected static files, including Django Admin assets. |
| `worker` | Celery prefork worker; concurrency defaults to two. Task results are ignored and no result backend is configured. |
| `beat` | Runs the PostgreSQL reconciler directly through a small custom Celery Beat scheduler. It does not publish periodic reconciliation messages to Redis. |

PostgreSQL and Redis have Compose health checks. The container configuration uses PostgreSQL at `db:5432` and the Redis broker at `redis://redis:6379/0`.

## Configuration

| Variable | Purpose |
| --- | --- |
| `DJANGO_SECRET_KEY` | Required Django secret key. |
| `DJANGO_DEBUG` | Debug mode; defaults to false. |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated allowed hostnames; the example permits local access. |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Database name and credentials. |
| `POSTGRES_POOL_TIMEOUT` | Maximum wait for a pooled database connection, in seconds; defaults to `0.3`. |
| `HELPDESK_ALLOWED_ORIGINS` | Comma-separated trusted reply URL origins; defaults to `http://web:8000,https://helpdesk.example.com`. |
| `WEB_PORT` | Host port for the web service; defaults to `8000`. |
| `CELERY_WORKER_CONCURRENCY` | Worker process count; defaults to `2`. |
| `CELERY_BROKER_VISIBILITY_TIMEOUT` | Redis message visibility timeout in seconds; defaults to `300`. |
| `GENERATION_ENABLED` | Defaults to `false`. Set `true` explicitly to enable automatic draft generation. |
| `GENERATION_RECONCILE_INTERVAL` | Reconciliation interval in seconds; defaults to `5`. |
| `GENERATION_MAX_INFLIGHT` | Global number of queued/running requests; defaults to worker concurrency. Set it to the intended total capacity if using multiple worker containers. |
| `GENERATION_MAX_ATTEMPTS`, `GENERATION_MAX_DISPATCHES` | Maximum execution attempts (`3`) and queue reservations (`10`) per request. |
| `GENERATION_QUEUE_LEASE_SECONDS`, `GENERATION_LEASE_SECONDS` | Queue reservation (`60`) and running execution (`120`) leases. |
| `PUBLIC_BASE_URL` | Public HTTP(S) origin for article links; defaults to `http://localhost:8000`. Match the host and port readers actually use. |
| `LLM_PROVIDER`, `LLM_MODEL` | Only `groq` is implemented; default model is `llama-3.3-70b-versatile`. Model access depends on your Groq account. |
| `LLM_API_KEY` | Groq API key. Empty selects the explicit no-key demo mode; an invalid nonempty key never does. |
| `LLM_HTTP_TIMEOUT` | Total HTTP deadline, including DNS and response reading; default `15s`, maximum `20s`. |
| `LLM_DEMO_DELAY_SECONDS` | Minimum total duration of a no-key attempt; default `5s`, permitted range `5..25s`. |
| `LLM_RATE_LIMIT_KEY` | Shared limiter namespace, default `helpdesk:llm:global`. Every worker sharing a quota must use the same key and Redis. |

The visibility timeout must exceed the maximum task execution time and any scheduled countdown or ETA delay. Revisit it before adding long-running or delayed tasks.

## Receive a helpdesk event

Send a UTF-8 JSON object with exactly these five fields. Every value must be a nonblank string; accepted strings are stored unchanged.

| Field | Validation |
| --- | --- |
| `event_id` | At most 255 characters; unique across events. |
| `ticket_id` | At most 255 characters; different events may share it. |
| `customer_email` | Valid email address, at most 254 characters. |
| `message` | At most 10,000 characters. |
| `reply_url` | At most 2,048 characters and permitted by the reply URL policy below. |

The endpoint requires `Content-Type: application/json` and accepts bodies up to 128 KiB. Malformed JSON, duplicate JSON keys, non-object JSON, missing or unexpected fields, wrong types, blank values, NUL characters, and invalid Unicode strings are rejected before database access.

| HTTP response | Meaning |
| --- | --- |
| `202 Accepted`, status `accepted` | A new event was committed to PostgreSQL. |
| `200 OK`, status `duplicate` | The same `event_id` and all original field values already exist; no new work is created. |
| `409 Conflict`, error `event_conflict` | The existing `event_id` has a different payload; the original event is unchanged. |
| `400 Bad Request` | Invalid JSON or field values. |
| `413 Content Too Large` | The body exceeds 128 KiB. |
| `415 Unsupported Media Type` | The request is not JSON. |
| `503 Service Unavailable`, error `database_unavailable` | Persistence or commit could not be confirmed. `Retry-After: 1` is included; retry the same event. |

A saved `SupportRequest` initially contains all original fields, a `pending` status, timestamps, attempt counters, a next-attempt time, and the last error. The webhook itself never publishes a Celery task. Beat finds work independently of the HTTP response; while generation is paused, accepted requests remain pending without consuming attempt budgets.

For a local PowerShell example:

```powershell
$uri = 'http://localhost:8000/webhooks/helpdesk'
$payload = @{
    event_id = 'evt_manual_001'
    ticket_id = 'T-1042'
    customer_email = 'user@example.com'
    message = 'Cannot export a report to PDF'
    reply_url = 'http://web:8000/tickets/T-1042/replies'
}
$body = $payload | ConvertTo-Json -Compress
Invoke-RestMethod -Method Post -Uri $uri -ContentType 'application/json' -Body $body
```

The first request returns `202` with status `accepted`. Repeat the last command with the same body to receive `200` with status `duplicate`. To check a conflict:

```powershell
$payload.message = 'A different message for the same event'
$body = $payload | ConvertTo-Json -Compress
Invoke-RestMethod -Method Post -Uri $uri -ContentType 'application/json' -Body $body
```

PowerShell reports the expected `409` HTTP error. Use a new `event_id` when trying another independent event.

### Reply URL policy

The URL must use an exact configured origin and the path `/tickets/<percent-encoded ticket_id>/replies`. User information, query strings, fragments, backslashes, and control characters are rejected. The `web` origin is for the future mock inside the Compose network; no mock delivery endpoint exists yet. The example HTTPS origin permits the employer's sample payload.

Validation performs no DNS lookup or HTTP request. This policy assumes the configured origins and their DNS are trusted. Future delivery must revalidate the destination, disable redirects, and preserve that trust boundary; an intake allowlist alone is not a complete outbound SSRF defense.

### Database time budget

Django uses the driver's connection pool, with one to four connections per process and a default acquisition timeout of 300 ms. New connections are established by the pool's background threads. The native connection timeout is two seconds; setting it to one would still be rounded up by Psycopg. The Linux TCP user timeout defaults to 1,000 ms.

The intake transaction sets local lock, statement, and transaction timeouts of 300 ms, 700 ms, and 1,200 ms respectively. These transaction-local settings do not shorten migrations or unrelated queries. A database error returns `503`, never an unconfirmed successful acknowledgement.

These controls provide a practical short failure path, not a universal guarantee that every HTTP response takes less than two seconds. HTTP queueing, network faults, DNS, and OS scheduling can add delay. If a commit outcome is uncertain or a response is lost, retrying the same `event_id` remains safe. Timing and load measurements must state their actual environment and concurrency.

## Background processing

Generation is opt-in: keep `GENERATION_ENABLED=false` while configuring the provider and public article origin, then enable it as shown below. Pending requests remain safely stored while generation is paused.

When enabled, the processing states are `pending → queued → generating → review`. Temporary failures return to `pending` with a persisted retry time; exhausted budgets and permanent errors become `failed`.

Every five seconds, the custom Beat scheduler checks broker availability, recovers expired queue/execution leases, and reserves due PostgreSQL rows. Reservation and claim transitions use short database transactions. A PostgreSQL advisory lock serializes planners; row locks and unique tokens protect individual requests. Only generation tasks enter the Redis `generation` queue. Run one Beat instance; the planner lock also protects against accidental overlapping reconcilers.

The global in-flight limit keeps the backlog in PostgreSQL instead of publishing every accepted request to Redis. A healthy broker is checked before reserving rows, so a Redis outage does not spend pending requests' dispatch budgets. Publication failures after that check are recorded and retried through PostgreSQL. A lost message is rediscovered after its queue reservation expires.

An execution increments its attempt count when claimed. No database transaction is held while the generator runs. Completion requires the current execution token and an unexpired lease; duplicate deliveries and results from stale workers cannot overwrite another execution. A crashed worker is recovered after its lease expires. A provider may still receive a repeated external call after a crash; this mechanism guarantees one accepted result, not exactly-once HTTP execution.

Retries use PostgreSQL `next_attempt_at`, with exponential backoff from 5 seconds up to 60 seconds, extended by a provider's numeric `Retry-After` (up to one hour). There are no competing Celery retries or long broker-held countdowns. Defaults allow three execution attempts and ten dispatch reservations; exhausting either budget leaves a visible error. A limiter denial or outage occurs before HTTP: it defers the request without spending those budgets, with a persisted wait of at least five seconds (60 seconds on Redis failure). Such waiting can continue through an outage; the backlog remains in PostgreSQL and the in-flight publication cap still applies.

Celery uses late acknowledgements, worker-loss rejection, prefetch `1`, and cancellation on broker connection loss. The soft limit is 60 seconds, hard limit 90 seconds, execution lease 120 seconds, and Redis visibility timeout 300 seconds. Django system checks reject inconsistent timeout/acknowledgment settings. The worker has a 100-second graceful shutdown period.

Inspect requests, counters, leases, and errors in the read-only Support Requests page in Django Admin. After investigating a terminal failure, an operator can reset its budgets explicitly; replace `123` with that request's database ID:

```powershell
docker compose exec web python manage.py shell -c "from support.processing import retry_failed_generation; print(retry_failed_generation(123))"
```

This returns the request to `pending`; generation must also be enabled. Resetting budgets is a deliberate operator action.

## Operator moderation

Use `/admin/support/supportrequest/` to search by event ID, ticket ID or email and filter by status. The list shows the customer email and creation/update dates. Open a ticket to read the original message, draft, verified knowledge links and generation error. Technical fields are read-only, including for users with the ordinary change permission.

A superuser can moderate directly. For a restricted operator, create an active staff user in Django Admin and assign these two permissions, directly or through a group:

- `support | support request | Can view support request`
- `support | support request | Can moderate support request drafts`

The ordinary change permission does not grant moderation. Viewing without the custom moderation permission remains read-only. Admin login and CSRF protection apply to both actions.

To moderate a draft:

1. Filter the list to **Awaiting review** and open the ticket to inspect its draft and sources.
2. Return to the list, select the ticket, choose **Approve selected drafts** or **Reject selected drafts**, then click **Go**.
3. Check the action summary and resulting status. Bulk selections validate each row independently; non-review or invalid drafts are skipped instead of bypassing checks.

Approval uses a short PostgreSQL transaction and row lock, revalidates the draft and its sources, and saves an exact `approved_text` snapshot, one unique `delivery_key`, the operator and decision timestamp. Repeated or competing decisions cannot replace these values. Reject records the operator/time and creates no delivery intent; even a malformed draft can be safely rejected. The database rejects incomplete approval records and rejected records with delivery intent.

`approved` means **awaiting delivery**, not sent. No HTTP or Celery publication occurs during moderation; C2 will send the frozen `approved_text` and reuse the same `delivery_key` for retries. The generation reconciler ignores both `approved` and `rejected`. Decisions cannot be undone in this interface, and draft editing is not supported. A later source edit can invalidate an existing draft; approval then fails safely. Referenced moderator accounts are protected from deletion to preserve the audit record; deactivate them instead.

Focused tests, including races on independent PostgreSQL connections:

```powershell
docker compose exec web python manage.py test support.tests.test_admin support.tests.test_moderation
```

## Knowledge base and LLM generation

The eight AI-authored MetricNest articles cover PDF/CSV export, data refresh, scheduled emails, permissions, date/time zones, billing, and account access. They contain Ukrainian and English text. Search ranks explicit bilingual topic aliases/prefixes and returns at most three articles; it intentionally does not match generic words such as "report". This is a small lexical corpus, not semantic search. No relevant article results in a visible `failed` request requiring investigation, with no invented answer or source.

Seeding runs on web startup and can be repeated manually:

```powershell
docker compose exec web python manage.py seed_knowledge
```

Stable unique slugs preserve article IDs and URLs across runs. Seeding restores the canonical eight articles, including edited text; additional slugs are preserved. `/knowledge/` and `/knowledge/<slug>` allow GET/HEAD only and escape stored HTML.

Groq receives the message and retrieved article text in a JSON user-data envelope, separate from system instructions. It returns a plain answer and retrieved article IDs. The application rejects model-provided hyperlinks, markup, unknown IDs and incomplete/invalid responses, then attaches canonical URLs from PostgreSQL. `finish_generation()` checks sources again before the existing fenced update. These checks mitigate obvious prompt injection and forged links; they do not prove every generated claim is correct. Operator review remains necessary.

The integration uses Python's HTTP client facilities, without an SDK or new dependency. Each task makes at most one HTTP attempt: no automatic retries, redirects, tools or proxy discovery. 429/temporary HTTP errors, network failures and invalid responses are retryable; other HTTP rejections, including 401, are permanent. Provider response bodies and credentials are not stored in errors. Retries use the existing PostgreSQL workflow.

Without a key, the same real Groq HTTP POST is attempted with no Authorization header. Its HTTP outcome or network error is recorded in an informational log and ignored in favor of an explicitly marked static demo draft with only retrieved article URLs. The minimum total duration includes the HTTP attempt: a fast 401 waits out five seconds; a 15-second timeout does not add five more. A POSIX alarm bounds the entire HTTP operation, including DNS and slowly streamed responses. This requires the supplied Linux prefork deployment; unsupported execution modes fail explicitly instead of creating a demo without HTTP. HTTP deadline plus configured demo delay stays below 30 seconds under normal scheduling; the 60/90/120-second task/lease limits remain unchanged.

The atomic Redis limiter allows at most 60 outstanding/recent HTTP attempts globally. It retains each permit until HTTP completes plus 61 seconds, including errors and no-key requests. Unfinished permits expire after the hard task limit plus 61 seconds and a five-second margin: currently 156 seconds. This conservative scheme also covers a worker paused between acquiring permission and starting HTTP, at the cost of throughput below 60/minute for long calls.

**Cold start, Redis restart, or missing limiter state imposes a 156-second recovery pause.** The Lua script checks Redis `run_id` atomically and waits out old running tasks plus a full window before issuing new permits; this covers lost recent AOF writes. Redis failure blocks HTTP. Correctness assumes all workers share one Redis/key, stable Redis server time, and an enforced 90-second prefork hard limit. Do not delete/modify limiter keys or bypass the generator when testing quota behavior. A custom Redis ACL must permit the Lua script's INFO, TIME, hash and sorted-set operations.

To enable generation, set `GENERATION_ENABLED=true` in your local `.env`, set `PUBLIC_BASE_URL`, and optionally supply `LLM_API_KEY`, then recreate the services:

```powershell
docker compose up -d --build
```

Send a webhook about PDF export, wait for the initial limiter pause if needed, and inspect `review`, `draft_text` and `draft_sources` in Django Admin. With an empty key the draft is clearly marked as a demonstration. A configured key uses the real provider response and is subject to the account's own quotas, which can be lower than the service limit. See the [Groq API reference](https://console.groq.com/docs/api-reference).

The custom Beat scheduler overrides `tick()` and currently runs only generation reconciliation. It does **not** execute the standard `beat_schedule`; future delivery reconciliation must be explicitly integrated into this scheduler or the scheduler changed with equivalent backlog protections.

## Verify the setup

Run Django checks and tests:

```powershell
docker compose exec web python manage.py check
docker compose exec web python manage.py test
```

Webhook tests use PostgreSQL; concurrent cases use independent database connections. The suite covers validation, duplicates and conflicts, persistence failures, concurrency, response timing, and preservation of unique events under load. A local timing result does not establish a production latency guarantee.

Run the focused background-processing tests with PostgreSQL and Redis available:

```powershell
docker compose exec web python manage.py test support.tests.test_processing support.tests.test_celery_processing
```

All automated tests live in `support/tests/`. Provider tests use controlled HTTP responses; limiter tests use real Redis and eight independent processes with a local HTTP server. These do not prove successful authenticated Groq generation. The prefork crash test deliberately logs `WorkerLostError`; use the final test result to determine whether recovery passed. Do not enable a test generator for real accepted requests.

Celery task completion describes the handler, not the business outcome. Inspect PostgreSQL/Admin status and `last_error` to distinguish `review`, deferred retries, and terminal failures.

Publish the smoke task, which queries PostgreSQL through Django's user model:

```powershell
docker compose exec web python manage.py shell -c "from support.tasks import check_database; print(check_database.delay().id)"
docker compose logs --tail=50 worker
```

Confirm that the worker reports a successful `check_database` task and its PostgreSQL user count. The printed task ID confirms publication, not successful execution; results are not stored in a result backend.

Inspect service status and logs:

```powershell
docker compose ps
docker compose logs --tail=100 web worker beat
```

## Stop or rebuild

```powershell
docker compose down
```

This preserves the PostgreSQL and Redis volumes. PostgreSQL initialization variables apply when its data volume is first created; changing them in `.env` does not change credentials in an existing database.

After changing application code, dependencies, or container configuration, rebuild and start again:

```powershell
docker compose up --build
```

## Current reliability limits

Celery uses late acknowledgements and rejects tasks when a worker process is lost. Tasks may execute more than once; these settings do not provide business-level deduplication or exactly-once execution. Redis AOF with `everysec` can lose approximately one second of writes during a crash.

Webhook acceptance and deduplication are stored in PostgreSQL, independently of Redis availability. Redis message expiration prevents late execution but does not physically remove expired list entries until a worker reads them. The global in-flight limit and bounded dispatch reservations limit business-message duplication; obsolete messages are ignored using PostgreSQL tokens when consumed. Prolonged failures can exhaust a request's budget and require the explicit operator retry described above.

Knowledge retrieval, draft generation and operator moderation are implemented, but generation remains disabled by default until explicitly enabled. Mock helpdesk delivery and outgoing reply idempotency remain later work.

This configuration is for a local demonstration. Public hosting requires deployment-specific configuration, including TLS and appropriate access controls.
