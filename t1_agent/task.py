from __future__ import annotations

import re
import shlex
import shutil
import tomllib
from pathlib import Path

EXCLUDED = {
    "checks",
    "tests",
    "reference",
    "reference_data",
    "solution",
    "solutions",
    ".git",
    "__pycache__",
    "dev",
}


def stage_task(source: Path, target: Path) -> dict:
    """Stage participant-visible inputs. The official original stays untouched for scoring."""
    source = source.resolve()
    card = tomllib.loads((source / "card.toml").read_text())
    marker = card.get("contamination", {}).get("canary_guid", "")
    if card.get("schema_version") != "2.0":
        raise ValueError("Expected T1 card schema_version 2.0")
    target.mkdir(parents=True, exist_ok=False)
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        if any(
            part in EXCLUDED or part.startswith(("oracle_", "answer_key", "expected"))
            for part in rel.parts
        ):
            continue
        if path.is_symlink():
            raise ValueError(f"Refusing task symlink: {rel}")
        if path.is_file():
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
            if marker and path.stat().st_size < 8 * 1024 * 1024:
                # Headers in Dockerfiles/scripts can repeat the card's marker too.
                try:
                    content = dest.read_text()
                except (UnicodeDecodeError, ValueError):
                    continue
                if marker.lower() in content.lower():
                    dest.write_text(
                        re.sub(
                            re.escape(marker),
                            "[redacted]",
                            content,
                            flags=re.IGNORECASE,
                        )
                    )
    # Contamination metadata is irrelevant to solving; keep its marker out of model trajectories.
    text = (target / "card.toml").read_text()
    text = re.sub(r"(?m)^\s*canary_guid\s*=.*$", 'canary_guid = "[redacted]"', text)
    (target / "card.toml").write_text(text)
    return card


def list_files(task: Path) -> str:
    files = [
        f"{p.relative_to(task)} ({p.stat().st_size} bytes)"
        for p in sorted(task.rglob("*"))
        if p.is_file()
    ]
    return "\n".join(files)[:24_000]


def input_mounts(task: Path) -> list[tuple[Path, str]]:
    """Read-only aliases from this task's Docker COPY instructions, never its checks."""
    task = task.resolve()
    mounts = []
    environment = task / "environment"
    dockerfile = environment / "Dockerfile"
    if dockerfile.exists():
        for line in dockerfile.read_text().splitlines():
            if not line.strip().upper().startswith("COPY "):
                continue
            words = shlex.split(line, comments=True)
            if not words or words[0].upper() != "COPY":
                continue
            if len(words) != 3 or words[1].startswith("--"):
                raise RuntimeError("Unsupported official Docker COPY mapping")
            source = (environment / words[1]).resolve()
            destination = words[2]
            if not source.is_relative_to(environment.resolve()) or not source.exists():
                raise RuntimeError(
                    "Official Docker COPY input is missing or outside task"
                )
            if not destination.startswith("/app/") or ".." in Path(destination).parts:
                raise RuntimeError("Unsupported official Docker COPY destination")
            if source.is_file() and destination.endswith("/"):
                destination += source.name
            mounts.append((source, destination.rstrip("/")))
    return mounts
