"""Write a synthetic private report with stored prompt previews, for the browser test: python scripts/prompts_fixture.py OUT.html"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tokenatlas.report import build_report, render_report, write_report  # noqa: E402


def obs(i, minute, turn, kind='main', agent='main', model='claude-sonnet-4-5', fresh=1000000, session='s1'):
    return dict(id=f'o{i}', harness='claude', session=session, agent=agent, thread_kind=kind, parent_session=session if kind == 'subagent' else None,
                turn_id=turn, turn_confidence='derived', ts=f'2026-09-03T10:{minute:02d}:00+00:00', model=model, provider='anthropic',
                machine='m', project_id='/w/app', project_label='app', effort=None, origin='cli', raw_usage={}, tariff=None,
                tokens=dict(fresh_input=fresh, cache_write=0, cache_read=0, output=0, reasoning=0), complete=True,
                id_synthetic=False, warnings=[], sources=[])


def main(out):
    records = [obs(1, 0, 't1'), obs(2, 5, None, 'subagent', 'a1', model='mystery-model'), obs(3, 10, 't2', fresh=3000000),
               obs(4, 20, 't3', model='mystery-model'), obs(5, 30, 't4', fresh=200000)]
    texts = {('claude', 's1', 't1'): 'Fix the <b>failing</b> build', ('claude', 's1', 't2'): 'Refactor the importer'}
    write_report(out, render_report(build_report(records, {}, redact=False, prompt_texts=texts)))


if __name__ == '__main__':
    main(sys.argv[1])
