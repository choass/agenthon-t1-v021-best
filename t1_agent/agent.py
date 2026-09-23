from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from .client import BudgetExceeded, BudgetUnavailable, ChatClient, ModelError
from .config import Config
from .context import short_feedback
from .prompts import IMPLEMENT, REVIEW, SYSTEM
from .sandbox import Sandbox
from .task import input_mounts, list_files, stage_task
from .workflow import Workflow, inspect_outputs


def parse_actions(message: dict) -> list[tuple[str, dict, str | None]]:
    calls = message.get("tool_calls") or []
    if calls:
        parsed = []
        for call in calls:
            fn = call.get("function", {})
            try:
                args = json.loads(fn.get("arguments", "{}"))
                if not isinstance(args, dict):
                    raise TypeError
            except (ValueError, TypeError):
                args = {"_error": "Tool arguments must be a JSON object"}
            parsed.append((fn.get("name", ""), args, call.get("id")))
        return parsed
    # Text fallback for compatible providers that respond without native function calls.
    content = message.get("content") or ""
    blocks = re.findall(r"```(?:bash|sh)\s*\n(.*?)```", content, re.DOTALL)
    if len(blocks) == 1:
        return [("bash", {"command": blocks[0]}, None)]
    try:
        data = json.loads(content)
        if isinstance(data, dict) and data.get("action") in {
            "bash",
            "finish",
            "checkpoint",
            "verify",
        }:
            return [(data["action"], data, None)]
    except ValueError:
        pass
    return []


