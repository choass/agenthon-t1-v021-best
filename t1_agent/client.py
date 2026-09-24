from __future__ import annotations

import time
from dataclasses import dataclass

from .config import Config
from .context import encoded_size, trim_history
from .file_tools import definitions
from .transport import exchange


class BudgetExceeded(RuntimeError):
    pass


class BudgetUnavailable(BudgetExceeded):
    """No room for a further request; already-created outputs remain admissible."""


class ModelError(RuntimeError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    estimated_calls: int = 0


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "checkpoint",
            "description": "Save the complete output contract, exact conventions, independent check plan and compact progress. Call early and update after milestones; this survives compaction and review.",
            "parameters": {
                "type": "object",
                "properties": {
                    "outputs": {"type": "array", "items": {"type": "string"}},
                    "conventions": {"type": "string"},
                    "checks": {"type": "array", "items": {"type": "string"}},
                    "progress": {"type": "string"},
                },
                "required": ["outputs", "conventions", "checks", "progress"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run one bash command in the local offline workspace. Files persist; shell state does not.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify",
            "description": "Execute independent task-derived self-checks against generated files. A successful run is tied to output hashes; required before finish. This is not the official grader.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "coverage": {"type": "string"},
                },
                "required": ["command", "coverage"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Finish after executing the solution and validating all requested output files.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        },
    },
]
TOOLS += definitions()


