"""Automatic execution memory. No extra inference and no external task knowledge."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path


class ExecutionMemory:
    def __init__(self, work: Path):
        self.work = work
        self.files: list[dict] = []
        self.actions: list[dict] = []
        self.last_error = ""
        self.last_error_action = ""
        self.idle_steps = 0
        self._signature = None
        self.write_prompts = 0
        self.repair_prompts = 0
        self.source_tail = {}

    def refresh(self, outputs: Path):
        files = []
        for root, prefix in [(self.work, "/workspace"), (outputs, "/app/output")]:
            for path in sorted(root.iterdir()):
                if path.name.startswith(".") or path.name == "harness_checks.py":
                    continue
                if path.suffix not in {".py", ".md", ".json", ".csv"}:
                    continue
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as f:
                        info = os.fstat(f.fileno())
                        if not stat.S_ISREG(info.st_mode):
                            continue
                        content = f.read(256_000)
                        digest = hashlib.sha256(content).hexdigest()[:12]
                    if prefix == "/workspace" and path.suffix == ".py":
                        self.source_tail[prefix + "/" + path.name] = "\n".join(
                            content.decode(errors="replace").splitlines()[-25:]
                        )[-2400:]
                    files.append(
                        {
                            "path": prefix + "/" + path.name,
                            "bytes": info.st_size,
                            "digest": digest,
                        }
                    )
                except OSError:
                    continue
        signature = json.dumps(files, sort_keys=True)
        self.idle_steps = self.idle_steps + 1 if signature == self._signature else 0
        self._signature, self.files = signature, files

    def observe(self, step: int, name: str, args: dict, result: dict):
        label = str(args.get("path") or args.get("command") or name)
        item = {
            "step": step,
            "tool": name,
            "action": label[:200],
            "returncode": result.get("returncode"),
            "timed_out": result.get("timed_out", False),
        }
        text = str(result.get("output", "")).strip()
        if result.get("returncode", 0) != 0 or result.get("error"):
            self.last_error = (str(result.get("error", "")) + text)[-1600:]
            self.last_error_action = label
            item["error"] = self.last_error[-650:]
        elif name == "verify" or (name == "bash" and label == self.last_error_action):
            self.last_error = self.last_error_action = ""
        elif name in {"write_file", "edit_file"}:
            item["result"] = text[-500:]
        self.actions = (self.actions + [item])[-6:]

    def state(self) -> dict:
        return {
            "files": self.files[:28],
            "recent_actions": self.actions,
            "last_execution_error": self.last_error,
            "rounds_without_file_change": self.idle_steps,
        }

    def needs_first_source(self, step: int, has_outputs: bool) -> bool:
        return (
            step >= 10
            and not has_outputs
            and self.idle_steps >= 5
            and not any(
                f["path"].startswith("/workspace/") and f["path"].endswith(".py")
                for f in self.files
            )
            and self.write_prompts < 8
        )

    def stalled_source(self, step: int, has_outputs: bool) -> str | None:
        if step < 16 or has_outputs or self.idle_steps < 8 or self.repair_prompts >= 6:
            return None
        sources = [f["path"] for f in self.files if f["path"] in self.source_tail]
        return (
            "/workspace/solve.py"
            if "/workspace/solve.py" in sources
            else next(iter(sources), None)
        )
