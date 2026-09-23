"""Bound conversation size without splitting assistant/tool call groups."""

from __future__ import annotations

import json

from .workflow import STATE_PREFIX


def encoded_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())


def trim_history(messages: list[dict], max_bytes: int) -> list[dict]:
    if encoded_size(messages) <= max_bytes:
        return messages
    pinned = (
        3
        if len(messages) > 2
        and (messages[2].get("content") or "").startswith(STATE_PREFIX)
        else 2
    )
    head = messages[:pinned]
    groups: list[list[dict]] = []
    for message in messages[pinned:]:
        if message["role"] != "tool" or not groups:
            groups.append([])
        groups[-1].append(message)
    note = {
        "role": "user",
        "content": "Older actions were removed to conserve the cumulative token budget. "
        "The pinned harness state preserves your contract and progress. All files persist. "
        "Read only needed sections of NOTES.md or solve.py. Continue existing work, do not restart.",
    }
    kept: list[dict] = []
    for group in reversed(groups):
        candidate = head + [note] + group + kept
        if encoded_size(candidate) > max_bytes:
            break
        kept = group + kept
    candidate = head + [note] + kept
    # Never truncate the task specification just to squeeze in another request.
    return candidate if encoded_size(candidate) <= max_bytes else head


def short_feedback(observation: dict, max_chars: int) -> dict:
    result = dict(observation)
    text = result.get("output", "")
    if len(text) > max_chars:
        result["output"] = (
            text[: max_chars // 2]
            + "\n... feedback shortened; query only needed rows/lines ...\n"
            + text[-max_chars // 2 :]
        )
        result["output_shortened"] = True
    return result
