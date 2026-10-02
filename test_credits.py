"""ChatGPT credit equivalents: the rate math against the eight published rows, aliases, left-out rows, the insights fact, `top` and the payload."""
import json
import tempfile
import unittest
from pathlib import Path

from tokenatlas import credits, insights, pricing, prompts
from tokenatlas.__main__ import render_top
from tokenatlas.report import build_report
from test_insights import ob

M = 1_000_000
TABLE = credits.load_credits()
# model id: (input, cached input, output) credits per 1M tokens, as printed on https://learn.chatgpt.com/docs/pricing (checked 2026-10-02)
CARD = {'gpt-6-astra': (250, 25, 1250), 'gpt-6.1-sol': (50, 2.5, 250), 'gpt-6-sol': (50, 5, 250), 'gpt-6-luna': (2.5, .25, 12.5),
        'gpt-5.6-sol': (100, 10, 500), 'gpt-5.6-terra': (50, 5, 300), 'gpt-5.6-luna': (5, .5, 30), 'gpt-5.5': (125, 12.5, 750)}


def row(model='gpt-5.5', provider='openai', fresh=0, read=0, out=0, **kw):
    return ob(kw.pop('i', 'x'), model=model, provider=provider, fresh=fresh, read=read, out=out, **kw)


class Rates(unittest.TestCase):
    def test_table_is_the_eight_rows_with_provenance(self):
        self.assertEqual({e['model']: (e['input'], e['cached_input'], e['output']) for e in TABLE['models']}, CARD)
        self.assertEqual((TABLE['retrieved_on'], TABLE['source_url']), ('2026-10-02', 'https://learn.chatgpt.com/docs/pricing'))

    def test_math_for_every_row(self):
        for model, (i, c, o) in CARD.items():
            with self.subTest(model):
                r = credits.credit_observation(row(model, fresh=2 * M, read=4 * M, out=M), TABLE)
                self.assertEqual(r['status'], 'credited')
                self.assertAlmostEqual(r['credits'], 2 * i + 4 * c + o)
                self.assertEqual(credits.credit_vector(row(model, fresh=1), TABLE), [i, c, o])

    def test_reasoning_is_not_added_again(self):
        o = row('gpt-5.5', out=M)
        o['tokens']['reasoning'] = M // 2  # a subset of output
        self.assertAlmostEqual(credits.credit_observation(o, TABLE)['credits'], 750)

    def test_provider_alias_and_other_providers(self):
        self.assertAlmostEqual(credits.credit_observation(row('gpt-5.5', 'openai-codex', out=M), TABLE)['credits'], 750)
        for provider in ('anthropic', 'openrouter', 'm5'):
            self.assertEqual(credits.credit_observation(row('gpt-5.5', provider, out=M), TABLE)['status'], 'other_provider')
        self.assertIsNone(credits.credit_vector(row('claude-x', 'anthropic', out=1), TABLE))

    def test_unknown_model_and_cache_write_have_no_rate(self):
        r = credits.credit_observation(row('gpt-9-mystery', out=M), TABLE)
        self.assertEqual((r['status'], r['credits'], r['model']), ('unrated', None, 'gpt-9-mystery'))
        self.assertEqual(credits.credit_observation(dict(row(out=1), model=None), TABLE)['status'], 'unrated')
        self.assertEqual(credits.credit_observation(dict(row(out=1), tokens=dict(fresh_input=0, cache_read=0, cache_write=5, output=1)), TABLE)['status'], 'cache_write')

    def test_unknown_cache_write_counter_is_not_zero(self):
        r = credits.credit_observation(dict(row(out=0), tokens=dict(fresh_input=1_000_000, cache_read=0, cache_write=None, output=0)), TABLE)
        self.assertEqual((r['status'], r['credits']), ('partial', None))
        self.assertIsNone(credits.credit_vector(dict(row(out=0), tokens=dict(fresh_input=1_000_000, cache_read=0, cache_write=None, output=0)), TABLE))

    def test_unknown_token_counts_give_no_credits(self):
        r = credits.credit_observation(dict(row(out=1), tokens=dict(fresh_input=None, cache_read=0, cache_write=0, output=1)), TABLE)
        self.assertEqual((r['status'], r['credits']), ('partial', None))

    def test_speed_not_recorded_standard_fast_and_others(self):
        base = row('gpt-5.5', out=M)
        self.assertEqual(credits.credit_observation(base, TABLE)['assumptions'], [credits.ASSUMED_STANDARD])
        std = credits.credit_observation(dict(base, tariff={'speed': 'standard'}), TABLE)
        self.assertEqual((std['credits'], std['assumptions']), (750, []))
        for tariff in ({'speed': 'fast'}, {'service_tier': 'fast'}):
            fast = credits.credit_observation(dict(base, tariff=tariff), TABLE)
            self.assertEqual((fast['credits'], fast['assumptions']), (1500, [credits.FAST]))
            self.assertEqual(credits.credit_vector(dict(base, tariff=tariff), TABLE), [250, 25, 1500])
        for tariff in ({'service_tier': 'priority'}, {'speed': 'ultrafast'}, {'speed': 'fast', 'service_tier': 'priority'}):
            r = credits.credit_observation(dict(base, model='gpt-6-astra', tariff=tariff), TABLE)
            self.assertEqual((r['status'], r['credits']), ('nonstandard', None), tariff)
            self.assertIsNone(credits.credit_vector(dict(base, tariff=tariff), TABLE))

    def test_table_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            for patch, message in ((dict(schema=2), 'schema'), (dict(models=[{'provider': 'openai', 'model': 'm'}]), 'm: missing key'),
                                   (dict(models=[dict(TABLE['models'][0], output=-1)]), 'output'), (dict(fast_multiplier=0), 'fast_multiplier'), (dict(fast_multiplier=True), 'fast_multiplier'),
                                   (dict(provider_aliases=[]), 'provider_aliases'), (dict(provider_aliases={'a': 1}), 'provider_aliases'),
                                   (dict(unit='tokens'), 'unit'), (dict(speed='fast'), 'speed'),
                                   (dict(models=[dict(TABLE['models'][0], model='')]), 'non-empty'), (dict(models=[dict(TABLE['models'][0], model=5)]), 'non-empty'),
                                   (dict(models=[TABLE['models'][0], TABLE['models'][0]]), 'duplicate')):
                path = Path(tmp) / 'c.json'
                path.write_text(json.dumps(dict(TABLE, **patch)))
                with self.assertRaisesRegex(ValueError, message):
                    credits.load_credits(path)

    def test_fmt(self):
        self.assertEqual([credits.fmt(x) for x in (1234.4, 99.96, 12.34, .456)], ['1,234', '100.0', '12.3', '0.46'])
        # ties are rounded half away from zero on the exact value, as the page's Intl.NumberFormat does (not half to even)
        self.assertEqual([credits.fmt(x) for x in (1.25, 100.5, .125, 2.5 + 100, 0)], ['1.3', '101', '0.13', '103', '0.00'])


