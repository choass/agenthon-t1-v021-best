"""Local grader adapter for the public executable-artifact contract.

This module is never used by the solver. It copies ONLY the grader's raw test input,
not checks or reference outputs, into a separate offline execution sandbox.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import shutil
from pathlib import Path

from .sandbox import Sandbox
from .task import input_mounts, stage_task
from .workflow import inspect_outputs


def prepare_polars(task: Path, output: Path, artifacts: Path, timeout: float) -> dict:
    if importlib.metadata.version("polars") != "1.39.3":
        return {"grader_error": "executable_runtime_version_mismatch"}
    instruction = (task / "instruction.md").read_text()
    if not all(
        token in instruction
        for token in ("pipeline_new", "function-under-new-api.py", "1.39.3")
    ):
        return {"grader_error": "executable_contract_changed"}
    raw = task / "checks/ticker_data_test.csv"
    if not raw.is_file() or raw.is_symlink():
        return {"grader_error": "executable_test_input_missing"}
    before = inspect_outputs(output, [])
    if before["errors"]:
        return {
            "preparation_error": "Unsafe or invalid submitted files",
            "details": before["errors"],
        }
    root = artifacts / "preparation"
    staged, derived, work = root / "input", root / "output", root / "workspace"
    root.mkdir(parents=True, exist_ok=False)
    stage_task(task, staged)
    # The task promises replacement of this pilot path at evaluation time.
    shutil.copyfile(raw, staged / "environment/data/ticker_data_pilot.csv")
    derived.mkdir()
    for name in before["files"]:
        dst = derived / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(output / name, dst)
    # Use the participant runtime, pinned to Python 3.13 + polars 1.39.3 at build time.
    runner = Sandbox(staged, derived, work, extra_binds=input_mounts(staged))
    log = root / "test_gen.log"
    command = """python -I - <<'PY'
import importlib.util
import warnings
from pathlib import Path
import polars
assert polars.__version__ == '1.39.3', 'Harness runtime must use Polars 1.39.3'
warnings.simplefilter('always')
path = '/app/output/function-under-new-api.py'
compile(Path(path).read_text(), path, 'exec')
spec = importlib.util.spec_from_file_location('submitted_pipeline', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.pipeline_new(polars, '/app/data/ticker_data_pilot.csv', '/app/output')
print('pipeline_new execution completed')
PY"""
    result = runner.run(command, timeout, max_chars=8000, log_path=log)
    record = {
        "adapter": "public_polars_callable_v1",
        "scope": "local_public_development",
        "original_output": str(output.resolve()),
        "grading_output": str(derived.resolve()),
        "input_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
        "submitted_sha256": before["files"],
        "log": str(log.resolve()),
        "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        "execution": result,
        "no_checks_or_reference_outputs_mounted": True,
        "original_output_unchanged": inspect_outputs(output, [])["files"]
        == before["files"],
    }
    if result["returncode"] != 0 or result["timed_out"]:
        record["preparation_error"] = "submitted_callable_execution_failed"
    (root / "preparation.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False)
    )
    return record
