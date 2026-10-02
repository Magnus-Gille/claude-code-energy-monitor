import base64
import gzip
import json
import re
import unittest
from datetime import datetime, timezone

from tokenatlas import __main__ as cli
from tokenatlas.report import build_report, render_report
from tokenatlas.resume import codex_link, resume_command, resume_info

UUID = '019a1b2c-3d4e-7f80-9a1b-2c3d4e5f6a7b'


def record(harness, session, turn='t1'):
    return dict(id='o1', harness=harness, session=session, agent='main', thread_kind='main', parent_session=None, turn_id=turn,
                turn_confidence='derived', ts='2026-09-03T10:00:00+00:00', model='gpt-5', provider='openai', machine='m',
                project_id='/w/app', project_label='app', effort=None, origin='cli', raw_usage={}, tariff=None,
                tokens=dict(fresh_input=1000, cache_write=0, cache_read=0, output=10, reasoning=0), complete=True,
                id_synthetic=False, warnings=[], sources=[])


def payload(html):
    match = re.search(r'<script id="report-data" type="application/octet-stream\+base64">([A-Za-z0-9+/=]+)</script>', html)
    return json.loads(gzip.decompress(base64.b64decode(match.group(1))).decode('utf-8'))


class ResumeCommandTests(unittest.TestCase):
    def test_one_command_per_harness_prefixed_with_cd(self):
        self.assertEqual(resume_command('claude', 'abc-123', '/w/app', windows=False), 'cd /w/app && claude --resume abc-123')
        self.assertEqual(resume_command('codex', UUID, '/w/app', windows=False), f'cd /w/app && codex resume {UUID}')
        self.assertEqual(resume_command('pi', 'p1', '/w/app', windows=False), 'cd /w/app && pi --session p1')
        self.assertEqual(resume_command('opencode', 'ses_1', '/w/app', windows=False), 'cd /w/app && opencode --session ses_1')

    def test_cwd_with_spaces_and_quotes_is_quoted(self):
        self.assertEqual(resume_command('codex', UUID, "/w/my app/it's", windows=False), f"""cd '/w/my app/it'"'"'s' && codex resume {UUID}""")

    def test_claude_needs_a_directory_others_do_not(self):
        self.assertIsNone(resume_command('claude', 'abc', None, windows=False))
        self.assertIsNone(resume_command('claude', 'abc', '  ', windows=False))
        self.assertEqual(resume_command('codex', UUID, None, windows=False), f'codex resume {UUID}')

    def test_unsafe_ids_harnesses_and_control_characters_give_none(self):
        for bad in (None, '', 'unknown ', '--help', '-x', 'a b', 'a;rm -rf', 'a\nb', 'x' * 200):
            self.assertIsNone(resume_command('claude', bad, '/w', windows=False), bad)
        self.assertIsNone(resume_command('mystery', 'abc', '/w', windows=False))
        self.assertIsNone(resume_command('codex', UUID, '/w\nrm -rf', windows=False))

    def test_windows_path_with_unsafe_characters_gives_none(self):
        self.assertIsNone(resume_command('claude', 'abc', r'C:\a&b', windows=True))
        self.assertIsNone(resume_command('claude', 'abc', r'C:\100%', windows=True))
        self.assertEqual(resume_command('claude', 'abc', r'C:\my work', windows=True), r'cd /d "C:\my work" && claude --resume abc')

    def test_main_module_keeps_its_shell_quoting_helper(self):
        self.assertEqual(cli._shell_command(['a', 'b c'], windows=False), "a 'b c'")

    def test_codex_link_only_for_uuid_shaped_ids(self):
        self.assertEqual(codex_link(UUID), f'codex://threads/{UUID}')
        for bad in (None, '', 'cx-acme-01', UUID + '/../x', UUID + '?a=b', 'codex://threads/' + UUID, UUID[:-1], UUID.upper() + ' '):
            self.assertIsNone(codex_link(bad), bad)
        self.assertEqual(resume_info('codex', 'not-a-uuid', '/w', windows=False), {'command': 'cd /w && codex resume not-a-uuid', 'codex_link': None})
        self.assertIsNone(resume_info('claude', 'abc', None))  # nothing known
        self.assertIsNone(resume_info('claude', UUID, '/w', windows=False)['codex_link'])  # only Codex has a deep link


