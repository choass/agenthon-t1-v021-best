from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .agent import run_agent
from .client import ChatClient
from .config import Config
from .evaluate import DEFAULT_UNITS, evaluate, grade, rescore
from .sandbox import Sandbox


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Agenthon T1 coding harness")
    commands = parser.add_subparsers(dest="verb", required=True)
    for verb in ("solve", "evaluate", "doctor"):
        p = commands.add_parser(verb)
        p.add_argument("--env-file", type=Path)
        p.add_argument("--max-steps", type=int, default=Config.max_steps)
        p.add_argument("--response-tokens", type=int, default=4000)
        p.add_argument(
            "--thinking", choices=["auto", "enabled", "disabled"], default="disabled"
        )
        p.add_argument("--request-timeout", type=float, default=600)
        p.add_argument("--request-retries", type=int, default=2)
        p.add_argument("--streaming", choices=["auto", "on", "off"], default="auto")
        p.add_argument("--context-bytes", type=int, default=36000)
        p.add_argument("--feedback-chars", type=int, default=4000)
        p.add_argument("--command-timeout", type=float, default=300)
        p.add_argument("--request-budget", type=int, default=25)
        p.add_argument("--timeout", type=float)
        if verb == "solve":
            p.add_argument("--task-dir", type=Path, required=True)
            p.add_argument("--out", type=Path, required=True)
            p.add_argument("--artifacts", type=Path)
            p.add_argument(
                "--runtime",
                choices=["local", "container"],
                default="container"
                if os.environ.get("T1_CONTAINER_RUNTIME") == "1"
                else "local",
            )
        if verb == "evaluate":
            p.add_argument("--units-dir", type=Path, default=DEFAULT_UNITS)
            p.add_argument(
                "--task",
                action="append",
                help="Task name; repeat to select a fixed subset. Default: all public tasks.",
            )
            p.add_argument("--jobs", type=int, default=8)
            p.add_argument(
                "--run-dir",
                type=Path,
                default=Path("artifacts")
                / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
            )
            p.add_argument("--scorer-python", type=Path)
    p = commands.add_parser("grade")
    p.add_argument("--task-dir", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--artifacts", type=Path, required=True)
    p.add_argument("--elapsed", type=float, required=True)
    p.add_argument("--scorer-python", type=Path)
    p = commands.add_parser("rescore")
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--units-dir", type=Path, default=DEFAULT_UNITS)
    p.add_argument("--scorer-python", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.verb == "rescore":
            result = rescore(
                args.run_dir, args.out_dir, args.units_dir, args.scorer_python
            )
            print(
                json.dumps(
                    {k: v for k, v in result.items() if k != "results"}, indent=2
                )
            )
            return 0 if result["valid_measurement"] else 2
        if args.verb == "grade":
            result = grade(
                args.task_dir,
                args.out,
                args.artifacts,
                args.elapsed,
                args.scorer_python,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("admissible") else 1
        config = Config.from_env(
            args.env_file,
            max_steps=args.max_steps,
            response_tokens=args.response_tokens,
            request_budget=args.request_budget,
            max_output_tokens=4000,
            request_timeout=args.request_timeout,
            request_retries=args.request_retries,
            streaming=args.streaming,
            context_bytes=args.context_bytes,
            feedback_chars=args.feedback_chars,
            command_timeout=args.command_timeout,
            thinking=args.thinking,
        )
        if args.verb == "doctor":
            with tempfile.TemporaryDirectory(prefix="t1-doctor-") as temp:
                root = Path(temp)
                (root / "input").mkdir()
                check = Sandbox(root / "input", root / "output", root / "work").run(
                    "python -c 'import numpy,pandas,scipy,pyarrow,plotly,lxml,sympy; print(\"runtime-ok\")'",
                    30,
                )
                if check["returncode"]:
                    print(json.dumps({"runtime_ok": False, "detail": check["output"]}))
                    return 1
            client = ChatClient(config)
            response = client.complete(
                [
                    {
                        "role": "user",
                        "content": 'Connectivity check: call finish(summary="connected").',
                    }
                ],
                time.monotonic() + 90,
            )
            result = {
                "runtime_ok": True,
                "model": config.model,
                "endpoint_ok": True,
                "streaming": client.streaming,
                "tools": [
                    x["function"]["name"] for x in response.get("tool_calls", [])
                ],
                "usage": asdict(client.usage),
            }
        elif args.verb == "solve":
            result = run_agent(
                args.task_dir,
                args.out,
                config,
                artifacts=args.artifacts,
                timeout=args.timeout,
                mode=args.runtime,
            )
        else:
            if args.jobs < 1 or args.jobs > 16:
                parser.error("--jobs must be between 1 and 16")
            available = {
                p.name: p
                for p in sorted(args.units_dir.iterdir())
                if (p / "card.toml").exists()
            }
            names = list(dict.fromkeys(args.task)) if args.task else list(available)
            if not names or any(n not in available for n in names):
                parser.error("No matching tasks or an unknown task name")
            result = evaluate(
                [available[n] for n in names],
                args.run_dir,
                config,
                jobs=args.jobs,
                timeout=args.timeout,
                scorer_python=args.scorer_python,
            )
            result = {k: v for k, v in result.items() if k != "results"}
        print(config.redact(json.dumps(result, ensure_ascii=False, indent=2)))
        if args.verb == "solve":
            return result["exit_code"]
        return 0 if result.get("valid_measurement", True) else 2
    except (ValueError, RuntimeError, OSError) as exc:
        # Avoid tracebacks containing API request details; configuration errors contain no values.
        print(json.dumps({"error": type(exc).__name__, "detail": str(exc)}))
        return 2
