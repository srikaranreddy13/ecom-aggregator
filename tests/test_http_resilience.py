"""
HTTP layer tests: status classification, retry policy, rate limiting.

No network is touched -- a fake session returns scripted responses. The point
is to prove the RETRY POLICY is right, because getting it wrong is expensive in
both directions: retrying a 401 wastes quota forever, and not retrying a 503
loses a source to a blip.
"""

import time

import pytest
import requests

from src.adapters import http
from src.adapters.errors import (
    AdapterAuthError,
    AdapterBadResponse,
    AdapterRateLimited,
    AdapterUnavailable,
)

URL = "https://example.invalid/api"


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
        self.text = text or ""

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """Replays a scripted list of responses/exceptions and counts calls."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        item = self.script.pop(0) if self.script else self.script_default()
        if isinstance(item, Exception):
            raise item
        return item

    def script_default(self):
        return FakeResponse(200, {"ok": True})


@pytest.fixture
def no_sleep(monkeypatch):
    """Backoff is correct behaviour but must not slow the suite down."""
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    monkeypatch.setattr(http, "_limiter", http.RateLimiter(per_second=0))


# ---------------------------------------------------------------- classification


@pytest.mark.parametrize("code,expected", [
    (401, AdapterAuthError),
    (403, AdapterAuthError),
    (429, AdapterRateLimited),
    (500, AdapterUnavailable),
    (503, AdapterUnavailable),
    (400, AdapterBadResponse),
    (404, AdapterBadResponse),
])
def test_status_maps_to_typed_error(code, expected, no_sleep):
    session = FakeSession([FakeResponse(code)] * 10)
    with pytest.raises(expected):
        http.get(URL, source="test", session=session, max_retries=1)


def test_success_returns_response(no_sleep):
    session = FakeSession([FakeResponse(200, {"ok": True})])
    resp = http.get(URL, source="test", session=session)
    assert resp.status_code == 200
    assert session.calls == 1


# ---------------------------------------------------------------- retry policy


def test_auth_failure_is_not_retried(no_sleep):
    """Retrying a bad credential is pure latency -- it will never succeed."""
    session = FakeSession([FakeResponse(401)] * 5)
    with pytest.raises(AdapterAuthError):
        http.get(URL, source="test", session=session, max_retries=3)
    assert session.calls == 1


def test_bad_request_is_not_retried(no_sleep):
    session = FakeSession([FakeResponse(400, text="bad")] * 5)
    with pytest.raises(AdapterBadResponse):
        http.get(URL, source="test", session=session, max_retries=3)
    assert session.calls == 1


def test_server_error_is_retried_then_gives_up(no_sleep):
    session = FakeSession([FakeResponse(503)] * 10)
    with pytest.raises(AdapterUnavailable):
        http.get(URL, source="test", session=session, max_retries=3)
    assert session.calls == 4          # 1 initial + 3 retries


def test_transient_failure_then_success(no_sleep):
    """The case that justifies retrying at all."""
    session = FakeSession([
        FakeResponse(503),
        requests.Timeout("slow"),
        FakeResponse(200, {"ok": True}),
    ])
    resp = http.get(URL, source="test", session=session, max_retries=3)
    assert resp.status_code == 200
    assert session.calls == 3


def test_timeout_is_wrapped_not_leaked(no_sleep):
    session = FakeSession([requests.Timeout("too slow")] * 5)
    with pytest.raises(AdapterUnavailable, match="timeout"):
        http.get(URL, source="test", session=session, max_retries=1)


def test_connection_error_is_wrapped(no_sleep):
    session = FakeSession([requests.ConnectionError("dns")] * 5)
    with pytest.raises(AdapterUnavailable, match="transport error"):
        http.get(URL, source="test", session=session, max_retries=1)


def test_source_name_is_attached(no_sleep):
    session = FakeSession([FakeResponse(401)])
    with pytest.raises(AdapterAuthError) as exc:
        http.get(URL, source="ebay", session=session)
    assert exc.value.source == "ebay"
    assert "[ebay]" in str(exc.value)


def test_retry_after_header_is_honoured(monkeypatch):
    """When a server says how long to wait, guessing shorter risks a ban."""
    slept = []
    monkeypatch.setattr(http.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(http, "_limiter", http.RateLimiter(per_second=0))

    session = FakeSession([
        FakeResponse(429, headers={"Retry-After": "7"}),
        FakeResponse(200, {"ok": True}),
    ])
    http.get(URL, source="test", session=session, max_retries=2)
    assert slept and max(slept) >= 7.0


def test_malformed_retry_after_does_not_crash(no_sleep):
    session = FakeSession([
        FakeResponse(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        FakeResponse(200, {"ok": True}),
    ])
    assert http.get(URL, source="test", session=session, max_retries=2).status_code == 200


def test_backoff_grows(monkeypatch):
    """Full-jitter backoff has a growing ceiling; assert the ceiling, not the
    draw, since the delay is deliberately random."""
    slept = []
    monkeypatch.setattr(http.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(http, "_limiter", http.RateLimiter(per_second=0))

    session = FakeSession([FakeResponse(503)] * 10)
    with pytest.raises(AdapterUnavailable):
        http.get(URL, source="test", session=session, max_retries=4)

    assert len(slept) == 4
    base = 0.5   # settings.http_backoff_base default
    for attempt, delay in enumerate(slept):
        assert 0 <= delay <= base * (2 ** attempt) + 1e-9


# ---------------------------------------------------------------- json parsing


def test_json_body_parses(no_sleep):
    assert http.json_body(FakeResponse(200, {"a": 1}), "t") == {"a": 1}


def test_non_json_body_raises_typed_error():
    with pytest.raises(AdapterBadResponse, match="not JSON"):
        http.json_body(FakeResponse(200, None, text="<html>oops</html>"), "t")


# ---------------------------------------------------------------- rate limiting


def test_rate_limiter_spaces_calls():
    limiter = http.RateLimiter(per_second=50)      # 20ms apart
    start = time.monotonic()
    for _ in range(4):
        limiter.wait("https://host/x")
    elapsed = time.monotonic() - start
    assert elapsed >= 0.055, f"calls were not throttled (took {elapsed:.3f}s)"


def test_rate_limiter_is_per_host():
    """One slow host must not throttle a different one."""
    limiter = http.RateLimiter(per_second=20)
    limiter.wait("https://a/x")
    start = time.monotonic()
    limiter.wait("https://b/x")
    assert time.monotonic() - start < 0.02


def test_rate_limiter_disabled_when_zero():
    limiter = http.RateLimiter(per_second=0)
    assert limiter.wait("https://a/x") == 0.0


# ---------------------------------------------------------------- guard


def test_guard_wraps_untyped_exceptions():
    def explode():
        raise KeyError("unexpected shape")

    with pytest.raises(AdapterUnavailable):
        http.guard(explode, source="t")()


def test_guard_passes_typed_errors_through():
    def fail():
        raise AdapterAuthError("nope", source="t")

    with pytest.raises(AdapterAuthError):
        http.guard(fail, source="t")()
