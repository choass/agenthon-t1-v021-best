"""Private launcher, always invoked in a new process before applying restrictions."""

from __future__ import annotations

import json
import os
import resource
import sys
from pathlib import Path

from t1_agent.isolation import restrict


def main() -> None:
    spec = json.loads(Path(sys.argv[1]).read_text())
    # Limit runaway output and recursion. Memory/CPU quotas are ultimately container-owned.
    resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    restrict(spec["read_paths"], spec["write_paths"])
    os.execve(spec["argv"][0], spec["argv"], spec["env"])


if __name__ == "__main__":
    main()
