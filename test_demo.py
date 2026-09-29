import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class DemoTests(unittest.TestCase):
    def test_demo_builds_synthetic_history_without_screens(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run([sys.executable, str(ROOT / 'scripts/demo.py'), tmp, '--no-screens'],
                                  capture_output=True, text=True, cwd=ROOT)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = Path(tmp)
            summary = json.loads((out / 'demo-summary.json').read_text())
            self.assertEqual(set(summary['harnesses']), {'claude', 'codex', 'pi', 'opencode'})
            self.assertTrue(all(h['observations'] > 0 for h in summary['harnesses'].values()))
            self.assertGreaterEqual(summary['session']['subagent_nodes'], 3)
            self.assertGreaterEqual(summary['session']['inferred_children'], 1)
            self.assertTrue((out / 'demo-report.html').is_file())
            overhead = (out / 'overhead.txt').read_text()
            self.assertRegex(overhead, r'(?m)^claude ')
            self.assertRegex(overhead, r'(?m)^codex ')
            self.assertFalse((out / 'overview.png').exists())


if __name__ == '__main__':
    unittest.main()
