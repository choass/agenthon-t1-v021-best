from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def load_env(path: Path | None) -> dict[str, str]:
    """Read only an explicitly selected env file; never execute shell expansions."""
    if path is None:
        return {}
    result = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key.strip() not in {"T1_BASE_URL", "T1_MODEL", "T1_API_KEY"}:
            raise ValueError("Env file contains an unsupported key or malformed line")
        result[key.strip()] = value.strip().strip("\"'")
    return result


@dataclass
class Config:
    base_url: str
    model: str
    api_key: str = field(default="", repr=False)
    official: bool = False
    thinking: str = "disabled"
    max_steps: int = 250
    request_budget: int = 25
    max_output_tokens: int = 4000
    response_tokens: int = 4000
    context_bytes: int = 36_000
    feedback_chars: int = 4000
    command_timeout: float = 300
    request_timeout: float = 600
    request_retries: int = 2
    streaming: str = "auto"
    temperature: float = 0.0
    seed: int = 42

    def __post_init__(self):
        cfg = self
        if cfg.thinking not in {"auto", "enabled", "disabled"}:
            raise ValueError("thinking must be auto, enabled, or disabled")
        if cfg.streaming not in {"auto", "on", "off"}:
            raise ValueError("streaming must be auto, on, or off")
        if not 0 <= cfg.request_retries <= 5:
            raise ValueError("request_retries must be between 0 and 5")
        for name in (
            "max_steps",
            "request_budget",
            "max_output_tokens",
            "response_tokens",
            "context_bytes",
            "feedback_chars",
            "command_timeout",
            "request_timeout",
        ):
            if not math.isfinite(getattr(cfg, name)) or getattr(cfg, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if cfg.request_budget > 25:
            raise ValueError("request_budget cannot exceed 25 admitted requests")
        if cfg.max_output_tokens > 4000 or cfg.response_tokens > 4000:
            raise ValueError("output tokens cannot exceed 4000 per request")

    @classmethod
    def from_env(cls, env_file: Path | None = None, **kwargs) -> Config:
        # An official endpoint always wins; no local credential is loaded in this mode.
        official = bool(os.environ.get("MODEL_ENDPOINT"))
        if official:
            endpoint = os.environ["MODEL_ENDPOINT"]
            model = os.environ.get("MODEL_NAME", "")
            key = os.environ.get("MODEL_TOKEN", "")
        else:
            if os.environ.get("QFBENCH_NETWORK") == "restricted":
                raise ValueError(
                    "Restricted evaluation requires MODEL_ENDPOINT and MODEL_NAME"
                )
            values = {**load_env(env_file), **os.environ}
            endpoint = values.get("T1_BASE_URL", "")
            model = values.get("T1_MODEL", "")
            key = values.get("T1_API_KEY", "")
        if not endpoint or not model:
            raise ValueError(
                "Set MODEL_ENDPOINT/MODEL_NAME, or provide --env-file with T1_* settings"
            )
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.query
        ):
            raise ValueError(
                "Model endpoint must be an HTTP(S) URL without credentials or query"
            )
        cfg = cls(endpoint.rstrip("/"), model, key, official=official, **kwargs)
        return cfg

    def redact(self, value: str) -> str:
        return value.replace(self.api_key, "[REDACTED]") if self.api_key else value
