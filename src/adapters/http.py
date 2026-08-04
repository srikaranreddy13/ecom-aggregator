"""
Shared HTTP client for live adapters: rate limiting, retry with exponential
backoff and jitter, and mapping of transport failures onto typed AdapterErrors.

Three things a real client needs that a bare `requests.get` does not:

  1. RATE LIMITING. A polite client self-throttles rather than discovering the
     limit by being blocked. `RateLimiter` enforces a minimum gap between calls
     per host, so a 200-item watchlist loop cannot burst.

  2. RETRY WITH JITTER. Retrying a timeout is correct; retrying a 401 is not.
     `retryable` on the exception type decides. Backoff is exponential with
     random jitter -- without jitter, several adapters that fail together
     retry in lockstep and hammer the source in synchronised waves.

  3. HONOURING Retry-After. When a server tells you how long to wait, guessing
     instead is how a key gets suspended.

Everything is configured from src/config.py, so timeouts and limits are
environment-tunable without code changes.
"""

from __future__ import annotations

import random
import threading
import time
from collections import defaultdict
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from ..config import settings
from ..logging_setup import get_logger
from .errors import (
    AdapterAuthError,
    AdapterBadResponse,
    AdapterError,
    AdapterRateLimited,
    AdapterUnavailable,
)

log = get_logger(__name__)


class RateLimiter:
    """Per-host minimum-interval throttle. Thread-safe."""

    def __init__(self, per_second: float | None = None):
        rate = per_second if per_second is not None else settings.http_rate_limit_per_sec
        self.min_interval = 1.0 / rate if rate > 0 else 0.0
        self._last: dict[str, float] = defaultdict(float)
        self._lock = threading.Lock()

    def wait(self, url: str) -> float:
        """Block until this host may be called again. Returns seconds slept."""
        if self.min_interval <= 0:
            return 0.0
        host = urlparse(url).netloc or url
        with self._lock:
            now = time.monotonic()
            gap = now - self._last[host]
            sleep_for = max(0.0, self.min_interval - gap)
            # Reserve the slot before releasing the lock so concurrent callers
            # queue behind us instead of all computing the same free slot.
            self._last[host] = now + sleep_for
        if sleep_for:
            time.sleep(sleep_for)
        return sleep_for


_limiter = RateLimiter()


def _classify(resp: requests.Response, source: str) -> AdapterError | None:
    """Map an HTTP status onto a typed error, or None if the response is fine."""
    code = resp.status_code
    if code < 400:
        return None
    if code in (401, 403):
        return AdapterAuthError(
            f"HTTP {code} -- credentials rejected or insufficient scope", source=source
        )
    if code == 429:
        # Retry-After may also be an HTTP-date; we ignore that form on purpose
        # and let plain backoff cover it rather than parsing two formats.
        retry_after = resp.headers.get("Retry-After")
        try:
            wait = float(retry_after) if retry_after else None
        except ValueError:
            wait = None
        return AdapterRateLimited("HTTP 429 -- rate limited",
                                 source=source, retry_after=wait)
    if code >= 500:
        return AdapterUnavailable(f"HTTP {code} -- upstream error", source=source)
    return AdapterBadResponse(
        f"HTTP {code} -- {resp.text[:200]}", source=source
    )


def request(
    method: str,
    url: str,
    *,
    source: str = "http",
    max_retries: int | None = None,
    limiter: RateLimiter | None = None,
    session: requests.Session | None = None,
    **kwargs: Any,
) -> requests.Response:
    """Rate-limited, retrying HTTP call. Raises a typed AdapterError on failure."""
    retries = settings.http_max_retries if max_retries is None else max_retries
    limiter = limiter or _limiter
    caller = session or requests
    kwargs.setdefault("timeout", settings.http_timeout_seconds)

    headers = dict(kwargs.pop("headers", None) or {})
    headers.setdefault("User-Agent", settings.user_agent)

    last: AdapterError | None = None

    for attempt in range(retries + 1):
        limiter.wait(url)
        try:
            resp = caller.request(method, url, headers=headers, **kwargs)
        except requests.Timeout as exc:
            last = AdapterUnavailable(f"timeout after {kwargs['timeout']}s: {exc}",
                                      source=source)
        except requests.RequestException as exc:
            last = AdapterUnavailable(f"transport error: {exc}", source=source)
        else:
            err = _classify(resp, source)
            if err is None:
                return resp
            last = err

        if not last.retryable or attempt == retries:
            log.warning("http call failed",
                        extra={"source": source, "url": url, "attempt": attempt + 1,
                               "attempts_allowed": retries + 1,
                               "retryable": last.retryable, "error": str(last)})
            raise last

        # exponential backoff with full jitter; a server-supplied Retry-After wins
        delay = settings.http_backoff_base * (2 ** attempt)
        delay = random.uniform(0, delay)
        server_hint = getattr(last, "retry_after", None)
        if server_hint:
            delay = max(delay, float(server_hint))

        log.info("retrying http call",
                 extra={"source": source, "attempt": attempt + 1,
                        "sleep_seconds": round(delay, 2), "error": str(last)})
        time.sleep(delay)

    raise last  # unreachable, but keeps the type checker and the reader happy


def get(url: str, **kwargs: Any) -> requests.Response:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs: Any) -> requests.Response:
    return request("POST", url, **kwargs)


def json_body(resp: requests.Response, source: str = "http") -> Any:
    """Parse a JSON body, turning a malformed one into a typed error."""
    try:
        return resp.json()
    except ValueError as exc:
        raise AdapterBadResponse(
            f"response was not JSON: {exc}; body starts {resp.text[:120]!r}",
            source=source,
        ) from exc


def guard(fn: Callable[..., Any], *, source: str) -> Callable[..., Any]:
    """Wrap a callable so any stray exception surfaces as an AdapterError.

    Used at the adapter boundary: whatever a third-party client does internally,
    the pipeline only ever has to handle AdapterError.
    """

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except AdapterError:
            raise
        except Exception as exc:  # noqa: BLE001 -- deliberate boundary
            raise AdapterUnavailable(f"unexpected adapter failure: {exc}",
                                     source=source) from exc

    return wrapper
