"""Task contracts and verifiable progress; no task IDs or reference answers."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .domain_checks import csv_semantic_issues, semantic_issues

STATE_PREFIX = "[Harness state]\n"


def output_name(value: str) -> str:
    for prefix in ("/app/output/", "/output/"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError("Outputs must be relative to /app/output; no parent traversal")
    if value in {".", "reward.json", "pytest_report.json"}:
        raise ValueError("Not a task deliverable")
    return str(path)


def inspect_outputs(root: Path, required: list[str]) -> dict:
    """Inspect regular files without following untrusted links or executing submitted code."""
    files, errors = {}, []
    root = root.resolve()
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink() or not p.resolve().is_relative_to(root):
            errors.append(f"Unsafe output link: {rel}")
            continue
        mode = p.stat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            errors.append(f"Not a regular output file: {rel}")
            continue
        if rel in {"reward.json", "pytest_report.json"}:
            errors.append(f"Reserved evaluator filename: {rel}")
            continue
        size = p.stat().st_size
        with p.open("rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
        files[rel] = {"bytes": size, "sha256": digest}
        if not size:
            errors.append(f"Empty output: {rel}")
        # Do not assume that nullable CSV columns must be finite. JSON must be valid JSON.
        if p.suffix.lower() == ".json" and size <= 8 * 1024 * 1024:
            try:

                def finite_float(text):
                    value = float(text)
                    if not math.isfinite(value):
                        raise ValueError("non-finite JSON number")
                    return value

                def no_constant(text):
                    raise ValueError("non-standard JSON constant")

                payload = json.loads(
                    p.read_text(), parse_constant=no_constant, parse_float=finite_float
                )
                errors.extend(semantic_issues(payload, rel))
            except (ValueError, UnicodeError) as exc:
                errors.append(f"Invalid JSON in {rel}: {type(exc).__name__}")
        elif p.suffix.lower() == ".csv":
            try:
                errors.extend(f"{rel}: {issue}" for issue in csv_semantic_issues(p))
            except (ValueError, UnicodeError):
                # CSV dialects and encodings differ; task-specific checks handle them.
                pass
    if sum(v["bytes"] for v in files.values()) > 64 * 1024 * 1024:
        errors.append("Output tree exceeds official 64 MiB acceptance limit")
    missing = [
        pat for pat in required if not any(fnmatch.fnmatchcase(f, pat) for f in files)
    ]
    return {"files": files, "missing": missing, "errors": errors}


@dataclass
class Workflow:
    phase: str = "build"
    outputs: list[str] = field(default_factory=list)
    conventions: str = ""
    checks: list[str] = field(default_factory=list)
    progress: str = ""
    verification: dict | None = None
    review_reason: str | None = None
    transitions: list[dict] = field(default_factory=list)
    latest_feedback: str = ""
    execution_memory: dict = field(default_factory=dict)

    def checkpoint(self, args: dict) -> dict:
        outputs = args.get("outputs")
        checks = args.get("checks")
        if not isinstance(outputs, list) or not outputs or len(outputs) > 128:
            raise ValueError("List all required output filenames/patterns (1–128)")
        if not all(isinstance(x, str) for x in outputs):
            raise ValueError("Output filenames must be strings")
        if (
            not isinstance(checks, list)
            or not checks
            or not all(isinstance(x, str) for x in checks)
        ):
            raise ValueError(
                "List independent checks derived from the task, not expected answers"
            )
        conventions, progress = args.get("conventions", ""), args.get("progress", "")
        if not isinstance(conventions, str) or not isinstance(progress, str):
            raise TypeError("Conventions and progress must be strings")
        if len(json.dumps(args, ensure_ascii=False).encode()) > 6500:
            raise ValueError(
                "Checkpoint too long; keep the full contract and progress under 6500 bytes"
            )
        self.outputs = list(dict.fromkeys(output_name(x) for x in outputs))
        self.checks, self.conventions, self.progress = checks, conventions, progress
        self.verification = None
        return {
            "saved": True,
            "outputs": self.outputs,
            "note": "Contract persists through context compaction and independent review.",
        }

    def should_review(self, consumed: float) -> bool:
        return self.phase in {"build", "implement"} and consumed >= 0.65

    def should_implement(self, consumed: float, steps: int, has_outputs: bool) -> bool:
        return (
            self.phase == "build"
            and not has_outputs
            and (consumed >= 0.3 or steps >= 16)
        )

    def begin_implementation(self, elapsed: float):
        self.phase = "implement"
        self.transitions.append(
            {
                "phase": "implement",
                "reason": "Bound exploration: produce a runnable solution before further manual inspection",
                "elapsed_sec": round(elapsed, 3),
            }
        )

    def begin_review(self, reason: str, elapsed: float):
        self.phase, self.review_reason = "review", reason
        self.verification = None
        self.transitions.append(
            {"phase": "review", "reason": reason, "elapsed_sec": round(elapsed, 3)}
        )

    def record_verification(
        self, command: str, coverage: str, observation: dict, inspection: dict
    ):
        self.verification = {
            "phase": self.phase,
            "command": command,
            "coverage": coverage,
            "passed": observation["returncode"] == 0
            and not observation["timed_out"]
            and not inspection["missing"]
            and not inspection["errors"]
            and bool(inspection["files"]),
            "outputs": inspection["files"],
        }

    def finish_errors(self, inspection: dict) -> list[str]:
        errors = list(inspection["errors"])
        if not self.outputs:
            errors.append("Register the complete task contract using checkpoint first")
        if inspection["missing"]:
            errors.append(
                "Missing required outputs: " + ", ".join(inspection["missing"])
            )
        if not inspection["files"]:
            errors.append("No deliverable exists")
        if not self.verification or not self.verification["passed"]:
            errors.append(
                "Run verify(command, coverage) with successful, task-derived assertions"
            )
        elif self.verification["outputs"] != inspection["files"]:
            errors.append(
                "Outputs changed after verification; rerun verify on the final files"
            )
        return errors

    def state_message(
        self,
        *,
        elapsed: float,
        limit: float,
        steps: int,
        max_steps: int,
        input_used: int,
        input_cap: int | None,
        output_used: int,
        output_cap: int,
        requests_used: int = 0,
        request_cap: int = 25,
    ) -> dict:
        consumed = max(
            elapsed / limit,
            requests_used / request_cap,
            steps / max_steps,
        )
        directive = "Implement the full solution and execute it. Register/update checkpoint; keep source and self-tests on disk."
        if not self.outputs:
            directive = "Read the instruction, then register ALL required outputs, exact conventions and independent checks with checkpoint now."
        elif self.phase == "review":
            directive = "Independently audit the implementation against the original task, repair evidenced bugs, rerun outputs and verify. Prioritize missing files."
        elif self.phase == "implement":
            directive = "Write and EXECUTE the full solve.py NOW. Perform further data inspection programmatically inside your solution. Produce all deliverables before additional exploratory queries."
        elif consumed >= 0.4:
            directive = "Create every required deliverable NOW, before further exploration. Reserve the final 35% for independent audit and repairs."
        if consumed >= 0.88:
            directive += " Finalize now: no broad rewrites or long new experiments; execute remaining outputs and focused assertions."
        state = {
            "phase": self.phase,
            "directive": directive,
            "remaining": {
                "seconds": max(0, int(limit - elapsed)),
                "steps": max_steps - steps,
                "requests": max(0, request_cap - requests_used),
            },
            "token_usage_diagnostic_only": {"input": input_used, "output": output_used},
            "per_request_output_cap": output_cap,
            "contract": {
                "outputs": self.outputs,
                "conventions": self.conventions,
                "checks": self.checks,
            },
            "progress": self.progress,
            "verification_passed": bool(
                self.verification and self.verification["passed"]
            ),
            "last_feedback": self.latest_feedback[-600:],
            "execution_memory": self.execution_memory,
        }
        return {
            "role": "user",
            "content": STATE_PREFIX + json.dumps(state, ensure_ascii=False),
        }
