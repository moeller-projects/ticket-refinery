"""retry.with_retry: transient retries, non-retryable passthrough, delays."""
import time
from unittest.mock import patch

import pytest

import retry as retry_mod


def _raise(exc):
    def _fn():
        raise exc
    return _fn


def test_returns_value_on_first_success():
    assert retry_mod.with_retry(lambda: 42) == 42


def test_retries_only_transient_then_succeeds():
    calls = []

    def _fn():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("nope")
        return "ok"

    delays = (0.0, 0.0, 0.0)  # zero-delay so test is fast
    out = retry_mod.with_retry(_fn, delays=delays)
    assert out == "ok"
    assert len(calls) == 3


def test_raises_last_exception_when_all_attempts_fail():
    calls = []

    def _fn():
        calls.append(1)
        raise TimeoutError(f"#{len(calls)}")

    with pytest.raises(TimeoutError) as ei:
        retry_mod.with_retry(_fn, delays=(0.0, 0.0, 0.0))
    assert "3" in str(ei.value)
    assert len(calls) == 3  # exactly MAX_ATTEMPTS


def test_does_not_retry_non_retryable_exception():
    calls = []
    def _fn():
        calls.append(1)
        raise ValueError("permanent")
    with pytest.raises(ValueError):
        retry_mod.with_retry(_fn, retryable=(ConnectionError,))
    assert len(calls) == 1


def test_uses_configured_delays_between_attempts(monkeypatch):
    """We don't sleep real seconds in the test — measure clock side-effects via patch."""
    sleeps = []
    monkeypatch.setattr(retry_mod.time, "sleep", lambda s: sleeps.append(s))

    calls = []
    def _fn():
        calls.append(1)
        raise ConnectionError()
    with pytest.raises(ConnectionError):
        retry_mod.with_retry(_fn, delays=(1.0, 2.0, 4.0))
    # 3 attempts → 2 sleeps between them (last attempt doesn't retry).
    assert sleeps == [1.0, 2.0]


def test_on_retry_callback_fires_for_each_failure():
    seen = []
    calls = []
    def _fn():
        calls.append(1)
        raise OSError(f"{len(calls)}")
    with pytest.raises(OSError):
        retry_mod.with_retry(
            _fn,
            delays=(0.0, 0.0, 0.0),
            on_retry=lambda attempt, exc: seen.append((attempt, type(exc).__name__)),
        )
    assert len(seen) == 2  # attempt 1, 2 fire callback; attempt 3 re-raises
    assert [a for a, _ in seen] == [1, 2]
    assert all(n == "OSError" for _, n in seen)


def test_default_policy_is_3_attempts_with_exponential_backoff():
    assert retry_mod.MAX_ATTEMPTS == 3
    assert retry_mod.DEFAULT_DELAYS == (1.0, 2.0, 4.0)


def test_default_retryable_excludes_auth_and_validation():
    # These should NOT be in the default tuple.
    for cls in (ValueError, KeyError, AssertionError, PermissionError):
        assert cls not in retry_mod.DEFAULT_RETRYABLE, cls
