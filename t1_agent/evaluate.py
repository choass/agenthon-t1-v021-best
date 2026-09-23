from __future__ import annotations

import json
import shlex
import shutil
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .agent import run_agent
from .config import Config
from .preparation import prepare_polars
from .sandbox import ROOT, Sandbox
from .task import input_mounts

OFFICIAL = ROOT.parent / "official_baselines"
DEFAULT_UNITS = OFFICIAL / "official/track1-coding-public/units"


def grading_mounts(task: Path) -> list[tuple[Path, str]]:
    """Reproduce trusted official check paths and simple Docker COPY mappings."""
    task = task.resolve()
    mounts = []
    if (task / "checks").is_dir():
        mounts.append((task / "checks", "/tests"))
    return mounts + input_mounts(task)


def pytest_infrastructure_error(report: dict, returncode: int | None) -> str | None:
    """Separate unavailable trusted inputs/dependencies from incorrect deliverables."""
    failures = []
    for collector in report.get("collectors", []):
        if collector.get("outcome") == "failed":
            failures.append(collector.get("longrepr", ""))
    for test in report.get("tests", []):
        for phase in ("setup", "call", "teardown"):
            detail = test.get(phase, {})
            if detail.get("outcome") == "failed":
                failures.append(
                    detail.get("crash", {}).get("message") or detail.get("longrepr", "")
                )
    for failure in failures:
        if "ModuleNotFoundError" in failure or "ImportError" in failure:
            return "pytest_dependency_missing"
        if "FileNotFoundError" in failure and any(
            path in failure for path in ("/tests/", "/input/", "/app/data/")
        ):
            return "pytest_trusted_input_missing"
    # A test module may read a required agent output during collection.
    if returncode not in (None, 0, 1) and (
        not failures
        or not all("FileNotFoundError" in f and "/app/output/" in f for f in failures)
    ):
        return "pytest_collection_or_runtime_error"
    return None


