"""Offline hardening.

The agent must run with no internet access. The SDK will happily probe
observability endpoints when given a key in the environment, and the OpenAI client
retries before failing. Rather than relying on the surrounding environment being
clean, this module explicitly neutralises those paths.

Call :func:`enforce_offline` once at process start (the CLI does this automatically).
"""

from __future__ import annotations

import os

# Environment variables that would make the SDK attempt network telemetry.
_TELEMETRY_KEYS = (
    "LMNR_PROJECT_API_KEY",
    "LMNR_BASE_URL",
    "OTEL_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OPENHANDS_TELEMETRY_POSTHOG_API_KEY",
)

_ENFORCED = False


def is_offline_enforced() -> bool:
    return _ENFORCED


def enforce_offline() -> list[str]:
    """Remove telemetry configuration so the process makes no outbound calls.

    Returns the names of the environment variables that were cleared.
    """
    global _ENFORCED
    cleared: list[str] = []
    for key in _TELEMETRY_KEYS:
        if os.environ.pop(key, None) is not None:
            cleared.append(key)

    # Suppress the SDK startup banner and analytics noise on a headless box.
    os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
    # Keep model metadata lookups from reaching out to a pricing service.
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

    _ENFORCED = True
    return cleared


def assert_local_model(model: str, base_url: str | None, ollama_base_url: str | None) -> None:
    """Fail fast if the configured model would leave the machine.

    Offline operation only makes sense against a local runtime such as Ollama.
    """
    if model.startswith(("ollama/", "ollama_chat/")):
        return
    if base_url and _is_loopback(base_url):
        return
    if ollama_base_url and model and "/" not in model:
        return
    raise RuntimeError(
        f"model {model!r} is not a local model. Offline mode requires an Ollama "
        "model (e.g. 'qwen2.5:3b-instruct') or an OpenAI-compatible base_url on "
        "localhost. Set --model or LLM_MODEL accordingly."
    )


def _is_loopback(url: str) -> bool:
    lowered = url.lower()
    return any(
        host in lowered
        for host in ("127.0.0.1", "localhost", "0.0.0.0", "::1", "host.docker.internal")
    )
