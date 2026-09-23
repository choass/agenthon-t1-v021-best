"""One HTTP request in a disposable process; credentials travel only over stdin."""

from __future__ import annotations

import email.utils
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

MAX_BODY = 16 * 1024 * 1024


def read_response(response, streaming: bool) -> dict:
    content_type = response.headers.get("Content-Type", "")
    if not streaming or "text/event-stream" not in content_type:
        raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise ValueError("Response too large")
        return json.loads(raw)
    message = {"role": "assistant", "content": ""}
    calls = {}
    usage = {}
    finish_reason = None
    received = 0
    event_lines = []
    done = False

    def event(raw):
        nonlocal usage, finish_reason, done
        if raw.strip() == "[DONE]":
            done = True
            return
        data = json.loads(raw)
        if "error" in data:
            raise ValueError("Stream error")
        if data.get("usage"):
            usage = data["usage"]
        for choice in data.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or choice.get("message") or {}
            for key in ("content", "reasoning_content"):
                if delta.get(key):
                    message[key] = message.get(key, "") + delta[key]
            for call in delta.get("tool_calls") or []:
                index = call.get("index", 0)
                saved = calls.setdefault(
                    index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if call.get("id"):
                    saved["id"] = call["id"]
                for key in ("name", "arguments"):
                    value = call.get("function", {}).get(key)
                    if value:
                        saved["function"][key] += (
                            value if isinstance(value, str) else json.dumps(value)
                        )
            finish_reason = choice.get("finish_reason") or finish_reason

    while not done:
        line = response.readline(MAX_BODY + 1)
        if not line:
            if event_lines:
                event("\n".join(event_lines))
            break
        received += len(line)
        if received > MAX_BODY:
            raise ValueError("Stream too large")
        line = line.decode().rstrip("\r\n")
        if not line and event_lines:
            event("\n".join(event_lines))
            event_lines = []
        elif line.startswith("data:"):
            event_lines.append(line[5:].lstrip())
    if finish_reason is None:
        raise ValueError("Incomplete stream")
    if calls:
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }


def exchange(spec: dict, timeout: float) -> dict:
    """Enforce a wall-clock limit even if the server drips bytes indefinitely."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "t1_agent.transport"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        raw, _ = proc.communicate(json.dumps(spec).encode(), timeout=max(0.01, timeout))
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return {"error": "transport", "detail": "RequestDeadlineExceeded"}
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if proc.returncode:
        return {"error": "transport", "detail": "TransportProcessError"}
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        return {"error": "protocol", "detail": "InvalidTransportResponse"}


def main():
    spec = json.load(sys.stdin)
    try:
        request = urllib.request.Request(
            spec["url"],
            data=json.dumps(spec["payload"]).encode(),
            headers=spec["headers"],
            method="POST",
        )
        with urllib.request.build_opener().open(
            request, timeout=spec["timeout"]
        ) as response:
            result = {"data": read_response(response, spec["payload"]["stream"])}
    except urllib.error.HTTPError as exc:
        retry_after = 0.0
        value = exc.headers.get("Retry-After", "") if exc.headers else ""
        try:
            retry_after = float(value)
        except ValueError:
            try:
                retry_after = (
                    email.utils.parsedate_to_datetime(value).timestamp() - time.time()
                )
            except (ValueError, TypeError, OverflowError):
                pass
        # Inspect compatibility errors privately; never forward an upstream body.
        body = exc.read(65536).decode(errors="replace").lower()
        result = {
            "error": "http",
            "status": exc.code,
            "retry_after": max(0, min(60, retry_after)),
            "unsupported_stream": exc.code in (400, 422) and "stream" in body,
        }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        result = {"error": "transport", "detail": type(exc).__name__}
    except (ValueError, KeyError, TypeError, UnicodeError):
        result = {"error": "protocol", "detail": "MalformedOrIncompleteResponse"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