def run_agent(
    task: Path,
    output: Path,
    config: Config,
    *,
    artifacts: Path | None = None,
    timeout: float | None = None,
    mode: str = "local",
    client=None,
) -> dict:
    import tomllib

    start = time.monotonic()
    card = tomllib.loads((task / "card.toml").read_text())
    card_timeout = float(card.get("agent", {}).get("timeout_sec", 1800))
    limit = min(card_timeout, timeout) if timeout is not None else card_timeout
    if limit <= 0:
        raise ValueError("timeout must be positive")
    deadline = start + max(0.1, limit - min(10, limit * 0.05))
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Output directory must be empty to prevent stale results")
    if artifacts is None:
        artifacts = Path(tempfile.mkdtemp(prefix="t1-agent-"))
    artifacts = artifacts.resolve()
    if artifacts == output or output in artifacts.parents:
        raise ValueError("Logs and scratch must be outside the deliverable directory")
    artifacts.mkdir(parents=True, exist_ok=True)
    # Official /input is already participant-only and read-only. Do not copy
    # datasets into the 64 MiB temporary filesystem.
    if mode == "container":
        staged = task.resolve()
    else:
        staged = artifacts / "input"
        stage_task(task, staged)
    sandbox = Sandbox(
        staged, output, artifacts / "workspace", mode, extra_binds=input_mounts(staged)
    )
    shutil.copyfile(
        Path(__file__).with_name("audit_checks.py"), sandbox.work / "harness_checks.py"
    )
    workflow = Workflow()
    model = client or ChatClient(config)
    system = SYSTEM
    instruction = (staged / "instruction.md").read_text()
    implement_prompt, review_prompt = IMPLEMENT, REVIEW
    if mode == "container":
        mapping = {
            "/workspace": str(sandbox.work),
            "/input": str(staged),
            "/app/output": str(output),
            "/output": str(output),
        }
        for source, destination in input_mounts(staged):
            mapping[destination] = str(source)
        mapping.setdefault('/app/data', str(staged / 'environment/data'))
        pattern = '|'.join(re.escape(k) for k in sorted(mapping, key=len, reverse=True))
        def translate(text):
            return re.sub(pattern, lambda m: mapping[m.group()], text)
        system, instruction = translate(system), translate(instruction)
        implement_prompt, review_prompt = translate(IMPLEMENT), translate(REVIEW)
    marker = card.get('contamination', {}).get('canary_guid', '')
    if marker:
        instruction = re.sub(re.escape(marker), '[redacted]', instruction, flags=re.IGNORECASE)
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": f"Task:\n{instruction}\n\nInput file inventory:\n{list_files(staged)}\n\nTime limit: {limit:g} seconds. Execute the solution and create every deliverable.",
        },
    ]
    task_message = messages[1]
    messages.append(
        {"role": "user", "content": ""}
    )  # Pinned, refreshed controller state.
    status, reason, steps = "step_limit", "Maximum model steps reached", 0
    trajectory = artifacts / "trajectory.jsonl"

    def log(kind, payload):
        with trajectory.open("a") as f:
            f.write(
                config.redact(
                    json.dumps(
                        {
                            "kind": kind,
                            "elapsed_sec": round(time.monotonic() - start, 3),
                            **payload,
                        },
                        ensure_ascii=False,
                    )
                )
                + "\n"
            )

    if isinstance(model, ChatClient):
        model.on_event = lambda event: log("client", {"event": event})

    log(
        "start",
        {
            "model": config.model,
            "official_endpoint": config.official,
            "messages": messages,
        },
    )
    try:
        for step in range(config.max_steps):
            steps = step + 1
            if time.monotonic() >= deadline:
                status, reason = "timeout", "Task deadline reached"
                break
            elapsed = time.monotonic() - start
            consumed = max(
                elapsed / limit,
                model.usage.calls / config.request_budget,
                step / config.max_steps,
            )
            if workflow.should_review(consumed):
                workflow.begin_review(
                    "65% resource milestone: preserve time/tokens for audit and repair",
                    elapsed,
                )
                messages[:] = [
                    {"role": "system", "content": system + review_prompt},
                    task_message,
                    {},
                ]
                log("phase", workflow.transitions[-1])
            elif workflow.should_implement(
                consumed,
                step,
                any(p.is_file() and not p.is_symlink() for p in output.iterdir()),
            ):
                workflow.begin_implementation(elapsed)
                messages[:] = [
                    {"role": "system", "content": system + implement_prompt},
                    task_message,
                    {},
                ]
                log("phase", workflow.transitions[-1])
            messages[2] = workflow.state_message(
                elapsed=elapsed,
                limit=limit,
                steps=step,
                max_steps=config.max_steps,
                input_used=model.usage.input_tokens,
                input_cap=None,
                output_used=model.usage.output_tokens,
                output_cap=config.max_output_tokens,
                requests_used=model.usage.calls,
                request_cap=config.request_budget,
            )
            # The client bounds each attempt; retries retain access to the remaining task time.
            message = model.complete(messages, deadline)
            message["role"] = "assistant"
            messages.append(message)
            log(
                "assistant",
                {"step": steps, "message": message, "usage": asdict(model.usage)},
            )
            actions = parse_actions(message)
            if model.last_finish_reason == 'length':
                actions = [(name, {'_error': 'Response was truncated: resend a smaller complete tool command; nothing was executed.'}, call_id)
                           for name, args, call_id in actions]
            if not actions:
                feedback = {
                    "role": "user",
                    "content": "Use checkpoint to save the contract, bash to execute code, verify to run assertions, or finish after verification. Text alone does not solve the task.",
                }
                messages.append(feedback)
                continue
            finished = False
            review_requested = False
            for name, args, call_id in actions:
                if "_error" in args:
                    observation = {"error": args["_error"]}
                elif finished or review_requested:
                    observation = {
                        "error": "Finish ends this action batch; remaining calls were not executed."
                    }
                elif name == "checkpoint":
                    try:
                        observation = workflow.checkpoint(args)
                        (artifacts / "contract.json").write_text(
                            config.redact(
                                json.dumps(args, indent=2, ensure_ascii=False)
                            )
                        )
                    except (ValueError, TypeError) as exc:
                        observation = {"error": str(exc)}
                elif name == "finish":
                    inspection = inspect_outputs(output, workflow.outputs)
                    errors = workflow.finish_errors(inspection)
                    if errors:
                        observation = {
                            "error": "Finish blocked by local delivery checks",
                            "issues": errors,
                        }
                    elif workflow.phase in {"build", "implement"}:
                        review_requested = True
                        observation = {
                            "note": "Build verification passed. Starting independent review of the files within the same budget."
                        }
                    else:
                        status, reason, finished = (
                            "completed",
                            str(args.get("summary", "")),
                            True,
                        )
                        observation = {
                            "files": list(inspection["files"]),
                            "note": "Deliverables submitted; correctness is evaluated independently.",
                        }
                elif (
                    name in {"bash", "verify"}
                    and isinstance(args.get("command"), str)
                    and args["command"].strip()
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Task deadline reached")
                    observation = sandbox.run(
                        args["command"], min(config.command_timeout, remaining)
                    )
                    if name == "verify":
                        inspection = inspect_outputs(output, workflow.outputs)
                        workflow.record_verification(
                            args["command"],
                            str(args.get("coverage", "")),
                            observation,
                            inspection,
                        )
                        observation["delivery_checks"] = {
                            "missing": inspection["missing"],
                            "errors": inspection["errors"],
                        }
                        observation["verification_passed"] = workflow.verification[
                            "passed"
                        ]
                        log("verification", workflow.verification)
                    elif steps % 4 == 0:
                        inspection = inspect_outputs(output, workflow.outputs)
                        observation["delivery_checks"] = {
                            "missing": inspection["missing"],
                            "errors": inspection["errors"][:16],
                        }
                else:
                    observation = {
                        "error": "Unknown tool or missing command. Available: checkpoint, bash, verify, finish."
                    }
                log("observation", {"step": steps, "tool": name, "result": observation})
                feedback = {
                    "role": "tool" if call_id else "user",
                    "content": config.redact(
                        json.dumps(
                            short_feedback(observation, config.feedback_chars),
                            ensure_ascii=False,
                        )
                    )
                    + f"\nTime remaining: {max(0, int(deadline - time.monotonic()))}s; token use: {model.usage.input_tokens} input, {model.usage.output_tokens} output.",
                }
                if call_id:
                    feedback["tool_call_id"] = call_id
                messages.append(feedback)
                workflow.latest_feedback = feedback["content"][-1800:]
            if review_requested:
                workflow.begin_review(
                    "Builder requested finish after successful verification",
                    time.monotonic() - start,
                )
                messages[:] = [
                    {"role": "system", "content": system + review_prompt},
                    task_message,
                    {},
                ]
                log("phase", workflow.transitions[-1])
            if finished:
                break
    except BudgetUnavailable as exc:
        status, reason = "budget_reserve", str(exc)
    except BudgetExceeded as exc:
        status, reason = "budget_exhausted", str(exc)
    except (TimeoutError, ModelError) as exc:
        status, reason = "model_error", config.redact(str(exc))
    except (OSError, RuntimeError) as exc:
        status, reason = "error", config.redact(str(exc))
    # Returning files is submission, not a correctness claim. A late inference failure must
    # not discard an already-written answer. Budget violations and empty output still fail.
    deliverables = [
        p
        for p in output.iterdir()
        if p.is_file()
        and not p.is_symlink()
        and p.name not in {"reward.json", "pytest_report.json"}
    ]
    if (
        status in {"model_error", "step_limit", "timeout", "budget_reserve"}
        and deliverables
        and time.monotonic() - start < card_timeout
    ):
        status = "submitted"
    report = {
        "task": task.name,
        "status": status,
        "exit_code": 0 if status in {"completed", "submitted"} else 1,
        "reason": config.redact(reason),
        "steps": steps,
        "elapsed_sec": round(time.monotonic() - start, 3),
        "timeout_sec": limit,
        "model": config.model,
        "official_endpoint": config.official,
        "usage": asdict(model.usage),
        "finish_reason": getattr(model, "last_finish_reason", None),
        "output_files": sorted(p.name for p in output.iterdir() if p.is_file()),
        "correctness": "not_evaluated",
        "runtime": mode,
        "client_metrics": {
            "retries": getattr(model, "retries", 0),
            "context_compactions": getattr(model, "compactions", 0),
            "streaming": getattr(model, "streaming", False),
        },
        "workflow": asdict(workflow),
        "settings": {k: v for k, v in asdict(config).items() if k != "api_key"},
        "code_sha256": hashlib.sha256(
            b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))
        ).hexdigest(),
    }
    (artifacts / "run.json").write_text(
        config.redact(json.dumps(report, ensure_ascii=False, indent=2))
    )
    return report
