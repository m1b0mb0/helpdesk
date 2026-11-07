# Helpdesk

Django backend for an AI-assisted support workflow. Incoming helpdesk events are validated and stored idempotently in PostgreSQL. Draft generation, moderation, and reply delivery are not implemented yet. Celery is configured and has a database-backed smoke task; accepted events remain pending until the next processing stage is implemented.

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

The web container waits for PostgreSQL, runs migrations, collects static files, and starts Gunicorn. The worker waits for healthy PostgreSQL, Redis, and web services, so database migrations have finished before it starts consuming tasks.

- Health check: `http://localhost:8000/health` returns HTTP `200` with body `OK`.
- Django Admin: `http://localhost:8000/admin/`.
- Incoming events: `POST http://localhost:8000/webhooks/helpdesk`.

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

A saved `SupportRequest` contains all original fields, a `pending` status, timestamps, an attempt count, a next-attempt time, and the last error. No Celery task is published and no draft is generated at this stage. Pending rows are the durable source for future processing and recovery.

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

## Verify the setup

Run Django checks and tests:

```powershell
docker compose exec web python manage.py check
docker compose exec web python manage.py test
```

Webhook tests use PostgreSQL; concurrent cases use independent database connections. The suite covers validation, duplicates and conflicts, persistence failures, concurrency, response timing, and preservation of unique events under load. A local timing result does not establish a production latency guarantee.

Publish the smoke task, which queries PostgreSQL through Django's user model:

```powershell
docker compose exec web python manage.py shell -c "from support.tasks import check_database; print(check_database.delay().id)"
docker compose logs --tail=50 worker
```

Confirm that the worker reports a successful `check_database` task and its PostgreSQL user count. The printed task ID confirms publication, not successful execution; results are not stored in a result backend.

Inspect service status and logs:

```powershell
docker compose ps
docker compose logs --tail=100 web worker
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

Webhook acceptance and deduplication are stored in PostgreSQL, independently of Redis availability. Events currently remain pending. The next application stage must add actual Celery processing and a periodic reconciler that republishes all due or stale work, including work whose broker message was lost after publication. No reconciler or Celery Beat service exists yet. Global LLM rate limiting and outgoing reply idempotency remain future work.

This configuration is for a local demonstration. Public hosting requires deployment-specific configuration, including TLS and appropriate access controls.
