"""Conservative, service-wide permits for actual LLM HTTP attempts.

A permit occupies one of 60 slots before HTTP starts and until 61 seconds
after HTTP finishes. This also covers a process paused between acquiring a
permit and opening its socket. An abandoned permit lives for the hard task
limit plus that window and a safety margin. Call only from bounded tasks.
"""

from dataclasses import dataclass
from functools import lru_cache
from math import ceil
from uuid import uuid4

from django.conf import settings
from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

LIMIT = 60
WINDOW_MS = 61_000  # One second of margin at the rolling-window boundary.
SAFETY_MS = 5_000

_ACQUIRE = """
local clock = redis.call('TIME')
local now = clock[1] * 1000 + math.floor(clock[2] / 1000)
local run_id = string.match(redis.call('INFO', 'server'), 'run_id:([a-f0-9]+)')
local epoch = redis.call('HGET', KEYS[1], 'epoch')
if epoch ~= run_id or redis.call('EXISTS', KEYS[2]) == 0 then
    redis.call('HSET', KEYS[1], 'epoch', run_id, 'warm_until', now + ARGV[2])
    redis.call('DEL', KEYS[2])
    redis.call('ZADD', KEYS[2], '+inf', '__state__')
    return {0, tonumber(ARGV[2]), run_id}
end
local warm_until = tonumber(redis.call('HGET', KEYS[1], 'warm_until'))
if now < warm_until then return {0, warm_until - now, run_id} end
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
if redis.call('ZCARD', KEYS[2]) - 1 >= tonumber(ARGV[3]) then
    local oldest = redis.call('ZRANGE', KEYS[2], 0, 0, 'WITHSCORES')
    return {0, math.max(1, tonumber(oldest[2]) - now), run_id}
end
redis.call('ZADD', KEYS[2], now + ARGV[2], ARGV[1])
return {1, 0, run_id}
"""

_FINISH = """
if redis.call('HGET', KEYS[1], 'epoch') ~= ARGV[1] then return 0 end
if not redis.call('ZSCORE', KEYS[2], ARGV[2]) then return 0 end
local clock = redis.call('TIME')
local now = clock[1] * 1000 + math.floor(clock[2] / 1000)
redis.call('ZADD', KEYS[2], 'XX', now + ARGV[3], ARGV[2])
return 1
"""


class RateLimitExceeded(Exception):
    def __init__(self, retry_after):
        self.retry_after = max(1, ceil(retry_after))
        super().__init__(f"Global LLM limit unavailable; retry in {self.retry_after}s.")


class RateLimitUnavailable(Exception):
    """Redis could not establish a safe permission; no HTTP may be sent."""


@dataclass(frozen=True)
class RateLimitTicket:
    token: str
    epoch: str


@lru_cache(maxsize=4)
def _client(url):
    # No transport retry: every generation execution performs at most one
    # explicit attempt, and application retries remain visible in PostgreSQL.
    return Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
        retry=Retry(NoBackoff(), 0),
        retry_on_timeout=False,
    )


def _keys():
    return (
        settings.LLM_RATE_LIMIT_KEY + ":state",
        settings.LLM_RATE_LIMIT_KEY + ":slots",
    )


def acquire_slot():
    """Acquire once without sleeping; callers persist a deferred retry on denial."""
    client = _client(settings.LLM_REDIS_URL)
    try:
        # run_id changes even when Redis restores an AOF with a few lost writes.
        # A new epoch waits out old in-flight tasks and the entire rolling window.
        token = uuid4().hex
        hold_ms = int(settings.CELERY_TASK_TIME_LIMIT * 1000) + WINDOW_MS + SAFETY_MS
        allowed, wait_ms, epoch = client.eval(
            _ACQUIRE, 2, *_keys(), token, hold_ms, LIMIT,
        )
    except (RedisError, KeyError, ValueError, TypeError):
        raise RateLimitUnavailable("Redis LLM rate limiter is unavailable; HTTP was not attempted.") from None
    if not allowed:
        raise RateLimitExceeded(wait_ms / 1000)
    return RateLimitTicket(token, epoch)


def finish_slot(ticket):
    """Retain the attempt for a full window, including failed HTTP requests.

    On failure the original, longer reservation remains. Never remove a slot
    because its HTTP response failed or Redis became unavailable.
    """
    try:
        _client(settings.LLM_REDIS_URL).eval(
            _FINISH, 2, *_keys(), ticket.epoch, ticket.token, WINDOW_MS,
        )
    except RedisError:
        # The conservative outstanding reservation remains until its deadline.
        # The actual provider result must not be overwritten by cleanup failure.
        pass
