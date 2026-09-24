"""Run inside the real submission image with production-like filesystem limits."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

requests_seen = []
errors = []
write = """python - <<'PYCODE'
import json, os
from pathlib import Path
assert not os.environ.get('MODEL_TOKEN')
value = json.loads(Path('/input/environment/data/value.json').read_text())['value']
Path('/app/output/results.json').write_text(json.dumps({'answer': value * 2}))
PYCODE"""
check = """python - <<'PYCODE'
import json
from pathlib import Path
assert json.loads(Path('/app/output/results.json').read_text())['answer'] == 14
PYCODE"""
source = """import json, os
from pathlib import Path
assert not os.environ.get('MODEL_TOKEN')
value = json.loads(Path('/input/environment/data/value.json').read_text())['value']
Path('/app/output/results.json').write_text(json.dumps({'answer': value * 1}))
"""
actions = [
    ('checkpoint', {'outputs': ['results.json'], 'checks': ['answer is double input'],
                    'conventions': 'integer arithmetic', 'progress': 'ready'}),
    ('write_file', {'path': 'solve.py', 'content': source}),
    ('read_file', {'path': 'solve.py'}),
    ('edit_file', {'path': 'solve.py', 'old': 'value * 1', 'new': 'value * 2'}),
    ('bash', {'command': 'python solve.py'}),
    ('verify', {'command': check, 'coverage': 'recompute answer from input'}),
    ('finish', {'summary': 'ready for independent review'}),
    ('verify', {'command': check, 'coverage': 'independent answer check'}),
    ('finish', {'summary': 'complete'}),
]

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert self.path == '/v1/chat/completions'
            assert self.headers['Authorization'] == 'Bearer smoke-only'
            assert data['max_tokens'] == 4000
            assert data['chat_template_kwargs'] == {'enable_thinking': False}
            assert data['model'] == 'nvidia/nemotron-3-super-120b-a12b'
            assert '/input/environment/data/value.json' in data['messages'][1]['content']
            index = len(requests_seen)
            requests_seen.append(data)
            name, args = actions[index]
            message = {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': f'call_{index}', 'type': 'function',
                 'function': {'name': name, 'arguments': json.dumps(args)}}]}
            body = json.dumps({'choices': [{'message': message, 'finish_reason': 'tool_calls'}],
                               'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}).encode()
            self.send_response(200)
        except Exception as exc:
            errors.append(repr(exc))
            body = b'{"error":"smoke assertion failed"}'
            self.send_response(500)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
env = dict(os.environ, MODEL_ENDPOINT=f'http://127.0.0.1:{server.server_port}',
           MODEL_NAME='nvidia/nemotron-3-super-120b-a12b', MODEL_TOKEN='smoke-only')
run = subprocess.run([sys.executable, '-m', 't1_agent', 'solve', '--task-dir', '/input',
                      '--out', '/app/output', '--artifacts', '/tmp/smoke-artifacts',
                      '--request-retries', '0'], env=env, text=True, capture_output=True, timeout=90)
server.shutdown()
assert not errors, errors
assert run.returncode == 0, (run.stdout, run.stderr)
report = json.loads(run.stdout)
assert report['status'] == 'completed', report
assert report['usage']['calls'] == 9, report['usage']
assert not Path('/tmp/smoke-artifacts/input').exists(), 'Input must not be copied to tmpfs'
assert json.loads(Path('/app/output/results.json').read_text()) == {'answer': 14}
assert Path('/output/results.json').read_bytes() == Path('/app/output/results.json').read_bytes()
print('Container smoke passed: nonroot, readonly, offline, 64MiB tmpfs, nine House calls with read/write/edit, tools, review, output aliases.')