class ReportBoundaryTests(unittest.TestCase):
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)

    def private(self, **extra):
        records = [record('codex', UUID), record('claude', 'sess-1', 't9')]
        ctx = {('codex', UUID, 't1'): {'cwd': "/w/my app"}, ('claude', 'sess-1', 't9'): {'cwd': '/w/app'}}
        return build_report(records, {}, redact=False, prompt_texts={('codex', UUID, 't1'): 'hello'}, prompt_context=ctx, now=self.now, **extra)

    def test_private_report_carries_the_resume_data(self):
        found = self.private()['prompt_resume']
        self.assertEqual(sorted(((v['codex_link'] or '', v['command']) for v in found.values())),
                         [('', 'cd /w/app && claude --resume sess-1'), (f'codex://threads/{UUID}', f"cd '/w/my app' && codex resume {UUID}")])
        self.assertNotIn('demo', self.private())
        self.assertIs(self.private(demo=True)['demo'], True)

    def test_text_only_turn_without_cwd_still_gets_a_codex_link(self):
        report = build_report([record('codex', UUID)], {}, redact=False, prompt_texts={('codex', UUID, 't1'): 'x'}, now=self.now)
        self.assertEqual(list(report['prompt_resume'].values()), [{'command': f'codex resume {UUID}', 'codex_link': f'codex://threads/{UUID}'}])

    def test_ids_with_colons_do_not_collide(self):
        # ('opencode', 'a:b', 'c') and ('opencode', 'a', 'b:c') join to the same string; each turn must keep its own text and command
        recs = [dict(record('opencode', 'a:b', 'c'), id='o1'), dict(record('opencode', 'a', 'b:c'), id='o2', ts='2026-09-03T11:00:00+00:00')]
        texts = {('opencode', 'a:b', 'c'): 'first', ('opencode', 'a', 'b:c'): 'second'}
        report = build_report(recs, {}, redact=False, prompt_texts=texts, now=self.now)
        self.assertEqual(sorted(report['prompt_texts'].values()), ['first', 'second'])
        self.assertEqual(len(set(report['prompt_texts'])), 2)
        commands = {report['prompt_texts'][k]: report['prompt_resume'][k]['command'] for k in report['prompt_texts']}
        self.assertEqual(commands, {'first': 'opencode --session a:b', 'second': 'opencode --session a'})

    def test_shared_report_has_no_ids_commands_links_or_paths(self):
        html = render_report(build_report([record('codex', UUID), record('claude', 'sess-1', 't9')], {}, redact=True, now=self.now))
        data = payload(html)
        self.assertNotIn('prompt_resume', data)
        self.assertNotIn('demo', data)
        blob = json.dumps(data)
        for text in (UUID, 'sess-1', 'codex://', 'resume', '--session', '/w/'):
            self.assertNotIn(text, blob)
        with self.assertRaises(ValueError):  # the redacted report refuses side-file data outright
            build_report([record('codex', UUID)], {}, redact=True, prompt_texts={}, now=self.now)


class TopOutputTests(unittest.TestCase):
    def test_top_prints_a_validated_resume_command_under_each_turn(self):
        prompt = dict(harness='codex', session=UUID, turn_id='t1', cost=1.0, cost_complete=True, credits=None, credits_lower_bound=False,
                      first_ts='2026-09-03T10:00:00+00:00', project_label='app', models=['gpt-5'], requests=1, subagents=0,
                      total_tokens=1000, resume=None)
        result = {'prompts': [prompt], 'total_prompts': 1}
        key = ('codex', UUID, 't1')
        out = cli.render_top(result, {key: 'hello'}, {key: {'cwd': '/w/app'}})
        self.assertIn(f'    resume: cd /w/app && codex resume {UUID}', out)
        self.assertIn(f'    resume: codex resume {UUID}', cli.render_top(result))  # always printed; Codex needs no directory
        self.assertIn(f'    resume: codex resume {UUID}', cli.render_top(result, {key: 'hello'}))
        hostile = dict(prompt, harness='claude', session='ok; touch /tmp/pwn', resume=None)
        self.assertNotIn('pwn', cli.render_top({'prompts': [hostile], 'total_prompts': 1}, None, {('claude', 'ok; touch /tmp/pwn', 't1'): {'cwd': '/w'}}))


if __name__ == '__main__':
    unittest.main()
