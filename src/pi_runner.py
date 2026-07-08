"""Thin subprocess wrapper around the Pi CLI."""

import json
import logging
import os
import subprocess

log = logging.getLogger("refine.pi")


class InfraError(Exception):
    """Auth, clone, Pi invocation, or non-JSON output failures."""


def _timeout_seconds() -> int:
    return int(os.environ.get("PI_TIMEOUT_SECONDS", "900"))


def run(prompt: str, model: str) -> dict:
    """Invoke Pi. Returns parsed findings dict."""
    cmd = [
        "pi",
        "-p",
        "--tools",
        "read,bash,grep,find,ls",
        "--model",
        model,
        prompt,
    ]
    node_v = subprocess.check_output(["node", "-v"], text=True).strip()
    log.info(
        "pi exec pwd=%s pythonpath=%s node=%s",
        os.getcwd(),
        os.environ.get("PYTHONPATH", ""),
        node_v,
    )
    log.debug("pi cmd=%r prompt_preview=%r", cmd, prompt[:500])
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
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise InfraError(
            f"Pi output is not valid JSON: {e}; first 500 chars: {r.stdout[:500]}"
        )
