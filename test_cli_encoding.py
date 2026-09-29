import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tokenatlas.history import History

ROOT = Path(__file__).resolve().parent


class CliEncodingTests(unittest.TestCase):
    def test_session_output_survives_a_non_utf8_stdout(self):
        # Windows pipes default to cp1252, which has no '≥'; the CLI must not crash on such a stdout.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'logs/proj/S.jsonl'; path.parent.mkdir(parents=True)
            row = {'type': 'assistant', 'uuid': 'u1', 'requestId': 'r1', 'sessionId': 'S', 'cwd': '/w/app',
                   'timestamp': '2026-09-10T10:00:00Z',
                   'message': {'id': 'm1', 'model': 'claude-opus-5-5', 'stop_reason': None,
                               'usage': {'input_tokens': 10, 'cache_read_input_tokens': 20,
                                         'cache_creation_input_tokens': 0, 'output_tokens': 5}}}
            path.write_text(json.dumps(row) + '\n')
            db = root / 'h.sqlite3'
            with History(db) as h:
                h.refresh('claude', root / 'logs')
            env = {**os.environ, 'PYTHONIOENCODING': 'cp1252', 'PYTHONDONTWRITEBYTECODE': '1'}
            proc = subprocess.run([sys.executable, '-m', 'tokenatlas', '--db', str(db), 'session', 'S'],
                                  cwd=ROOT, env=env, capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode('utf-8', 'replace'))
            self.assertIn(b'claude:S', proc.stdout)


if __name__ == '__main__':
    unittest.main()