class Fact(unittest.TestCase):
    def fact(self, rows, **kw):
        res = insights.cost_facts(rows, pricing.load_prices(), **kw)
        return next((f for f in res['facts'] if f['id'] == 'credits'), None)

    def test_sums_credited_rows_and_leaves_the_rest_out_by_name(self):
        rows = [row('gpt-5.5', fresh=M, i='a'), row('gpt-5.6-luna', 'openai-codex', out=M, i='b'), row('claude-x', 'anthropic', fresh=M, i='c'),
                row('gpt-9-mystery', fresh=M, i='d'), row('gpt-5.5', out=M, tariff={'service_tier': 'priority'}, i='e'),
                dict(row('gpt-5.5', i='f'), tokens=dict(fresh_input=None, cache_read=0, cache_write=0, output=1)), row('gpt-5.5', out=M, tariff={'speed': 'fast'}, i='g')]
        f = self.fact(rows)
        v = f['values']
        self.assertAlmostEqual(v['credits'], 125 + 30 + 1500)
        self.assertEqual((v['credited_requests'], v['openai_requests'], v['unrated_requests'], v['nonstandard_requests'], v['unknown_token_requests']), (3, 6, 1, 1, 1))
        self.assertEqual(v['unrated'], [dict(name='gpt-9-mystery', requests=1)])
        self.assertEqual(v['nonstandard'], {'service_tier=priority': 1})
        self.assertEqual(v['cache_write_requests'], 0)
        self.assertEqual(f['provenance'], 'computed')
        self.assertFalse(v['lower_bound'])
        joined = ' '.join(f['assumptions'])
        for needle in ('standard speed', 'corresponds to', 'not what was drawn', 'legacy rate card', 'depends on the plan', '2026-10-02', 'Fast mode, counted at a higher'):
            self.assertIn(needle, joined)
        self.assertNotIn('{', f['computation'] + joined)

    def test_cache_write_rows_have_their_own_exclusion(self):
        w = lambda i, model='gpt-5.5': dict(row(model, i=i), tokens=dict(fresh_input=M, cache_read=0, cache_write=7, output=0))
        f = self.fact([row('gpt-5.5', fresh=M, i='a'), w('b'), w('c'), row('gpt-9-mystery', fresh=1, i='d')])
        v = f['values']
        self.assertEqual((v['cache_write_requests'], v['unrated_requests'], v['credited_requests'], v['openai_requests']), (2, 1, 1, 4))
        self.assertEqual(v['unrated'], [dict(name='gpt-9-mystery', requests=1)])  # a rated model with cache writes is not 'no rate for the model'
        text = insights.render_text({'window': {'start': None, 'end': None}, 'requests': 4, 'priced_requests': 0, 'unpriced_requests': 4,
                                     'ambiguous_requests': 0, 'incomplete_requests': 0, 'facts': [f]})
        self.assertIn('left out, requests with cache writes (no credit rate): 2', text)
        self.assertIn('left out, no credit rate for the model: 1 (gpt-9-mystery 1)', text)
        self.assertIn('cache writes', ' '.join(f['assumptions']))

    def test_no_fact_without_openai_rows(self):
        self.assertIsNone(self.fact([row('claude-x', 'anthropic', fresh=M)]))

    def test_incomplete_observation_is_a_lower_bound(self):
        f = self.fact([row('gpt-5.5', fresh=M, i='a'), dict(row('gpt-5.5', fresh=M, i='b'), complete=False)])
        self.assertTrue(f['values']['lower_bound'])
        self.assertIn('lower bounds', ' '.join(f['assumptions']))
        text = insights.render_text({'window': {'start': None, 'end': None}, 'requests': 2, 'priced_requests': 2, 'unpriced_requests': 0,
                                     'ambiguous_requests': 0, 'incomplete_requests': 1, 'facts': [f]})
        self.assertIn('credit equivalent: ≥ 250 credits', text)

    def test_text_en(self):
        f = self.fact([row('gpt-5.5', fresh=M, i='a'), row('gpt-9-mystery', fresh=1, i='b')])
        text = insights.render_text({'window': {'start': None, 'end': None}, 'requests': 2, 'priced_requests': 1, 'unpriced_requests': 1,
                                     'ambiguous_requests': 0, 'incomplete_requests': 0, 'facts': [f]})
        for line in ('ChatGPT credits (Codex) [computed]', 'gpt-5.5: ≈ 125 credits, 1 request', 'credit equivalent: ≈ 125 credits over 1 of 2 requests from OpenAI',
                     'left out, no credit rate for the model: 1 (gpt-9-mystery 1)'):
            self.assertIn(line, text)

    def test_text_sv_has_every_key_and_placeholder(self):
        texts = json.loads((Path(insights.__file__).with_name('report_i18n.json')).read_text(encoding='utf-8'))
        import re
        keys = [k for k in texts['en'] if k.startswith(('ins_credits', 'ins_a_credit', 'ins_pa_credit', 'ins_l_credit', 'ins_v_credit')) or k in ('ins_l_credits', 'cr_n')]
        self.assertGreaterEqual(len(keys), 14)
        for k in keys:
            self.assertIn(k, texts['sv'], k)
            self.assertEqual(set(re.findall(r'\{(\w+)\}', texts['sv'][k])), set(re.findall(r'\{(\w+)\}', texts['en'][k])), k)
        self.assertEqual(texts['sv']['ins_l_credit_write'], 'Anrop med cacheskrivning (saknar kreditpris)')
        self.assertEqual(texts['en']['ins_l_credit_write'], 'Requests with cache writes (no credit rate)')
        self.assertIn('cacheskrivning', texts['sv']['ins_a_credit_standard'])
        self.assertIn('krediter', texts['sv']['cr_n'])
        self.assertIn('inte vad som dragits', texts['sv']['ins_a_credit_notdrawn'])


