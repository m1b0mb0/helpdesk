"""One bounded HTTP attempt, without SDK retries, redirects or proxy discovery.

The supported deployment is Linux Celery prefork. A POSIX deadline also bounds
DNS and slowly streamed responses, unlike a socket inactivity timeout alone.
"""

import json
import signal
import threading
from contextlib import contextmanager
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from django.conf import settings

ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
MAX_RESPONSE_BYTES = 128 * 1024


class ProviderNetworkError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@contextmanager
def http_deadline(seconds):
    if not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        from .generation import PermanentGenerationError

        raise PermanentGenerationError("HTTP deadline requires Linux prefork/main thread.")

    def expired(signum, frame):
        raise TimeoutError("LLM HTTP deadline exceeded")

    previous = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def post_completion(payload, api_key):
    headers = {"Content-Type": "application/json", "User-Agent": "MetricNest-Helpdesk/1.0"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    request = Request(ENDPOINT, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with http_deadline(settings.LLM_HTTP_TIMEOUT):
            try:
                response = opener.open(request, timeout=settings.LLM_HTTP_TIMEOUT)
            except HTTPError as error:
                response = error
            with response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ProviderNetworkError("Oversized LLM response")
                length = response.headers.get("Content-Length")
                if length is not None and len(body) != int(length):
                    raise ProviderNetworkError("Truncated LLM response")
                return response.code, {k.lower(): v for k, v in response.headers.items()}, body
    except (URLError, OSError, ValueError, HTTPException) as error:
        raise ProviderNetworkError("LLM HTTP transport failed") from error