def grade(
    task: Path,
    output: Path,
    artifacts: Path,
    elapsed: float,
    scorer_python: Path | None = None,
) -> dict:
    """Run the unmodified official gate chain in an independent process, after the solve."""
    python = scorer_python or OFFICIAL / ".venv-agenthon/bin/python"
    if not python.exists():
        raise RuntimeError("Official scorer Python is missing; pass --scorer-python")
    started = time.monotonic()
    card = tomllib.loads((task / "card.toml").read_text())
    verifier_limit = float(card.get("verifier", {}).get("timeout_sec", 900))
    preparation = None
    if task.name == "t1-polars-api-migration":
        preparation = prepare_polars(
            task, output, artifacts, max(0.1, verifier_limit - 10)
        )
        if preparation.get("grader_error"):
            return preparation
        if not preparation.get("grading_output"):
            return {
                "admissible": False,
                "score": None,
                "artifact_preparation": preparation,
            }
        output = Path(preparation["grading_output"])
    work = artifacts / "grader"
    work.mkdir(parents=True, exist_ok=True)
    if output.resolve() == work.resolve() or output.resolve() in work.resolve().parents:
        raise ValueError("Grader artifacts must be outside the deliverable directory")
    for filename in ("verdict.json", "pytest_report.json", "grader.log"):
        (work / filename).unlink(missing_ok=True)
    if preparation:
        log_dir = work / ".rootfs/logs/verifier"
        log_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(preparation["log"], log_dir / "test_gen.log")
    # This is an adapter around the official factory, not a replacement scoring implementation.
    script = """import json, os, tomllib
from dataclasses import asdict
from pathlib import Path
from qfbench2_track_coding.scoring import build_verifier
from qfbench2_common.leakage import scan_tree
from qfbench2_common.failure_labels import FailureLabel
card = tomllib.loads(Path('/input/card.toml').read_text())
os.environ['PYTEST_ADDOPTS'] = '--json-report --json-report-file=/workspace/pytest_report.json'
ctx = {'unit_dir': '/input', 'output_dir': '/app/output',
       'agent_timeout_sec': card['agent']['timeout_sec'],
       'elapsed_sec': ELAPSED, 'network': card['environment']['network'],
       'canary_registry': set()}
try:
    # T1 still calls scan_canary, withdrawn by shared v2.3.1. Run its supported,
    # stricter all-bytes scanner explicitly; never disable contamination checking.
    registry = {card.get('contamination', {}).get('canary_guid', '')} - {''}
    leakage = scan_tree(Path('/app/output'), registry).as_dict()
    if leakage['canary_verdict'] == 'hit':
        verdict = {'admissible': False, 'score': None, 'labels': [FailureLabel.CONTAMINATION_CANARY.value],
                   'gate_results': {}, 'detail': {'error': 'official scan_tree detected contamination'}}
    else:
        verdict = asdict(build_verifier(ctx).run(ctx))
    verdict['canary_scan'] = leakage
    verdict['local_compatibility'] = 'official scan_tree preflight replaces withdrawn scan_canary call'
    Path('/workspace/verdict.json').write_text(json.dumps(verdict, indent=2, default=str))
    print(json.dumps({'admissible': verdict['admissible'], 'score': verdict['score']}))
except Exception as exc:
    Path('/workspace/verdict.json').write_text(json.dumps({'grader_error': type(exc).__name__, 'detail': str(exc)}))
    raise
""".replace("ELAPSED", repr(elapsed))
    (work / "grade.py").write_text(script)
    extra = [
        OFFICIAL / "official/track1-coding-public",
        OFFICIAL / "official/Agenthon2026-toolkit-v2.3.1/common",
    ]
    mounts = grading_mounts(task)
    runner = Sandbox(
        task, output, work, python=python, extra_read=extra, extra_binds=mounts
    )
    remaining = max(0.1, verifier_limit - (time.monotonic() - started))
    result = runner.run(
        f"{shlex.quote(str(python))} /workspace/grade.py", remaining, max_chars=80_000
    )
    (work / "grader.log").write_text(result["output"])
    path = work / "verdict.json"
    if result["timed_out"] or not path.exists():
        return {
            "grader_error": "timeout" if result["timed_out"] else "runtime_failure",
            "returncode": result["returncode"],
        }
    verdict = json.loads(path.read_text())
    verdict["verifier_elapsed_sec"] = round(time.monotonic() - started, 3)
    verdict["verifier_timeout_sec"] = verifier_limit
    if preparation:
        verdict["artifact_preparation"] = preparation
        if preparation.get("preparation_error"):
            verdict["admissible"] = False
            verdict["score"] = None
    verdict["grading_readonly_mounts"] = [
        {"source": str(source), "destination": destination}
        for source, destination in mounts
    ]
    report_path = work / "pytest_report.json"
    if report_path.exists():
        report = json.loads(report_path.read_text())
        code = (
            verdict.get("gate_results", {})
            .get("g3_domain_semantics", {})
            .get("detail", {})
            .get("pytest_returncode")
        )
        error = pytest_infrastructure_error(report, code)
        if error:
            verdict["grader_error"] = error
            verdict["admissible"] = False
    path.write_text(json.dumps(verdict, indent=2, ensure_ascii=False))
    return verdict


