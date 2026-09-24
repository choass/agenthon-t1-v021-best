"""Small file operations executed inside the same offline sandbox as bash."""

from __future__ import annotations

import json
import shlex

FILE_TOOLS = [
    (
        "read_file",
        "Read numbered text: 200 lines by default, up to 500 per call. Read the needed source functions together instead of repeatedly reading small overlapping fragments. Also supports task instructions and local finance guides.",
        {
            "path": {"type": "string"},
            "start": {"type": "integer"},
            "lines": {"type": "integer"},
        },
        ["path"],
    ),
    (
        "write_file",
        "Write UTF-8 source or notes exactly, without shell quoting. Python syntax is checked; execute the file afterwards.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    ),
    (
        "edit_file",
        "Replace one exact, unique text span. Fails without changing the file if the old text is missing or ambiguous. Prefer this to rewriting working code.",
        {
            "path": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string"},
        },
        ["path", "old", "new"],
    ),
]

# This script runs in the child sandbox, never in the credential-bearing controller.
SCRIPT = r"""
import ast, hashlib, json, os, sys, tempfile
from pathlib import Path
spec = json.loads(sys.argv[1])
path = Path(spec['path']).expanduser()
if not path.is_absolute(): path = Path.cwd() / path
if '..' in path.parts: raise ValueError('Parent traversal is not allowed')
path = path.resolve()
action = spec['action']
if action == 'read_file':
    start = max(1, int(spec.get('start', 1)))
    count = min(500, max(1, int(spec.get('lines', 200))))
    if path.stat().st_size > 8_000_000: raise ValueError('Use a bounded data query for large files')
    rows = path.read_text().splitlines()
    print(json.dumps({'path': str(path), 'total_lines': len(rows), 'start': start, 'end': min(len(rows),start+count-1)}))
    print('\n'.join(f'{i+1}: {rows[i]}' for i in range(start-1,min(len(rows),start+count-1))))
else:
    roots = [Path.cwd().resolve(), Path(os.environ['OUTPUT_DIR']).resolve(), Path('/workspace').resolve(), Path('/output').resolve(), Path('/app/output').resolve()]
    if not any(path.is_relative_to(r) and path != r for r in roots): raise ValueError('Only workspace and output files are writable')
    if path.name in {'reward.json','pytest_report.json'}: raise ValueError('Reserved evaluator filename')
    if path.exists() and not path.is_file(): raise ValueError('Expected a regular file')
    if action == 'edit_file':
        old, new = spec['old'], spec['new']
        text = path.read_text()
        if not old or text.count(old) != 1: raise ValueError('Old text must match exactly once; read the relevant lines first')
        text = text.replace(old, new, 1)
    else: text = spec['content']
    if not isinstance(text,str) or len(text.encode()) > 256_000: raise ValueError('Write a smaller source module')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.edit-')
    try:
        with os.fdopen(fd,'w') as f: f.write(text)
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)
    result = {'path': str(path), 'bytes': len(text.encode()), 'sha256': hashlib.sha256(text.encode()).hexdigest()}
    if path.suffix == '.py':
        try: ast.parse(text); result['python_syntax'] = 'valid; execution still required'
        except SyntaxError as exc: result['python_syntax_error'] = f'line {exc.lineno}: {exc.msg}'
    print(json.dumps(result))
"""


def file_command(name: str, args: dict) -> str:
    if name not in {x[0] for x in FILE_TOOLS}:
        raise ValueError("Unknown file tool")
    spec = {**args, "action": name}
    return "python -c " + shlex.quote(SCRIPT) + " " + shlex.quote(json.dumps(spec))


def definitions() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }
        for name, description, props, required in FILE_TOOLS
    ]
