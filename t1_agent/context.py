"""Bound conversation size without splitting assistant/tool call groups."""

from __future__ import annotations

import json
from copy import deepcopy

from .workflow import STATE_PREFIX


def encoded_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())


def compact_group(group: list[dict], chars: int = 1200) -> list[dict]:
    """Keep tool IDs, filenames, recent errors and action order, eliding large bodies."""
    result = deepcopy(group)
    for message in result:
        content = message.get("content")
        if isinstance(content, str) and len(content) > chars:
            message["content"] = (
                content[: chars // 3]
                + "\n[executed history abbreviated; files and action logs persist]\n"
                + content[-2 * chars // 3 :]
            )
        for call in message.get("tool_calls") or []:
            fn = call.get("function", {})
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except (ValueError, TypeError):
                continue
            if not isinstance(args, dict):
                continue
            for key, value in args.items():
                if isinstance(value, str) and len(value) > chars:
                    args[key] = (
                        value[: chars // 3]
                        + "\n[already executed; body elided from history]\n"
                        + value[-chars // 3 :]
                    )
            fn["arguments"] = json.dumps(args, ensure_ascii=False)
    return result


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
        "content": "Older actions were removed to bound the per-request context size. "
        "The pinned harness state preserves your contract and progress. All files persist. "
        "Read only needed sections of NOTES.md or solve.py. Continue existing work, do not restart.",
    }
    kept: list[dict] = []
    for index, group in enumerate(reversed(groups)):
        # Prefer reducing old tool payloads to losing the entire latest execution.
        if index >= 2:
            group = compact_group(group, 700)
        candidate = head + [note] + group + kept
        if encoded_size(candidate) > max_bytes:
            group = compact_group(group, 900 if index == 0 else 400)
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