class ChatClient:
    def __init__(self, config: Config):
        self.config = config
        self.usage = Usage()
        self.last_finish_reason = None
        self.streaming = config.streaming == "on" or (
            config.streaming == "auto" and not config.official
        )
        self.compactions = 0
        self.retries = 0
        self.events = []
        self.on_event = None
        self.tool_choice = "auto"
        self.execute_after_truncated_thinking = False

    def _event(self, kind, **fields):
        event = {"kind": kind, **fields}
        self.events.append(event)
        if self.on_event:
            self.on_event(event)

    def _exchange(self, spec, timeout):
        return exchange(spec, timeout)

    def offered_tools(self):
        if isinstance(self.tool_choice, dict):
            name = self.tool_choice.get("function", {}).get("name")
            selected = [t for t in TOOLS if t["function"]["name"] == name]
            if selected:
                return selected
        return TOOLS

    def reservation(self, messages):
        return encoded_size({"messages": messages, "tools": TOOLS}) + 2048

    def prepare_messages(self, messages):
        # Current House accounting has no cumulative input-token allowance.
        # The context target is only a local transport safeguard.
        target = self.config.context_bytes
        before = encoded_size(messages)
        compacted = trim_history(messages, target)
        if compacted != messages:
            messages[:] = compacted
            self.compactions += 1
            self._event(
                "context_compacted",
                before_bytes=before,
                after_bytes=encoded_size(messages),
                input_tokens_diagnostic=self.usage.input_tokens,
            )
        return self.reservation(messages)


    def _charge(self, data, reservation, allowance):
        usage = data.get("usage") or {}
        known = all(
            isinstance(usage.get(k), int) and usage[k] >= 0
            for k in ("prompt_tokens", "completion_tokens")
        )
        self.usage.input_tokens += usage["prompt_tokens"] if known else reservation
        self.usage.output_tokens += usage["completion_tokens"] if known else allowance
        self.usage.estimated_calls += int(not known)


    def complete(self, messages: list[dict], deadline: float) -> dict:
        cfg = self.config
        for attempt in range(cfg.request_retries + 1):
            if self.usage.calls >= cfg.request_budget:
                raise BudgetUnavailable(
                    "Official House request budget exhausted (25 admitted requests)"
                )
            reservation = self.prepare_messages(messages)
            allowance = min(cfg.response_tokens, cfg.max_output_tokens, 4000)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Task deadline reached before model call")
            payload = {
                "model": cfg.model,
                "messages": messages,
                "tools": self.offered_tools(),
                "tool_choice": getattr(self, "tool_choice", "auto"),
                "temperature": cfg.temperature,
                "seed": cfg.seed,
                "max_tokens": allowance,
                "stream": self.streaming,
            }
            if self.streaming:
                payload["stream_options"] = {"include_usage": True}
            thinking = cfg.thinking
            if thinking == "auto" and cfg.model.lower().startswith("glm-5.2"):
                thinking = "disabled"
            if not (cfg.official or "nemotron" in cfg.model.lower()) and thinking in {"enabled", "disabled"}:
                payload["thinking"] = {"type": thinking}
            elif cfg.official or "nemotron" in cfg.model.lower():
                payload["chat_template_kwargs"] = {
                    "enable_thinking": thinking != "disabled"
                }
            headers = {"Content-Type": "application/json"}
            if cfg.api_key:
                headers["Authorization"] = "Bearer " + cfg.api_key
            endpoint = cfg.base_url.rstrip("/")
            if endpoint.endswith("/chat/completions"):
                url = endpoint
            elif endpoint.endswith("/v1"):
                url = endpoint + "/chat/completions"
            else:
                url = endpoint + "/v1/chat/completions"
            # Leave time for the configured retry attempts and for local repairs. The
            # complete() deadline remains the original task deadline, not this attempt's.
            limit = min(cfg.request_timeout, remaining, max(5, remaining * 0.4))
            self.usage.calls += 1
            self._event(
                "request_start",
                call=self.usage.calls,
                attempt=attempt + 1,
                streaming=self.streaming,
                timeout_sec=limit,
                input_reserve=reservation,
                output_allowance=allowance,
                thinking=payload.get("thinking"),
                chat_template_kwargs=payload.get("chat_template_kwargs"),
            )
            start = time.monotonic()
            reply = self._exchange(
                {"url": url, "headers": headers, "payload": payload, "timeout": limit},
                limit,
            )
            data = reply.get("data")
            self._charge(data if isinstance(data, dict) else {}, reservation, allowance)
            if isinstance(data, dict):
                try:
                    choice = data["choices"][0]
                    message = choice["message"]
                    if not isinstance(message, dict):
                        raise TypeError
                    self.last_finish_reason = choice.get("finish_reason")
                    self._event(
                        "request_complete",
                        call=self.usage.calls,
                        elapsed_sec=round(time.monotonic() - start, 3),
                        finish_reason=self.last_finish_reason,
                    )
                    return {
                        k: v
                        for k, v in message.items()
                        if k in {"role", "content", "tool_calls", "reasoning_content"}
                    }
                except (KeyError, IndexError, TypeError):
                    reply = {
                        "error": "protocol",
                        "detail": "Model response has no assistant message",
                    }
            status = reply.get("status")
            compatible_fallback = bool(
                reply.get("unsupported_stream")
                and self.streaming
                and cfg.streaming == "auto"
            )
            retryable = (
                reply.get("error") in {"transport", "protocol"}
                or status in {408, 429, 500, 502, 503, 504}
                or compatible_fallback
            )
            detail = (
                f"Model API HTTP {status}"
                if status
                else "Model request failed: " + reply.get("detail", "UnknownResponse")
            )
            self._event(
                "request_failed",
                call=self.usage.calls,
                error=detail,
                elapsed_sec=round(time.monotonic() - start, 3),
                retryable=retryable,
            )
            if not retryable or attempt >= cfg.request_retries:
                raise ModelError(detail + f"; stopped after {attempt + 1} attempt(s)")
            if compatible_fallback:
                self.streaming = False
            delay = max(min(2**attempt, 8), min(60, reply.get("retry_after", 0)))
            if deadline - time.monotonic() <= delay + 1:
                raise ModelError(detail + "; insufficient time for retry")
            self.retries += 1
            self._event("request_retry", delay_sec=delay, next_attempt=attempt + 2)
            time.sleep(delay)
        raise ModelError("Model retries exhausted")

