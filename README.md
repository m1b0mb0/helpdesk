# Helpdesk

Django backend for an AI-assisted support workflow. This stage provides the container setup and a database-backed Celery smoke task; webhook processing, draft generation, moderation, and reply delivery are not implemented yet.

## Requirements

- Docker Engine or Docker Desktop with Docker Compose.
- Internet access for the initial image build.

## Start locally

From the repository root, using PowerShell:

```powershell
Copy-Item .env.example .env
```

Set `DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD` in `.env` before starting. Keep this file local; it is ignored by Git. The example contains configuration placeholders, not production credentials.

```powershell
docker compose up --build
```

The web container waits for PostgreSQL, runs migrations, collects static files, and starts Gunicorn. The worker waits for healthy PostgreSQL, Redis, and web services, so database migrations have finished before it starts consuming tasks.

- Health check: `http://localhost:8000/health` returns HTTP `200` with body `OK`.
- Django Admin: `http://localhost:8000/admin/`.

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
| `WEB_PORT` | Host port for the web service; defaults to `8000`. |
| `CELERY_WORKER_CONCURRENCY` | Worker process count; defaults to `2`. |
| `CELERY_BROKER_VISIBILITY_TIMEOUT` | Redis message visibility timeout in seconds; defaults to `300`. |

The visibility timeout must exceed the maximum task execution time and any scheduled countdown or ETA delay. Revisit it before adding long-running or delayed tasks.

## Verify the setup

Run Django checks and tests:

```powershell
docker compose exec web python manage.py check
docker compose exec web python manage.py test
```

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

The next application stage must keep business work in PostgreSQL and add a periodic reconciler that republishes all due or stale work. No reconciler or Celery Beat service exists yet. The global LLM rate limiter, webhook deduplication, and outgoing reply idempotency are also future work.

This configuration is for a local demonstration. Public hosting requires deployment-specific configuration, including TLS and appropriate access controls.