class Turns(unittest.TestCase):
    def rows(self):
        return [row('gpt-5.5', fresh=M, i='1', turn='t1', ts='2026-09-03T10:00:00+00:00'), row('gpt-5.5', out=M, i='2', turn='t1', ts='2026-09-03T10:01:00+00:00'),
                row('gpt-5.5', fresh=M, i='3', turn='t2', ts='2026-09-03T11:00:00+00:00'), row('claude-x', 'anthropic', fresh=M, i='4', turn='t2', ts='2026-09-03T11:01:00+00:00'),
                dict(row('gpt-5.5', fresh=M, i='5', turn='t3', ts='2026-09-03T12:00:00+00:00'), complete=False)]

    def test_credits_only_when_every_request_has_a_rate(self):
        res = prompts.top_prompts(self.rows(), pricing.load_prices(), 10)
        got = {p['turn_id']: (p['credits'], p['credits_lower_bound']) for p in res['prompts']}
        self.assertEqual(got, {'t1': (875, False), 't2': (None, False), 't3': (125, True)})

    def test_top_output(self):
        res = prompts.top_prompts(self.rows(), pricing.load_prices(), 10)
        out = render_top(res)
        self.assertIn('≈ 875 credits', out)
        self.assertIn('≥ 125 credits', out)
        self.assertEqual(sum('credits' in line for line in out.splitlines()), 2)  # the mixed-provider turn shows none


class Payload(unittest.TestCase):
    def test_columns_carry_credit_classes_only_for_rated_rows(self):
        rows = [row('gpt-5.5', fresh=1, i='a'), row('gpt-5.5', fresh=1, i='b', tariff={'speed': 'fast'}), row('claude-x', 'anthropic', fresh=1, i='c'), row('gpt-5.5', 'openai-codex', fresh=1, i='d', ts='2026-09-03T11:00:00+00:00')]
        c = build_report(rows, {}, redact=False)['columns']
        self.assertEqual(c['credit_classes'], [[125, 12.5, 750], [250, 25, 1500]])
        self.assertEqual(c['credit'], [0, 1, None, 0])


if __name__ == '__main__':
    unittest.main()