def evaluate(
    tasks: list[Path],
    run_dir: Path,
    config: Config,
    *,
    jobs=1,
    timeout=None,
    scorer_python: Path | None = None,
) -> dict:
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "roster.json").write_text(json.dumps([p.name for p in tasks], indent=2))

    def one(task):
        path = run_dir / task.name
        path.mkdir()
        try:
            agent = run_agent(
                task, path / "output", config, artifacts=path / "agent", timeout=timeout
            )
            verdict = grade(
                task, path / "output", path, agent["elapsed_sec"], scorer_python
            )
            solved = (
                agent.get("exit_code", int(agent["status"] != "completed")) == 0
                and verdict.get("admissible") is True
            )
            result = {
                "task": task.name,
                "solved": solved,
                "agent": agent,
                "verdict": verdict,
            }
        except Exception as exc:  # noqa: BLE001 - preserve the roster and mark infrastructure failures explicitly
            result = {
                "task": task.name,
                "solved": False,
                "error": config.redact(f"{type(exc).__name__}: {exc}"),
            }
        (path / "result.json").write_text(
            config.redact(json.dumps(result, indent=2, ensure_ascii=False))
        )
        print(
            json.dumps(
                {
                    "task": task.name,
                    "solved": result["solved"],
                    "agent_status": result.get("agent", {}).get("status"),
                    "grader_error": result.get("verdict", {}).get("grader_error"),
                    "error": result.get("error"),
                }
            ),
            flush=True,
        )
        return result

    results = {}
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(one, p): p.name for p in tasks}
        for future in as_completed(futures):
            result = future.result()
            results[result["task"]] = result
            snapshot = {
                "completed": len(results),
                "total": len(tasks),
                "solved": sum(r["solved"] for r in results.values()),
            }
            (run_dir / "progress.json").write_text(json.dumps(snapshot, indent=2))
    ordered = [results[p.name] for p in tasks]
    summary = summarize(ordered, config.model)
    (run_dir / "summary.json").write_text(
        config.redact(json.dumps(summary, ensure_ascii=False, indent=2))
    )
    return summary


def summarize(ordered: list[dict], model: str) -> dict:
    errors = [
        r["task"]
        for r in ordered
        if r.get("error") or r.get("verdict", {}).get("grader_error")
    ]
    solved = sum(r["solved"] for r in ordered)
    return {
        "scope": "local_public_development",
        "model": model,
        "tasks": len(ordered),
        "solved": solved,
        "pass_at_1": solved / len(ordered) if ordered and not errors else None,
        "valid_measurement": not errors,
        "infrastructure_errors": errors,
        "official_leaderboard_score": False,
        "attempts_per_task": 1,
        "usage": {
            key: sum(r.get("agent", {}).get("usage", {}).get(key, 0) for r in ordered)
            for key in ("input_tokens", "output_tokens", "calls", "estimated_calls")
        },
        "results": ordered,
    }


def rescore(
    run_dir: Path,
    out_dir: Path,
    units_dir: Path = DEFAULT_UNITS,
    scorer_python: Path | None = None,
) -> dict:
    """Re-evaluate existing deliverables without model calls or replacing original records."""
    names = json.loads((run_dir / "roster.json").read_text())
    if any(not (run_dir / name / "agent/run.json").exists() for name in names):
        raise ValueError(
            "Every roster task must have a completed agent run record before rescore"
        )
    out_dir.mkdir(parents=True, exist_ok=False)
    (out_dir / "roster.json").write_text(json.dumps(names, indent=2))
    results = []
    for name in names:
        if Path(name).name != name or name in {".", ".."}:
            raise ValueError("Invalid task name in roster")
        agent = json.loads((run_dir / name / "agent/run.json").read_text())
        verdict = grade(
            units_dir / name,
            run_dir / name / "output",
            out_dir / name,
            agent["elapsed_sec"],
            scorer_python,
        )
        result = {
            "task": name,
            "agent": agent,
            "verdict": verdict,
            "solved": agent.get("exit_code", int(agent["status"] != "completed")) == 0
            and verdict.get("admissible") is True,
        }
        results.append(result)
        (out_dir / name / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
        print(json.dumps({"task": name, "solved": result["solved"]}), flush=True)
    summary = summarize(results, results[0]["agent"]["model"] if results else "unknown")
    summary["source_run"] = str(run_dir.resolve())
    summary["new_model_calls"] = 0
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)
    )
    return summary
