"""
Typed adapter failures.

The pipeline needs to tell three cases apart, because it reacts differently to
each:

    AdapterAuthError    -- credentials wrong/absent. Retrying is pointless;
                           drop the source for the whole process.
    AdapterRateLimited  -- we are going too fast. Retrying *is* correct, after
                           the server's Retry-After.
    AdapterUnavailable  -- timeout, 5xx, DNS failure. Retry a few times, then
                           serve the query from the remaining sources.

Before this, every failure surfaced as a bare `Exception` caught by a blanket
`except Exception` in the pipeline, so a typo in an API key looked exactly like
a transient network blip and got retried forever.
"""

from __future__ import annotations


class AdapterError(Exception):
    """Base class for every source-adapter failure."""

    retryable: bool = False

    def __init__(self, message: str, *, source: str | None = None):
        super().__init__(message)
        self.source = source

    def __str__(self) -> str:
        base = super().__str__()
        return f"[{self.source}] {base}" if self.source else base


class AdapterAuthError(AdapterError):
    """Missing or rejected credentials. Not retryable."""

    retryable = False


class AdapterRateLimited(AdapterError):
    """429 / quota exhausted. Retryable after `retry_after` seconds."""

    retryable = True

    def __init__(self, message: str, *, source: str | None = None,
                 retry_after: float | None = None):
        super().__init__(message, source=source)
        self.retry_after = retry_after


class AdapterUnavailable(AdapterError):
    """Timeout, 5xx or transport error. Retryable."""

    retryable = True


class AdapterBadResponse(AdapterError):
    """2xx with a body we cannot parse. Not retryable -- the shape is wrong."""

    retryable = False
