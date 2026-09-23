from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Sandbox:
    def __init__(
        self,
        task: Path,
        output: Path,
        work: Path,
        mode: str = "local",
        *,
        python: Path | None = None,
        extra_read: list[Path] | None = None,
        extra_binds: list[tuple[Path, str]] | None = None,
    ):
        self.task, self.output, self.work = (
            task.resolve(),
            output.resolve(),
            work.resolve(),
        )
        self.mode = mode
        self.python = Path(os.path.abspath(python or sys.executable))
        self.extra_read = extra_read or []
        self.extra_binds = extra_binds or []
        for source, destination in self.extra_binds:
            if (
                not source.exists()
                or not Path(destination).is_absolute()
                or ".." in Path(destination).parts
            ):
                raise ValueError("Invalid read-only sandbox binding")
        for p in (self.output, self.work):
            p.mkdir(parents=True, exist_ok=True)
        self.proot = ROOT / ".tools/proot/usr/bin/proot"
        if mode == "local" and not self.proot.exists():
            raise RuntimeError("Local runtime missing: run scripts/setup_local.sh")
        if mode == "container" and os.environ.get("T1_CONTAINER_RUNTIME") != "1":
            raise RuntimeError(
                "container mode is only enabled inside the supplied Docker image"
            )

    def run(
        self,
        command: str,
        timeout: float,
        *,
        max_chars: int = 24_000,
        log_path: Path | None = None,
    ) -> dict:
        started = time.monotonic()
        env = {
            "PATH": f"{self.python.parent}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.work),
            "TMPDIR": str(self.work),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONNOUSERSITE": "1",
            "OPENBLAS_NUM_THREADS": "4",
            "OMP_NUM_THREADS": "4",
            "MKL_NUM_THREADS": "4",
            "POLARS_MAX_THREADS": "4",
            "MPLCONFIGDIR": str(self.work / ".mpl"),
            "NUMBA_CACHE_DIR": str(self.work / ".numba"),
            "TASK_DIR": "/input" if self.mode == "local" else str(self.task),
            "OUTPUT_DIR": "/app/output" if self.mode == "local" else str(self.output),
        }
        if self.mode == "container":
            argv = [
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-o",
                "pipefail",
                "-c",
                command,
            ]
            cwd = self.work
        else:
            rootfs = self.work / ".rootfs"
            for name in (
                "usr",
                "bin",
                "lib",
                "lib64",
                "dev",
                "etc",
                "tmp",
                "input",
                "output",
                "app/output",
                "workspace",
            ):
                (rootfs / name).mkdir(parents=True, exist_ok=True)
            libdir = ROOT / ".tools/proot/usr/lib/x86_64-linux-gnu"
            env.update(
                {
                    "LD_LIBRARY_PATH": str(libdir),
                    "PROOT_TMP_DIR": str(self.work),
                    "PROOT_NO_SECCOMP": "1",
                    "PROOT_LOADER": str(ROOT / ".tools/proot/loader"),
                    "PROOT_LOADER_32": str(ROOT / ".tools/proot/loader-m32"),
                }
            )
            # Bind only standard runtime trees, never the full host root or competition folder.
            readonly = [
                Path(p)
                for p in (
                    "/usr",
                    "/bin",
                    "/lib",
                    "/lib64",
                    "/etc/ld.so.cache",
                    "/etc/localtime",
                    # Polars 1.39.3's sysinfo dependency requires memory metadata.
                    # Expose this one read-only file; never expose process environments/fds.
                    "/proc/meminfo",
                )
                if Path(p).exists()
            ]
            # A venv symlinks to a standalone interpreter; both prefixes are needed.
            venv = self.python.parent.parent
            base = self.python.resolve().parent.parent
            readonly += [venv, base, ROOT / ".tools/proot", self.task, *self.extra_read]
            readonly += [source.resolve() for source, _ in self.extra_binds]
            if self.python.is_symlink():
                link = self.python.readlink()
                if link.is_absolute():
                    readonly.append(link.parent.parent)
            for p in readonly + [
                self.work,
                self.output,
                Path("/dev/null"),
                Path("/dev/urandom"),
            ]:
                dest = rootfs / str(p).lstrip("/")
                if p.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.touch(exist_ok=True)
            argv = [str(self.proot), "-r", str(rootfs)]
            for p in dict.fromkeys(readonly):
                argv += ["-b", f"{p}:{p}"]
            argv += [
                "-b",
                f"{self.task}:/input",
                "-b",
                f"{self.output}:/app/output",
                "-b",
                f"{self.output}:/output",
                "-b",
                f"{self.work}:/workspace",
                "-b",
                f"{self.work}:{self.work}",
                "-b",
                f"{self.output}:{self.output}",
                "-b",
                f"{self.work}:/tmp",
                "-b",
                "/dev/null",
                "-b",
                "/dev/urandom",
                "-w",
                "/workspace",
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-o",
                "pipefail",
                "-c",
                command,
            ]
            data = self.task / "environment/data"
            if data.is_dir():
                (rootfs / "app/data").mkdir(parents=True, exist_ok=True)
                (rootfs / "data").mkdir(parents=True, exist_ok=True)
                argv[3:3] = ["-b", f"{data}:/app/data", "-b", f"{data}:/data"]
            # Trusted grader-only mappings reproduce official input and check paths.
            # The solver never supplies these bindings or receives reference data.
            for source, destination in self.extra_binds:
                target = rootfs / destination.lstrip("/")
                if source.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.touch(exist_ok=True)
                argv[3:3] = ["-b", f"{source.resolve()}:{destination}"]
            spec = {
                "argv": argv,
                "env": env,
                "read_paths": [str(p.resolve()) for p in readonly] + ["/dev/urandom"],
                "write_paths": [str(self.output), str(self.work), "/dev/null"],
            }
            # This control file lives outside the child-visible workspace.
            fd, filename = tempfile.mkstemp(prefix="t1-launch-", suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(spec, f)
            argv = [sys.executable, "-m", "t1_agent.worker", filename]
            cwd = ROOT
        timed_out = False
        try:
            with tempfile.TemporaryFile() as log:
                proc = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    proc.wait(timeout=max(0.01, timeout))
                except subprocess.TimeoutExpired:
                    timed_out = True
                finally:
                    # Kill the whole group, including background children after a successful shell exit.
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
                size = log.tell()
                log.seek(0)
                if log_path is not None:
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    with log_path.open("wb") as target:
                        shutil.copyfileobj(log, target)
                    log.seek(0)
                if size <= max_chars:
                    raw = log.read()
                else:
                    raw = log.read(max_chars // 2)
                    log.seek(-max_chars // 2, os.SEEK_END)
                    raw += b"\n... output truncated ...\n" + log.read()
            return {
                "returncode": proc.returncode,
                "timed_out": timed_out,
                "elapsed_sec": round(time.monotonic() - started, 3),
                "output": raw.decode(errors="replace"),
            }
        finally:
            if self.mode == "local":
                Path(filename).unlink(missing_ok=True)
