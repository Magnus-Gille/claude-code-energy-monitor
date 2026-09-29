import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from usage.report import build_report, render_report, write_report


def observation(identity='one', **changes):
    item = dict(id=identity, ts='2026-10-25T00:30:00+00:00', harness='claude',
                provider='anthropic', project_id='/private/client/app', project_label='app',
                session='private-session', parent_session=None, turn_id='private-turn',
                turn_confidence='observed', model='model-a', effort='high',
                thread_kind='main', agent='private-agent', origin='cli',
                tokens=dict(fresh_input=10, cache_read=20, cache_write=5, output=7, reasoning=3),
                complete=True, id_synthetic=False, warnings=[],
                cwd='/private/client/app', sources=['/private/log.jsonl'],
                raw_usage={'secret': 'PRIVATE PROMPT'}, machine='private-machine')
    item.update(changes)
    return item


class ReportTests(unittest.TestCase):
    def test_redaction_is_default_and_allowlist_excludes_private_fields(self):
        report = build_report([observation()], {'imports': [{'root': '/private/logs', 'harness': 'claude', 'status': 'ok'}]})
        encoded = json.dumps(report)
        for secret in ('/private/', 'PRIVATE PROMPT', 'private-session', 'private-agent', 'private-turn', 'private-machine'):
            self.assertNotIn(secret, encoded)
        self.assertEqual(report['privacy'], 'redacted')
        self.assertEqual(report['records'][0]['tokens']['reasoning'], 3)

    def test_project_collisions_are_distinct_and_private_labels_unique(self):
        rows = [observation(), observation('two', project_id='/other/client/app')]
        result = build_report(rows, {}, redact=False)['records']
        self.assertNotEqual(result[0]['project_id'], result[1]['project_id'])
        self.assertNotEqual(result[0]['project_label'], result[1]['project_label'])
        self.assertTrue(all('app' in r['project_label'] for r in result))
        self.assertNotIn('/private/', json.dumps(result))

    def test_session_identity_is_scoped_by_harness(self):
        rows = [observation(), observation('two', harness='pi', provider='test')]
        redacted = build_report(rows, {})['records']
        private = build_report(rows, {}, redact=False)['records']
        self.assertNotEqual(redacted[0]['session'], redacted[1]['session'])
        self.assertEqual({row['session'] for row in private}, {
            'claude:private-session', 'pi:private-session',
        })

    def test_template_uses_neutral_copy_and_cache_comparison_mounts(self):
        html = render_report(build_report([], {}))
        self.assertIn('<h1>Tokenanvändning</h1>', html)
        self.assertIn('Cacheläsningsandel', html)
        self.assertIn('id="cache-comparisons"', html)
        for dimension in ('session', 'harness', 'model', 'project_id'):
            self.assertIn(f'data-cache-dimension="{dimension}"', html)
        for old_copy in ('Din användning, förklarad', 'Vart tog alla tokens vägen?',
                         'Se mönstret. Hitta toppen.', 'Vad driver användningen?',
                         'Synlig täckning. Ärliga gränser.'):
            self.assertNotIn(old_copy, html)

    def test_dst_repeated_hour_remains_distinct(self):
        rows = [observation(), observation('two', ts='2026-10-25T01:30:00+00:00')]
        result = build_report(rows, {})['records']
        self.assertEqual(result[0]['date'], result[1]['date'])
        self.assertNotEqual(result[0]['hour'], result[1]['hour'])
        self.assertTrue(result[0]['hour'].endswith('+02:00'))
        self.assertTrue(result[1]['hour'].endswith('+01:00'))

    def test_nulls_and_ambiguous_identity_are_preserved(self):
        row = observation(id_synthetic=True, complete=False)
        row['tokens']['cache_read'] = None
        result = build_report([row], {})['records'][0]
        self.assertIsNone(result['tokens']['cache_read'])
        self.assertTrue(result['id_synthetic'])
        self.assertFalse(result['complete'])

    def test_safe_json_cannot_break_out_of_script(self):
        report = build_report([observation(model='</script><script>alert(1)</script>')], {}, redact=False)
        html = render_report(report, template='<script type="application/json">__USAGE_DATA__</script>')
        self.assertEqual(html.count('</script>'), 1)
        self.assertNotIn('<script>alert', html)
        decoded = json.loads(html.split('>', 1)[1].rsplit('</script>', 1)[0])
        self.assertEqual(decoded['records'][0]['model'], '</script><script>alert(1)</script>')

    def test_empty_report_coverage_is_not_claimed_complete(self):
        report = build_report([], {})
        self.assertEqual(report['records'], [])
        self.assertFalse(report['coverage']['coverage_complete'])
        self.assertFalse(report['coverage']['billing_verified'])

    def test_write_is_private_and_replaces_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.html'
            write_report(path, 'one')
            write_report(path, 'two')
            self.assertEqual(path.read_text(), 'two')
            if os.name != 'nt':
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)


class ReportReviewTests(unittest.TestCase):
    def test_lone_surrogate_renders_and_writes(self):
        report = build_report([observation(project_id='/work/\ud800')], {}, redact=False)
        html = render_report(report)
        with tempfile.TemporaryDirectory() as tmp:
            write_report(Path(tmp) / 'r.html', html)
            self.assertTrue((Path(tmp) / 'r.html').is_file())

    def test_redacted_report_only_shows_public_provider_and_model_names(self):
        rows = [observation('a', provider='m5', model='qwen3-coder', origin='cli'),
                observation('b', provider='inference-gille', model='gpt-oss-120b', origin='my-host-tui'),
                observation('c', provider='anthropic', model='claude-opus-5-5'),
                observation('d', provider='openai', model='gpt-5.6-luna', effort='xhigh')]
        html = render_report(build_report(rows, {}))
        for private in ('m5', 'inference-gille', 'qwen3-coder', 'gpt-oss-120b', 'my-host-tui'):
            self.assertNotIn(private, html)
        for public in ('anthropic', 'claude-opus-5-5', 'openai', 'gpt-5.6-luna', 'xhigh'):
            self.assertIn(public, html)
        private = render_report(build_report(rows, {}, redact=False))
        self.assertIn('inference-gille', private)

if __name__ == '__main__':
    unittest.main()
