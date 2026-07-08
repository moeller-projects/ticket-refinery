"""Retry with exponential backoff for transient infrastructure failures.

Why this is separate:
- Network, clone, and CLI subprocess failures are safe to repeat.
- Schema validation, malformed JSON, unresolved sourceRefs, and business
  validation failures are NOT — retrying them just amplifies the noise.
- Centralising the policy means all retries look the same and we don't
  duplicate the loop. Per-project rule: 3 attempts, 1s/2s/4s backoff.
"""
from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")

MAX_ATTEMPTS = 3
DEFAULT_DELAYS = (1.0, 2.0, 4.0)  # ponytail: spec-mandated; 4s is the post-final backoff ceiling.

# Transient exceptions safe to repeat. Auth/permission/schema errors are NOT here.
DEFAULT_RETRYABLE: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    OSError,
)


def with_retry(
    fn: Callable[[], T],
    *,
    retryable: tuple[type[BaseException], ...] = DEFAULT_RETRYABLE,
    delays: tuple[float, ...] = DEFAULT_DELAYS,
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    """Run `fn()` up to 3 times. Re-raise last exception on final failure.

    Delays[i] is the sleep after attempt i+1 fails (i=0,1). With
    delays=(1, 2, 4) and 3 attempts total: 1s after first failure,
    2s after second, raise on third. The 4s value is a budget ceiling
    — kept in the spec so future code with more retries still uses it.
    """
    last_err: BaseException | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            return fn()
        except retryable as e:
            last_err = e
            # Sleep before next attempt, except after the final one (we'll re-raise).
            if attempt < MAX_ATTEMPTS - 1 and attempt < len(delays):
                if on_retry is not None:
                    on_retry(attempt + 1, e)
                time.sleep(delays[attempt])
    assert last_err is not None  # at least one attempt ran
    raise last_err
