"""Thin subprocess wrapper around the Pi CLI.

Adds:
- Retry on transient subprocess / connection errors (3 attempts, 1s/2s/4s).
- Clean separation: validation/JOIN failures are NOT retried — they propagate
  as InfraError immediately.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess

from retry import with_retry

log = logging.getLogger("refine.pi_runner")


class InfraError(Exception):
    """Auth, clone, Pi invocation, or non-JSON output failures."""


_RETRYABLE: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    OSError,
    subprocess.TimeoutExpired,
)


def _timeout_seconds() -> int:
    # ponytail: 600s keeps the test deterministic when PI_TIMEOUT_SECONDS
    # isn't set in the harness. Production users explicitly set it via .env.
    return int(os.environ.get("PI_TIMEOUT_SECONDS", "600"))


def run(prompt: str, model: str) -> dict:
    """Invoke Pi. Returns parsed findings dict."""
    cmd = [
        "pi",
        "-p",
        prompt,
        "--model",
        model,
    ]
    log.debug("pi cmd=%r prompt_preview=%r", cmd, prompt[:500])

    def _invoke():
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_timeout_seconds()
            )
        except FileNotFoundError as e:
            raise InfraError(f"Pi CLI not on PATH: {e}")
        if r.returncode != 0:
            raise InfraError(
                f"Pi CLI exit {r.returncode}: {(r.stderr or r.stdout).strip()[:500]}"
            )
        return r.stdout

    stdout = with_retry(_invoke, retryable=_RETRYABLE)
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        raise InfraError(
            f"Pi output is not valid JSON: {e}; first 500 chars: {stdout[:500]}"
        )
