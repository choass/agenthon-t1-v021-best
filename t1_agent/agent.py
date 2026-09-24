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
from .file_tools import FILE_TOOLS, file_command
from .guidance import select_guide
from .memory import ExecutionMemory
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
            except json.JSONDecodeError as exc:
                args = {
                    "_error": f"Tool JSON is incomplete or malformed at line {exc.lineno}, column {exc.colno}: {exc.msg}. No action was executed. Split long source into smaller modules or make an incremental edit; send complete JSON."
                }
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
            "read_file",
            "write_file",
            "edit_file",
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
    staged = task.resolve() if mode == "container" else artifacts / "input"
    if mode != "container":
        stage_task(task, staged)
    sandbox = Sandbox(
        staged, output, artifacts / "workspace", mode, extra_binds=input_mounts(staged)
    )
    shutil.copyfile(
        Path(__file__).with_name("audit_checks.py"), sandbox.work / "harness_checks.py"
    )
    guides = Path(__file__).with_name("guides")
    if guides.is_dir():
        shutil.copytree(guides, sandbox.work / "finance_guides")
    if (staged / "environment").is_dir():
        (sandbox.work / "environment").symlink_to(
            "/input/environment" if mode == "local" else str(staged / "environment"),
            target_is_directory=True,
        )
    memory = ExecutionMemory(sandbox.work)
    action_logs = artifacts / "action_logs"
    action_logs.mkdir(parents=True)
    sandbox.extra_binds.append((action_logs, "/harness_logs"))
    workflow = Workflow()
    model = client or ChatClient(config)
    system = SYSTEM
    implement_prompt, review_prompt = IMPLEMENT, REVIEW
    instruction = (staged / "instruction.md").read_text()
    guide = select_guide(instruction)
    if guide:
        system += (
            "\nOne relevant generic guide is loaded below. The task's explicit requirements override it.\n"
            + guide[1]
        )
    instruction = (staged / "instruction.md").read_text()
    if mode == "container":
        mapping = {"/workspace": str(sandbox.work), "/input": str(staged),
                   "/app/output": str(output), "/output": str(output)}
        for source, destination in input_mounts(staged):
            mapping[destination] = str(source)
        mapping.setdefault('/app/data', str(staged / 'environment/data'))
        mapping['/harness_logs'] = str(artifacts / 'action_logs')
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
            "content": f"Task:\n{instruction}\n\nInput file inventory (relative to /input):\n{list_files(staged)}\n\nRead supplied data at /input/environment/data or /app/data. The relative path environment/data works from /workspace; /app/environment/data is also an alias. Use these known paths rather than searching the filesystem.\nTime limit: {limit:g} seconds. Execute the solution and create every deliverable.",
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
            "loaded_guide": guide[0] if guide else None,
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
            memory.refresh(output)
            workflow.execution_memory = memory.state()
            has_outputs = any(
                p.is_file() and not p.is_symlink() for p in output.iterdir()
            )
            required_source_tool = None
            if isinstance(model, ChatClient):
                model.tool_choice = "auto"
                if memory.needs_first_source(step, has_outputs):
                    required_source_tool = "write_file"
                    model.tool_choice = {
                        "type": "function",
                        "function": {"name": "write_file"},
                    }
                    memory.write_prompts += 1
                    log(
                        "progress_guard",
                        {"step": steps, "action": "write_first_source"},
                    )
                    workflow.execution_memory["required_next_action"] = (
                        "Write a runnable Python source file with write_file now. "
                        "Use /workspace/solve.py (or the task-requested .py output). "
                        "Do further data discovery in code. Reading more files or writing notes "
                        "will be rejected until source exists; preserve all prior findings."
                    )
                elif stalled := memory.stalled_source(step, has_outputs):
                    required_source_tool = "edit_file"
                    model.tool_choice = {
                        "type": "function",
                        "function": {"name": "edit_file"},
                    }
                    memory.repair_prompts += 1
                    workflow.execution_memory["required_next_action"] = (
                        f"No output or source progress in {memory.idle_steps} rounds. "
                        f"Use edit_file now to complete or repair {stalled}; "
                        "then execute it. Further input inspection is not the next action. "
                        "The following is the actual source tail, usable for an exact edit.\n"
                        + memory.source_tail[stalled]
                    )
                    log(
                        "progress_guard",
                        {"step": steps, "action": "complete_stalled_source"},
                    )
            consumed = max(
                elapsed / limit,
                model.usage.calls / config.request_budget,
                step / config.max_steps,
            )
            pending_execution = (
                isinstance(model, ChatClient) and model.execute_after_truncated_thinking
            )
            if workflow.should_review(consumed) and not pending_execution:
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
                has_outputs,
            ):
                workflow.begin_implementation(elapsed)
                # Preserve recently discovered input paths and actual execution failures.
                messages[0] = {"role": "system", "content": system + implement_prompt}
                log("phase", workflow.transitions[-1])
            messages[2] = workflow.state_message(
                elapsed=elapsed,
                limit=limit,
                steps=step,
                max_steps=config.max_steps,
                input_used=model.usage.input_tokens,
                input_cap=None,
                output_used=model.usage.output_tokens,
                output_cap=config.response_tokens,
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
                actions = [(name, {'_error': 'Response truncated; resend a smaller complete command. Nothing executed.'}, call_id)
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
            for action_index, (name, args, call_id) in enumerate(actions):
                if "_error" in args:
                    observation = {"error": args["_error"]}
                elif required_source_tool and (
                    name != required_source_tool
                    or not str(args.get("path", "")).endswith(".py")
                ):
                    observation = {
                        "error": f"Exploration action not executed: use {required_source_tool} on a .py solution now; the task and previous findings are already available."
                    }
                    log("progress_guard_rejected", {"step": steps, "tool": name})
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
                elif name in {x[0] for x in FILE_TOOLS}:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Task deadline reached")
                    try:
                        observation = sandbox.run(
                            file_command(name, args), min(30, remaining)
                        )
                        if (
                            name == required_source_tool
                            and observation.get("returncode") == 0
                            and str(args.get("path", "")).endswith(".py")
                        ):
                            required_source_tool = None
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
                    command_log = action_logs / f"{steps:03d}-{action_index}.sh"
                    command_log.write_text(args["command"])
                    output_log = command_log.with_suffix(".txt")
                    output_log.write_text(str(observation.get("output", "")))
                    log_prefix = (
                        "/harness_logs" if mode == "local" else str(action_logs)
                    )
                    observation["action_log"] = f"{log_prefix}/{command_log.name}"
                    observation["output_log"] = f"{log_prefix}/{output_log.name}"
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
                memory.observe(steps, name, args, observation)
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
        and model.usage.calls <= config.request_budget
    ):
        status = "submitted"
    final_inspection = inspect_outputs(output, workflow.outputs)
    if sum(v['bytes'] for v in final_inspection['files'].values()) > 64 * 1024 * 1024:
        status, reason = 'resource_limit', 'Output tree exceeds 64 MiB; not accepted as a submission'
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
