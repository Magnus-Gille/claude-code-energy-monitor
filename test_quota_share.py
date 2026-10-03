"""Share of the weekly / 5-hour limit per turn and per window (issue #90)."""
import unittest
from datetime import datetime, timedelta, timezone

from tokenatlas import limits, pricing, prompts, quota_share as qs

T0 = datetime(2026, 9, 3, 10, tzinfo=timezone.utc)
TABLE = pricing.load_prices()
RESET = (T0 + timedelta(days=3)).isoformat()
RESET5 = (T0 + timedelta(hours=4)).isoformat()


def iso(minutes, seconds=0):
    return (T0 + timedelta(minutes=minutes, seconds=seconds)).isoformat()


def quota(week=None, five=None, reached=None, resets=RESET, resets5=RESET5, plan='pro', limit_id='codex'):
    windows = []
    if five is not None:
        windows.append({'slot': 'primary', 'minutes': 300, 'used_percent': float(five), 'resets_at': resets5})
    if week is not None:
        windows.append({'slot': 'secondary', 'minutes': 10080, 'used_percent': float(week), 'resets_at': resets})
    return {'limit_id': limit_id, 'plan_type': plan, 'reached': reached, 'windows': windows}


def req(id, ts, turn='t1', session='s1', out=1000, **kw):
    return dict(id=id, ts=ts, harness='codex', provider='openai', machine='m', session=session, turn_id=turn, thread_kind=kw.pop('thread_kind', 'main'), agent='main',
                model=kw.pop('model', 'gpt-5.5'), complete=kw.pop('complete', True), id_synthetic=kw.pop('id_synthetic', False), effort=None, origin=None, turn_confidence='observed', parent_session=kw.pop('parent_session', None),
                project_id=None, project_label=None, cwd=None, warnings=[], sources=[], raw_usage={}, tariff=None,
                tokens=dict(fresh_input=0, cache_read=0, cache_write=0, output=out, reasoning=0), quota=kw.pop('q', None), **kw)


def shares(records):
    snaps = qs.snapshots_from_records(records)
    return snaps, qs.largest(qs.turn_shares(records, snaps, TABLE))


K1, K2 = ('codex', 's1', 't1'), ('codex', 's2', 't2')


class Snapshots(unittest.TestCase):
    def test_one_per_window_skipping_rejected_and_unreset(self):
        rejected = req('x', iso(1), q={**quota(week=5), 'status': 'rejected'})
        noreset = req('y', iso(2), q={'limit_id': 'codex', 'plan_type': 'pro', 'reached': None,
                                      'windows': [{'slot': 'primary', 'minutes': 300, 'used_percent': 1.0, 'resets_at': None}]})
        both = req('z', iso(3), q=quota(week=5, five=2))
        snaps = qs.snapshots_from_records([rejected, noreset, both, req('plain', iso(4))])
        self.assertEqual(sorted(s['window'][0] for s in snaps), [300, 10080])
        self.assertEqual({(s['harness'], s['account'], s['plan_type'], s['turn']) for s in snaps}, {('codex', 'codex', 'pro', K1)})

    def test_resets_at_drift_is_one_window_instance_and_a_real_drop_is_a_new_one(self):
        a = (datetime.fromisoformat(RESET) + timedelta(seconds=3)).isoformat()
        b = (datetime.fromisoformat(RESET) + timedelta(days=7)).isoformat()
        recs = [req('a', iso(1), q=quota(week=10)), req('b', iso(2), q=quota(week=11, resets=a)), req('c', iso(3), q=quota(week=2, resets=b))]
        snaps = qs.snapshots_from_records(recs)
        self.assertEqual([s['window'][1] for s in snaps], [a, a, b])  # the latest reset time reported is the instance's context
        self.assertEqual(len(qs.windows(snaps)), 2)

    def test_account_falls_back_to_harness(self):
        snaps = qs.snapshots_from_records([req('a', iso(1), q=quota(week=5, limit_id=None))])
        self.assertEqual(snaps[0]['account'], 'codex')

    def test_windows_peak_hit_and_credits(self):
        recs = [req('a', iso(1), q=quota(week=10)), req('b', iso(2), q=quota(week=100, reached='rate_limit_reached')),
                req('c', iso(3), q=quota(week=95)), req('d', iso(4), q=quota(week=5, resets='2026-09-20T00:00:00+00:00', reached='workspace_owner_credits_depleted'))]
        ws = qs.windows(qs.snapshots_from_records(recs))
        self.assertEqual(len(ws), 2)
        first = ws[0]
        self.assertEqual((first['minutes'], first['peak_percent'], first['peak_at'], first['first_percent'], first['snapshots'], first['hit']),
                         (10080, 100.0, iso(2), 10.0, 3, True))
        self.assertEqual(first['start'], (datetime.fromisoformat(RESET) - timedelta(minutes=10080)).isoformat())
        self.assertFalse(ws[1]['hit'])  # depleted credits are not a hit of the window

    def test_reached_marks_only_the_window_that_is_full(self):
        recs = [req('a', iso(1), q=quota(week=40, five=100, reached='rate_limit_reached'))]
        by = {w['minutes']: w['hit'] for w in qs.windows(qs.snapshots_from_records(recs))}
        self.assertEqual(by, {300: True, 10080: False})
        both = [req('a', iso(1), q=quota(week=100, five=100, reached='rate_limit_reached'))]
        self.assertEqual({w['hit'] for w in qs.windows(qs.snapshots_from_records(both))}, {None})  # ambiguous: which window was reached is unknown
        none = [req('a', iso(1), q=quota(week=40, five=60, reached='rate_limit_reached'))]
        self.assertEqual({w['hit'] for w in qs.windows(qs.snapshots_from_records(none))}, {False})

    def test_window_cost_excludes_ambiguous_identities(self):
        recs = [req('a', iso(1), q=quota(week=10), out=1_000_000), req('b', iso(2), q=quota(week=12), out=1_000_000, id_synthetic=True)]
        cost = lambda r: prompts._cost(r, TABLE)
        both = qs.windows(qs.snapshots_from_records(recs), records=recs, cost_of=cost)[0]
        one = qs.windows(qs.snapshots_from_records(recs[:1]), records=recs[:1], cost_of=cost)[0]
        self.assertAlmostEqual(both['cost'], one['cost'])
        self.assertGreater(both['cost'], 0)

    def test_last_keeps_the_most_recent_per_length(self):
        recs = [req(str(i), iso(i * 20000), q=quota(week=i, resets=(T0 + timedelta(days=3 + 7 * i)).isoformat())) for i in range(5)]  # more than a window apart
        ws = qs.windows(qs.snapshots_from_records(recs), last=2)
        self.assertEqual([w['first_percent'] for w in ws], [3.0, 4.0])

    def test_window_cost(self):
        recs = [req('a', iso(1), q=quota(week=10), out=1_000_000), req('b', iso(2), q=quota(week=12), out=1_000_000)]
        ws = qs.windows(qs.snapshots_from_records(recs), records=recs, cost_of=lambda r: prompts._cost(r, TABLE))
        self.assertGreater(ws[0]['cost'], 0)
        self.assertEqual(ws[0]['unpriced_requests'], 0)
        self.assertNotIn('key', ws[0])


class Shares(unittest.TestCase):
    def test_single_turn_observed(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=10)), req('a', iso(10), q=quota(week=11)), req('b', iso(11), q=quota(week=13))]
        _, got = shares(recs)
        s = got[K1]
        self.assertEqual((s['label'], s['estimate']), ('observed', None))
        self.assertEqual(s['observed'], dict(before=10.0, after=13.0, delta=3.0, shared_with=0))
        self.assertEqual(s['window_key'][2:4], (10080, RESET))
        self.assertEqual(qs.text(s), '~3% of weekly Codex limit')
        self.assertEqual(qs.as_json(s), dict(window_minutes=10080, delta_percent=3.0, label='observed', before=10.0, after=13.0, shared_with=0))

    def test_zero_delta_is_less_than_one_percent(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=10)), req('a', iso(10), q=quota(week=10))]
        s = shares(recs)[1][K1]
        self.assertEqual(s['observed']['delta'], 0.0)
        self.assertEqual(qs.text(s), '< 1% of weekly Codex limit')
        self.assertNotIn('.', qs.percent_text(s))

    def test_before_comes_from_any_session(self):
        recs = [req('0', iso(0), turn='t0', session='other', q=quota(week=40)), req('a', iso(10), q=quota(week=42))]
        self.assertEqual(shares(recs)[1][K1]['observed']['before'], 40.0)

    def test_estimates_over_the_same_interval_sum_to_the_observed_movement(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
                req('a1', iso(10), turn='t1', session='s1', out=1_000_000, q=quota(week=11)),
                req('b1', iso(10), turn='t2', session='s2', out=3_000_000, q=quota(week=12)),
                req('a2', iso(12), turn='t1', session='s1', out=1_000_000, q=quota(week=18)),
                req('b2', iso(12), turn='t2', session='s2', out=3_000_000, q=quota(week=18))]
        got = shares(recs)[1]
        self.assertEqual((got[K1]['label'], got[K1]['observed']['shared_with'], got[K2]['observed']['shared_with']), ('estimate', 1, 1))
        self.assertAlmostEqual(got[K1]['estimate'] + got[K2]['estimate'], 8.0, places=6)
        self.assertAlmostEqual(got[K1]['estimate'] / got[K2]['estimate'], 1 / 3, places=6)
        self.assertEqual(qs.text(got[K2]), '≈6% of weekly Codex limit (estimate)')

    def test_staggered_turns_overlap_so_they_estimate_and_conserve(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
                req('a1', iso(10), turn='t1', session='s1', out=1_000_000, q=quota(week=11)),
                req('b1', iso(11), turn='t2', session='s2', out=1_000_000, q=quota(week=17)),
                req('a2', iso(12), turn='t1', session='s1', out=1_000_000, q=quota(week=18))]
        got = qs.largest(qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE))
        a, b = got[K1], got[K2]
        self.assertEqual((a['label'], b['label']), ('estimate', 'estimate'))  # b ran inside a's span
        self.assertEqual((a['observed']['shared_with'], b['observed']['shared_with']), (1, 1))  # symmetric
        self.assertAlmostEqual(a['estimate'], 5.0)  # 1 alone, half of the 6, 1 alone
        self.assertAlmostEqual(b['estimate'], 3.0)
        self.assertAlmostEqual(a['estimate'] + b['estimate'], 8.0)  # all of the window's movement, once

    def test_a_millisecond_shift_does_not_flip_the_label(self):
        def build(shift):
            return [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
                    req('a1', iso(10), turn='t1', session='s1', out=1_000_000, q=quota(week=11)),
                    req('b1', iso(10, shift), turn='t2', session='s2', out=3_000_000, q=quota(week=12)),
                    req('a2', iso(12), turn='t1', session='s1', out=1_000_000, q=quota(week=18)),
                    req('b2', iso(12), turn='t2', session='s2', out=3_000_000, q=quota(week=18))]
        for shift in (0, 0.001, 0.5):
            recs = build(shift)
            got = qs.largest(qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE))
            self.assertEqual((got[K1]['label'], got[K2]['label']), ('estimate', 'estimate'), shift)
            self.assertAlmostEqual(got[K1]['estimate'] + got[K2]['estimate'], 8.0)

    def test_a_step_shared_by_two_turns_is_split_by_cost_and_conserved(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
                req('a1', iso(10), turn='t1', session='s1', out=1_000_000, q=quota(week=12)),
                req('b1', iso(10), turn='t2', session='s2', out=1_000_000, q=quota(week=12)),
                req('a2', iso(11), turn='t1', session='s1', out=1_000_000, q=quota(week=16))]
        got = qs.largest(qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE))
        a, b = got[K1], got[K2]
        self.assertEqual((a['label'], b['label']), ('estimate', 'estimate'))
        self.assertAlmostEqual(a['observed']['delta'] + b['observed']['delta'], 6.0)  # 2 shared (1 + 1) and 4 alone for a, all of the 6 allocated once
        self.assertAlmostEqual(b['estimate'], 1.0)

    def test_decrease_then_recovery_inside_a_turn_is_unknown(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)), req('a', iso(10), q=quota(week=11)),
                req('b', iso(11), q=quota(week=2)), req('c', iso(12), q=quota(week=12))]
        self.assertEqual(shares(recs)[1][K1]['label'], 'unknown')

    def test_a_stale_lower_reading_is_not_a_reset(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)), req('a', iso(10), q=quota(week=12)),
                req('b', iso(11), q=quota(week=11)), req('c', iso(12), q=quota(week=14))]
        s = shares(recs)[1][K1]
        self.assertEqual((s['label'], s['observed']['delta'], s['observed']['after']), ('observed', 4.0, 14.0))

    def orchestration(self, other_account=False):
        """A parent turn with three parallel subagent sessions whose counters arrive interleaved and out of order (10 -> 18 overall)."""
        sub = lambda n: dict(session=f'sub{n}', turn=None, thread_kind='subagent', parent_session='s1')
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
                req('m1', iso(10), out=1_000_000, q=quota(week=11)),
                req('a1', iso(11), out=1_000_000, q=quota(week=12), **sub(1)),
                req('b1', iso(12), out=1_000_000, q=quota(week=11), **sub(2)),  # stale
                req('c1', iso(13), out=1_000_000, q=quota(week=14), **sub(3)),
                req('m2', iso(14), out=1_000_000, q=quota(week=13)),  # stale
                req('a2', iso(15), out=1_000_000, q=quota(week=16), **sub(1)),
                req('b2', iso(16), out=1_000_000, q=quota(week=15), **sub(2)),  # stale
                req('m3', iso(17), out=1_000_000, q=quota(week=18))]
        if other_account:  # another account's counter (same limit id, plan and reset time) near its own limit
            recs += [req(f'x{i}', iso(10 + i, 30), turn='t9', session='s9', out=1000, q=quota(week=98 + i % 2)) for i in range(20)]
        return sorted(recs, key=lambda r: r['ts'])

    def test_a_parent_turn_gets_the_movement_its_subagents_cause(self):
        recs = self.orchestration()
        got = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)
        parent = got[K1][10080]
        self.assertEqual((parent['label'], parent['observed']['delta']), ('observed', 8.0))
        self.assertEqual(sum(x[10080]['observed']['delta'] for k, x in got.items() if x[10080]['observed']), 8.0)  # conserved

    def test_an_interleaved_second_counter_does_not_take_the_movement(self):
        recs = self.orchestration(other_account=True)
        snaps = qs.snapshots_from_records(recs)
        got = qs.turn_shares(recs, snaps, TABLE)
        self.assertEqual((got[K1][10080]['label'], got[K1][10080]['observed']['delta']), ('observed', 8.0))
        self.assertEqual(len(qs.windows(snaps)), 2)  # two counters, two windows

    def test_a_lone_jump_in_one_session_stays_on_the_same_counter(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=10)), req('a', iso(1), turn='t0', q=quota(week=10)),
                req('b', iso(20), turn='t1', q=quota(week=25)), req('c', iso(21), turn='t1', q=quota(week=26))]
        got = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)
        s = got[('codex', 's1', 't1')][10080]
        self.assertEqual((s['label'], s['observed']['delta']), ('observed', 16.0))

    def test_sequential_sessions_with_a_jump_share_a_counter(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)), req('a', iso(1), turn='t0', session='s0', q=quota(week=11)),
                req('b', iso(60), turn='t1', session='s1', q=quota(week=30)), req('c', iso(61), turn='t1', session='s1', q=quota(week=31))]
        snaps = qs.snapshots_from_records(recs)
        self.assertEqual(len({s['key'] for s in snaps}), 1)
        got = qs.turn_shares(recs, snaps, TABLE)
        self.assertEqual(got[('codex', 's1', 't1')][10080]['observed']['delta'], 20.0)  # 11 -> 31

    def test_window_lower_bound_for_incomplete_counters(self):
        recs = [req('a', iso(1), q=quota(week=10), out=1_000_000), req('b', iso(2), q=quota(week=12), out=1_000_000, complete=False)]
        w = qs.windows(qs.snapshots_from_records(recs), records=recs, cost_of=lambda r: prompts._cost(r, TABLE))[0]
        self.assertTrue(w['lower_bound'])

    def test_unattributed_usage_takes_its_movement(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
                req('a1', iso(10), out=1000, q=quota(week=11)),
                req('u1', iso(11), turn=None, session='su', out=10_000_000, q=quota(week=16)),  # no turn owns this session
                req('a2', iso(12), out=1000, q=quota(week=17))]
        got = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)
        a = got[K1][10080]
        self.assertEqual(a['label'], 'estimate')
        self.assertTrue(2.0 <= a['estimate'] < 2.1, a['estimate'])  # 1 + 1 alone, almost nothing of the unattributed 5
        self.assertNotIn(('codex', 'su', None), got)  # a pseudo-turn is never returned

    def test_a_sequential_jump_inside_the_conflict_interval_stays_on_one_counter(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10))]
        recs += [req(f'a{i}', iso(i), turn='t0', session='s0', q=quota(week=10)) for i in range(1, 6)]
        recs += [req('b1', iso(6), turn='t1', session='s1', q=quota(week=25)), req('b2', iso(7), turn='t1', session='s1', q=quota(week=25))]
        snaps = qs.snapshots_from_records(recs)
        self.assertEqual(len({s['key'] for s in snaps}), 1)
        got = qs.turn_shares(recs, snaps, TABLE)[('codex', 's1', 't1')][10080]
        self.assertEqual((got['label'], got['observed']['delta']), ('observed', 15.0))

    def test_requests_without_a_snapshot_count_for_endpoints_and_competitors(self):
        end = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)), req('x1', iso(10), q=quota(week=11)),
               req('x2', iso(12), q=quota(week=14)), req('x3', iso(14))]  # the last request has no snapshot
        s = qs.turn_shares(end, qs.snapshots_from_records(end), TABLE)[K1][10080]
        self.assertEqual((s['label'], s['estimate']), ('estimate', 4.0))
        gap = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)), req('a1', iso(10), out=1_000_000, q=quota(week=11)),
               req('c1', iso(12), turn='t2', session='s2', out=2_000_000),  # a competitor that reports no quota
               req('a2', iso(14), out=1_000_000, q=quota(week=19))]
        got = qs.turn_shares(gap, qs.snapshots_from_records(gap), TABLE)
        a = got[K1][10080]
        self.assertEqual(a['label'], 'estimate')
        self.assertEqual(a['observed']['shared_with'], 1)
        self.assertTrue(1.0 < a['estimate'] < 8.0, a['estimate'])  # the competitor took part of the 7 points
        self.assertNotIn(K2, got)  # nothing to show for it

    def test_a_sliding_reset_time_is_one_window_instance(self):
        slide = lambda n: (datetime.fromisoformat(RESET) + timedelta(minutes=20 * n)).isoformat()
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)), req('a1', iso(10), q=quota(week=11, resets=slide(1))),
                req('a2', iso(20), q=quota(week=12, resets=slide(2)))]
        snaps = qs.snapshots_from_records(recs)
        self.assertEqual(len({s['key'] for s in snaps}), 1)
        got = qs.turn_shares(recs, snaps, TABLE)[K1][10080]
        self.assertEqual((got['label'], got['observed']['delta']), ('observed', 2.0))
        self.assertEqual(qs.windows(snaps)[0]['resets_at'], slide(2))  # the latest, as context

    def test_zero_of_one_limit_does_not_hide_an_unknown_one(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=10, five=20)), req('a', iso(10), q=quota(week=10, five=20))]
        per = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)[K1]
        per[300] = dict(per[300], observed=None, estimate=None, label='unknown')
        self.assertEqual(qs.largest({K1: per})[K1]['label'], 'unknown')

    def test_sessions_converging_after_a_reset_are_two_windows_and_no_split(self):
        def at(minutes, seconds=0):
            return iso(minutes, seconds)
        recs = [req('x0', at(0), turn='tx0', session='sx', q=quota(week=97)), req('y0', at(1), turn='ty0', session='sy', q=quota(week=98)),
                req('x1', at(2), turn='tx0', session='sx', q=quota(week=98)), req('y1', at(3), turn='ty0', session='sy', q=quota(week=98)),
                req('y2', at(10), turn='ty1', session='sy', q=quota(week=1)),
                req('x2', at(10, 10), turn='tx1', session='sx', q=quota(week=99)),  # a slower session still on the old counter
                req('y3', at(10, 20), turn='ty1', session='sy', q=quota(week=2)),
                req('x3', at(10, 30), turn='tx1', session='sx', q=quota(week=99)),
                req('x4', at(13), turn='tx1', session='sx', q=quota(week=3)), req('y4', at(14), turn='ty1', session='sy', q=quota(week=4))]
        snaps = qs.snapshots_from_records(recs)
        self.assertEqual({s['key'][5] for s in snaps}, {0})  # the stragglers were no evidence of a second account
        self.assertEqual(len(qs.windows(snaps)), 2)
        self.assertEqual([s['key'][6] for s in snaps if s['ts'] in (at(10, 10), at(10, 30))], [0, 0])  # they stay with the old instance

    def test_a_low_usage_reset_is_a_new_window_even_with_a_small_drop(self):
        reset = (datetime.fromisoformat(RESET5) + timedelta(minutes=300)).isoformat()
        recs = [req('0', iso(0), turn='t0', q=quota(five=4)), req('0b', iso(120), turn='t0', q=quota(five=4)),
                req('a', iso(200), q=quota(five=1, resets5=reset)), req('b', iso(210), q=quota(five=2, resets5=reset))]
        snaps = qs.snapshots_from_records(recs)
        self.assertEqual(len(qs.windows(snaps)), 2)
        got = qs.turn_shares(recs, snaps, TABLE)[K1][300]
        self.assertEqual((got['label'], got['observed']), ('unknown', None))  # not an observed 0% against the old window's 4%
        slide = lambda n: (datetime.fromisoformat(RESET5) + timedelta(minutes=2 * n)).isoformat()
        drift = [req('0', iso(0), turn='t0', q=quota(five=4)), req('a', iso(10), q=quota(five=3, resets5=slide(1))), req('b', iso(20), q=quota(five=4, resets5=slide(2)))]
        self.assertEqual(len(qs.windows(qs.snapshots_from_records(drift))), 1)  # drift of minutes is not a reset

    def test_window_cost_includes_quota_less_requests_of_reporting_sessions(self):
        recs = [req('a', iso(1), q=quota(week=10), out=1_000_000), req('b', iso(2), out=1_000_000),  # same session, no snapshot
                req('c', iso(3), q=quota(week=12), out=1_000_000),
                req('d', iso(2, 30), turn='t9', session='s9', out=1_000_000)]  # a session that never reported a window
        cost = lambda r: prompts._cost(r, TABLE)
        w = qs.windows(qs.snapshots_from_records(recs), records=recs, cost_of=cost)[0]
        one = cost(recs[0])
        self.assertAlmostEqual(w['cost'], 3 * one)  # a, b and c; not d
        self.assertEqual((w['uncertain_requests'], w['lower_bound']), (1, True))
        none = [recs[0], recs[1], recs[2]]
        w = qs.windows(qs.snapshots_from_records(none), records=none, cost_of=cost)[0]
        self.assertEqual((w['uncertain_requests'], w['lower_bound']), (0, False))

    def test_window_costs_add_up_across_a_reset_with_stragglers(self):
        def at(minutes, seconds=0):
            return iso(minutes, seconds)
        ra, rb = (T0 + timedelta(minutes=9)).isoformat(), (T0 + timedelta(minutes=9 + 10080)).isoformat()
        one = dict(out=1_000_000)
        recs = [req('x0', at(0), turn='tx0', session='sx', q=quota(week=97, resets=ra), **one), req('y0', at(1), turn='ty0', session='sy', q=quota(week=98, resets=ra), **one),
                req('x1', at(2), turn='tx0', session='sx', q=quota(week=98, resets=ra), **one), req('y1', at(3), turn='ty0', session='sy', q=quota(week=98, resets=ra), **one),
                req('y2', at(10), turn='ty1', session='sy', q=quota(week=1, resets=rb), **one),
                req('x2', at(10, 10), turn='tx1', session='sx', q=quota(week=99, resets=ra), **one),  # a straggler of the old instance
                req('xq', at(10, 15), turn='tx1', session='sx', **one),  # no snapshot: placed by the time, in the new window
                req('y3', at(10, 20), turn='ty1', session='sy', q=quota(week=2, resets=rb), **one),
                req('x3', at(10, 30), turn='tx1', session='sx', q=quota(week=99, resets=ra), **one),
                req('x4', at(13), turn='tx1', session='sx', q=quota(week=3, resets=rb), **one), req('y4', at(14), turn='ty1', session='sy', q=quota(week=4, resets=rb), **one)]
        snaps = qs.snapshots_from_records(recs)
        cost = lambda r: prompts._cost(r, TABLE)
        ws = qs.windows(snaps, records=recs, cost_of=cost)
        self.assertEqual(len(ws), 2)
        self.assertAlmostEqual(sum(w['cost'] for w in ws), len(recs) * cost(recs[0]))  # every request in exactly one instance
        self.assertAlmostEqual(ws[0]['cost'], 6 * cost(recs[0]))  # the four before the reset and the two stragglers
        self.assertAlmostEqual(ws[1]['cost'], 5 * cost(recs[0]))

    def test_a_request_before_the_reset_stays_in_the_old_window_though_the_new_reading_is_nearer(self):
        cost = lambda r: prompts._cost(r, TABLE)
        old, new = (T0 + timedelta(hours=2)).isoformat(), (T0 + timedelta(hours=7)).isoformat()
        one = dict(out=1_000_000)
        recs = [req('a', iso(0), q=quota(five=40, resets5=old), **one), req('b', iso(1), q=quota(five=41, resets5=old), **one),
                req('q', iso(105), **one),  # 11:45 on the clock of the example: before the old window's reset, closer to the new readings
                req('c', iso(180), q=quota(five=2, resets5=new), **one), req('d', iso(181), q=quota(five=3, resets5=new), **one)]
        ws = qs.windows(qs.snapshots_from_records(recs), records=recs, cost_of=cost)
        self.assertEqual(len(ws), 2)
        c = cost(recs[0])
        self.assertAlmostEqual(ws[0]['cost'], 3 * c)
        self.assertAlmostEqual(ws[1]['cost'], 2 * c)

    def test_a_hit_that_learned_its_window_keeps_its_first_time_in_the_table(self):
        resets = (T0 + timedelta(hours=4)).isoformat()
        windowless = req('w', iso(1), q={'limit_id': 'codex', 'plan_type': 'pro', 'reached': 'rate_limit_reached', 'windows': []})
        full = req('f', iso(2), q={'limit_id': 'codex', 'plan_type': 'pro', 'reached': None,
                                   'windows': [{'slot': 'primary', 'minutes': 300, 'used_percent': 100.0, 'resets_at': resets}]})
        recs = [windowless, full]
        hits = limits.limit_hits(recs, [], TABLE)
        self.assertEqual([(h['window_minutes'], h['at']) for h in hits], [(300, iso(1))])  # resolved to the 5-hour window, at the first time
        w = qs.windows(qs.snapshots_from_records(recs), hits=hits)[0]
        self.assertIs(w['hit'], True)

    def test_a_reset_rollover_is_a_new_window_whichever_way_the_counter_went(self):
        for before, after in ((1, 2), (1, 1), (4, 1)):
            reset = (datetime.fromisoformat(RESET5) + timedelta(minutes=70)).isoformat()
            soon = (T0 + timedelta(minutes=60)).isoformat()
            recs = [req('a', iso(0), turn='t0', q=quota(five=before, resets5=soon)), req('b', iso(70), q=quota(five=after, resets5=reset))]
            self.assertEqual(len(qs.windows(qs.snapshots_from_records(recs))), 2, (before, after))
        early = [req('a', iso(0), turn='t0', q=quota(five=1, resets5=(T0 + timedelta(minutes=60)).isoformat())),
                 req('b', iso(30), q=quota(five=2, resets5=(T0 + timedelta(minutes=370)).isoformat()))]  # before the old reset time: not yet
        self.assertEqual(len(qs.windows(qs.snapshots_from_records(early))), 1)

    def test_a_turn_that_only_reports_the_five_hour_window_competes_in_the_weekly_one(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10, five=20)),
                req('a1', iso(10), out=1_000_000, q=quota(week=11, five=21)),
                req('c1', iso(12), turn='t2', session='s2', out=1_000_000, q=quota(five=30)),  # no weekly reading
                req('c2', iso(15), turn='t2', session='s2', out=1_000_000, q=quota(five=31)),
                req('a2', iso(20), out=1_000_000, q=quota(week=19, five=32))]
        got = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)
        a = got[K1][10080]
        self.assertEqual(a['label'], 'estimate')
        self.assertEqual(a['observed']['shared_with'], 1)

    def test_the_window_table_takes_hits_from_limit_hits_and_counts_quota_events(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=98), out=1_000_000), req('a', iso(10), q=quota(week=99), out=1_000_000),
                req('b', iso(20), q=quota(week=100), out=1_000_000)]  # crosses to 100 % without a reached type
        hits = limits.limit_hits(recs, [], TABLE)
        self.assertEqual([h['reached'] for h in hits], ['window_full'])
        snaps = qs.snapshots_from_records(recs)
        self.assertFalse(qs.windows(snaps)[0]['hit'])  # the readings alone name no reached limit
        self.assertTrue(qs.windows(snaps, hits=hits)[0]['hit'])
        event = req('e', iso(30), turn=None, session='se', out=0, q={**quota(week=100), 'status': 'event'})
        cost = lambda r: prompts._cost(r, TABLE)
        only = [recs[0], recs[1]]
        snaps = qs.snapshots_from_records(only, events=[event])
        w = qs.windows(snaps, records=only, cost_of=cost, hits=limits.limit_hits(only, [event], TABLE))[0]
        self.assertEqual((w['peak_percent'], w['snapshots'], w['hit']), (100.0, 3, True))  # the quota-only event shows in the table
        self.assertAlmostEqual(w['cost'], 2 * cost(recs[0]))  # but it is no request and no cost
        self.assertEqual(len(snaps), 2)

    def test_a_hit_belongs_to_its_own_limit_plan_and_counter(self):
        cost = lambda r: prompts._cost(r, TABLE)
        # two weekly limits of one account: one at 12 %, one reaching 100 %
        recs = [req('a', iso(1), session='sa', q=quota(week=12, limit_id='codex')),
                req('b', iso(2), session='sb', turn='tb', q=quota(week=100, limit_id='codex_x', reached='rate_limit_reached'))]
        by = {w['account']: w['hit'] for w in qs.windows(qs.snapshots_from_records(recs), hits=limits.limit_hits(recs, [], TABLE))}
        self.assertEqual(by, {'codex': False, 'codex_x': True})
        # two plans
        recs = [req('a', iso(1), session='sa', q=quota(week=12, plan='pro')),
                req('b', iso(2), session='sb', turn='tb', q=quota(week=100, plan='team', reached='rate_limit_reached'))]
        by = {w['plan_type']: w['hit'] for w in qs.windows(qs.snapshots_from_records(recs), hits=limits.limit_hits(recs, [], TABLE))}
        self.assertEqual(by, {'pro': False, 'team': True})
        # two counters of one limit and plan (the interleaved second account is the one that reaches 100 %)
        recs = self.orchestration(other_account=True)
        for r in recs:
            if r['session'] == 's9':
                r['quota'] = quota(week=100, reached='rate_limit_reached')
        snaps = qs.snapshots_from_records(recs)
        hits = limits.limit_hits(recs, [], TABLE)
        ws = qs.windows(snaps, hits=hits)
        self.assertEqual(sorted(w['hit'] for w in ws), [False, True])
        self.assertTrue(next(w for w in ws if w['peak_percent'] >= 100)['hit'])

    def test_every_request_of_a_reporting_session_is_in_one_instance(self):
        cost = lambda r: prompts._cost(r, TABLE)
        one = dict(out=1_000_000)
        # the edge requests (before the first and after the last reading) belong to the instance too
        recs = [req(str(n), iso(n), **one, **({'q': quota(week=10 + n)} if n in (2, 4) else {})) for n in range(1, 6)]
        w = qs.windows(qs.snapshots_from_records(recs), records=recs, cost_of=cost)[0]
        self.assertAlmostEqual(w['cost'], 5 * cost(recs[0]))
        self.assertFalse(w['lower_bound'])
        # between two instances: the nearer in time; and every request once
        first, reset = (T0 + timedelta(minutes=1000)).isoformat(), (T0 + timedelta(minutes=1000 + 10080)).isoformat()
        recs = [req('a', iso(0), q=quota(week=10, resets=first), **one), req('b', iso(100), q=quota(week=50, resets=first), **one), req('m1', iso(200), **one),
                req('m2', iso(2000), **one), req('c', iso(2200), q=quota(week=2, resets=reset), **one), req('d', iso(2300), q=quota(week=3, resets=reset), **one)]
        ws = qs.windows(qs.snapshots_from_records(recs), records=recs, cost_of=cost)
        self.assertEqual(len(ws), 2)
        self.assertAlmostEqual(ws[0]['cost'], 3 * cost(recs[0]))  # a, b, m1 (nearer to b) ...
        self.assertAlmostEqual(ws[1]['cost'], 3 * cost(recs[0]))  # ... m2 (nearer to c), c, d
        self.assertAlmostEqual(sum(w['cost'] for w in ws), len(recs) * cost(recs[0]))  # six requests, each in exactly one instance
        # a request too far from any reading of its session (beyond the window length) cannot be placed
        far = [req('a', iso(0), q=quota(five=10), **one), req('b', iso(10), q=quota(five=11), **one), req('x', iso(500), **one)]
        w = qs.windows(qs.snapshots_from_records(far), records=far, cost_of=cost)[0]
        self.assertEqual((w['uncertain_requests'], w['lower_bound']), (1, True))
        self.assertAlmostEqual(w['cost'], 2 * cost(far[0]))

    def test_a_turn_whose_readings_were_lost_after_a_reset_still_competes(self):
        reset = (datetime.fromisoformat(RESET) + timedelta(days=7)).isoformat()
        recs = [req('b0', iso(0), turn='tb', session='sb', q=quota(week=80)), req('b1', iso(10), turn='tb', session='sb', q=quota(week=81)),
                req('z', iso(19), turn='tz', session='sz', q=quota(week=1, resets=reset)),  # the first reading of the new instance
                req('a0', iso(20), turn='ta', session='sa', q=quota(week=1, resets=reset)), req('a1', iso(21), turn='ta', session='sa', q=quota(week=2, resets=reset)),
                req('a2', iso(25), turn='ta', session='sa', q=quota(week=5, resets=reset)), req('a3', iso(31), turn='ta', session='sa', q=quota(week=8, resets=reset))]
        recs += [req(f'q{n}', iso(22 + n), turn='tb', session='sb', out=2_000_000) for n in range(8)]  # the same turn went on, with no readings
        snaps = qs.snapshots_from_records(recs)
        a = qs.turn_shares(recs, snaps, TABLE)[('codex', 'sa', 'ta')][10080]
        self.assertEqual(a['label'], 'estimate')
        self.assertEqual(a['observed']['shared_with'], 1)
        self.assertLess(a['estimate'], 7.0)  # not all of the 7 points

    def test_a_request_after_an_instances_reset_is_not_charged_to_it(self):
        cost = lambda r: prompts._cost(r, TABLE)
        reset_a = (T0 + timedelta(minutes=60)).isoformat()
        reset_b = (T0 + timedelta(minutes=60 + 10080)).isoformat()
        one = dict(out=1_000_000)
        recs = [req('a', iso(0), q=quota(week=10, resets=reset_a), **one), req('b', iso(58), q=quota(week=50, resets=reset_a), **one),
                req('q', iso(61), **one),  # after the first window reset, 3 minutes after its last reading
                req('c', iso(100), q=quota(week=2, resets=reset_b), **one), req('d', iso(110), q=quota(week=3, resets=reset_b), **one)]
        ws = qs.windows(qs.snapshots_from_records(recs), records=recs, cost_of=cost)
        self.assertEqual(len(ws), 2)
        c = cost(recs[0])
        self.assertAlmostEqual(ws[0]['cost'], 2 * c)
        self.assertAlmostEqual(ws[1]['cost'], 3 * c)
        # a request after the reset that no later instance covers cannot be placed
        late = [recs[0], recs[1], req('z', iso(61 + 20000), **one)]
        w = qs.windows(qs.snapshots_from_records(late), records=late, cost_of=cost)[0]
        self.assertAlmostEqual(w['cost'], 2 * c)
        self.assertEqual((w['uncertain_requests'], w['lower_bound']), (1, True))

    def test_an_unpriced_concurrent_turn_is_weighed_by_its_tokens_at_the_average_price(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
                req('a1', iso(10), out=1_000_000, q=quota(week=11)),
                req('b1', iso(10, 30), turn='t2', session='s2', out=1_000_000, model='no-such-model', q=quota(week=13)),
                req('a2', iso(12), out=1_000_000, q=quota(week=15)), req('b2', iso(12, 30), turn='t2', session='s2', out=1_000_000, model='no-such-model', q=quota(week=16))]
        got = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)
        a, b = got[K1][10080], got[K2][10080]
        self.assertEqual((a['label'], b['label']), ('estimate', 'estimate'))
        self.assertGreater(b['estimate'], 1.0)  # not weighed as free
        self.assertAlmostEqual(a['estimate'] + b['estimate'], 6.0)  # the movement 10 -> 16, once

    def test_with_no_priced_request_in_the_window_a_shared_step_is_unknown(self):
        recs = [req('0', iso(0), turn='t0', session='s0', model='no-such-model', q=quota(week=10)),
                req('a1', iso(10), out=1_000_000, model='no-such-model', q=quota(week=11)),
                req('b1', iso(10, 30), turn='t2', session='s2', out=1_000_000, model='no-such-model', q=quota(week=13)),
                req('a2', iso(12), out=1_000_000, model='no-such-model', q=quota(week=15)), req('b2', iso(12, 30), turn='t2', session='s2', out=1_000_000, model='no-such-model', q=quota(week=16))]
        got = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)
        self.assertEqual((got[K1][10080]['label'], got[K2][10080]['label']), ('unknown', 'unknown'))

    def test_a_lone_unpriced_turn_is_observed(self):
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
                req('a1', iso(10), out=1_000_000, model='no-such-model', q=quota(week=11)),
                req('a2', iso(12), out=1_000_000, model='no-such-model', q=quota(week=15))]
        a = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)[K1][10080]
        self.assertEqual((a['label'], a['observed']['delta']), ('observed', 5.0))

    def test_a_five_hour_only_session_counts_in_the_weekly_window_cost(self):
        recs = [req('0', iso(0), turn='t0', session='s0', out=1_000_000, q=quota(week=10, five=20)),
                req('a1', iso(10), out=1_000_000, q=quota(week=11, five=21)),
                req('c1', iso(12), turn='t2', session='s2', out=1_000_000, q=quota(five=30)),  # no weekly reading
                req('c2', iso(15), turn='t2', session='s2', out=1_000_000, q=quota(five=31)),
                req('a2', iso(20), out=1_000_000, q=quota(week=19, five=32))]
        cost = lambda r: prompts._cost(r, TABLE)
        ws = {w['minutes']: w for w in qs.windows(qs.snapshots_from_records(recs), records=recs, cost_of=cost)}
        self.assertAlmostEqual(ws[10080]['cost'], 5 * cost(recs[0]))
        self.assertFalse(ws[10080]['lower_bound'])

    def test_per_window_shares_are_kept(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=10, five=20)), req('a', iso(10), q=quota(week=12, five=26))]
        per = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)[K1]
        self.assertEqual({m: x['observed']['delta'] for m, x in per.items()}, {10080: 2.0, 300: 6.0})
        self.assertEqual(qs.largest({K1: per})[K1]['window_key'][2], 300)

    def test_reset_inside_a_turn_is_not_observed(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=60)), req('a', iso(10), q=quota(week=61)), req('b', iso(11), q=quota(week=3))]
        s = shares(recs)[1][K1]
        self.assertEqual((s['observed'], s['estimate'], s['label']), (None, None, 'unknown'))
        self.assertEqual(qs.text(s), 'share of weekly Codex limit: n/a')

    def test_a_turn_crossing_into_a_new_window_instance_is_not_observed(self):
        new = (T0 + timedelta(days=10)).isoformat()
        recs = [req('0', iso(0), turn='t0', q=quota(week=60)), req('a', iso(10), q=quota(week=61)), req('b', iso(11), q=quota(week=2, resets=new))]
        self.assertEqual(shares(recs)[1][K1]['label'], 'unknown')

    def test_no_snapshot_before_the_turn_is_unknown(self):
        recs = [req('a', iso(10), q=quota(week=11)), req('b', iso(11), q=quota(week=13))]
        s = shares(recs)[1][K1]
        self.assertEqual((s['observed'], s['label']), (None, 'unknown'))
        self.assertNotIn('0%', qs.text(s))

    def test_turn_spanning_both_windows_picks_the_largest_delta(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=10, five=20)), req('a', iso(10), q=quota(week=11, five=26))]
        s = shares(recs)[1][K1]
        self.assertEqual((s['window_key'][2], s['observed']['delta']), (300, 6.0))
        self.assertEqual(qs.window_name(300), '5-hour')

    def test_only_limits_the_turns_computed(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=10)), req('a', iso(10), q=quota(week=12))]
        snaps = qs.snapshots_from_records(recs)
        self.assertEqual(set(qs.turn_shares(recs, snaps, TABLE, only={K1})), {K1})
        self.assertEqual(qs.turn_shares(recs, snaps, TABLE, only=set()), {})

    def test_tie_prefers_the_weekly_window(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=10, five=20)), req('a', iso(10), q=quota(week=12, five=22))]
        self.assertEqual(shares(recs)[1][K1]['window_key'][2], 10080)

    def test_credits_depleted_and_rejected_events(self):
        recs = [req('0', iso(0), turn='t0', q=quota(week=10)), req('a', iso(10), q=quota(week=12, reached='workspace_owner_credits_depleted')),
                req('r', iso(11), q={**quota(week=100), 'status': 'rejected'})]
        snaps, got = shares(recs)
        self.assertEqual(len(snaps), 2)
        self.assertEqual(got[K1]['observed']['delta'], 2.0)
        self.assertFalse(qs.windows(snaps)[0]['hit'])

    def test_unwindowed_and_quota_free_records_make_no_share(self):
        self.assertEqual(qs.compute([req('a', iso(1))], TABLE), ([], {}))

    def test_performance_shape_many_records(self):
        recs = [req(str(i), iso(i), turn=f't{i // 5}', q=quota(week=i // 50)) for i in range(2000)]
        _, got = shares(recs)
        self.assertEqual(len(got), 400)



def history_records():
    """Codex observations as History would return them: a first request, then two turns, the second one expensive."""
    return [req('0', iso(0), turn='t0', session='s0', q=quota(week=10)),
            req('a', iso(10), turn='t1', session='s1', out=1_000_000, q=quota(week=12)),
            req('b', iso(60), turn='t2', session='s2', out=2_000_000, q=quota(week=21))]


class TestOrchestration:
    @staticmethod
    def build():
        return Shares('test_per_window_shares_are_kept').orchestration()


class Surfaces(unittest.TestCase):
    def build(self, **kw):
        from tokenatlas import report
        recs = history_records()
        return report.build_report(recs, {}, quota=kw.pop('with_quota', True), now=datetime(2026, 9, 5, tzinfo=timezone.utc), **kw)

    def test_report_payload_with_and_without_snapshots(self):
        payload = self.build()
        self.assertEqual(sorted(v['percent'] for v in payload['quota_shares'].values() if v['percent'] is not None), [2.0, 9.0])
        self.assertEqual(len(payload['quota_windows']), 1)
        self.assertEqual(payload['quota_windows'][0]['peak_percent'], 21.0)
        bare = self.build(with_quota=False)
        self.assertNotIn('quota_shares', bare)
        self.assertNotIn('quota_windows', bare)

    def test_strings_in_both_languages_and_section_markup(self):
        import base64, gzip, json, re
        from tokenatlas import report
        page = report.render_report(self.build())
        i18n = json.loads(gzip.decompress(base64.b64decode(re.search(r'id="report-i18n"[^>]*>([^<]+)<', page).group(1))).decode())
        self.assertEqual(i18n['sv']['qs_est'], '≈ {n} % av {w} (uppskattning)')
        self.assertEqual(i18n['sv']['qs_obs'], '~{n} % av {w}')
        self.assertEqual(i18n['sv']['qs_w_week'], 'veckogränsen')
        self.assertEqual(i18n['en']['qs_est'], '≈ {n}% of {w} (estimate)')
        self.assertEqual(i18n['en']['qw_title'], 'Codex limit windows')
        self.assertIn('id="quota-windows" class="hidden"', page)

    def test_shared_report_has_no_ids_or_text(self):
        import json
        payload = self.build(redact=True)
        blob = json.dumps({k: payload[k] for k in ('quota_shares', 'quota_windows')})
        for secret in ('s1', 's2', 't1', 't2', 'codex"', 'pro'):
            self.assertNotIn(f'"{secret}"', blob)
        self.assertEqual(set(payload['quota_windows'][0]), {'harness', 'account', 'minutes', 'resets_at', 'start', 'peak_percent', 'peak_at', 'hit',
                                                            'snapshots', 'cost', 'unpriced_requests', 'uncertain_requests', 'lower_bound'})
        self.assertEqual(set(next(iter(payload['quota_shares'].values()))), {'minutes', 'label', 'percent', 'shared_with'})

    def test_a_subagent_only_filter_keeps_the_quota_fact_equal_to_the_card(self):
        from tokenatlas import report
        recs = TestOrchestration.build()
        now = datetime(2026, 9, 5, tzinfo=timezone.utc)
        subs = [r for r in recs if r['thread_kind'] == 'subagent']
        payload = report.build_report(subs, {}, universe=recs, now=now)
        fact = next(f for f in payload['insights']['windows'][1]['facts'] if f['id'] == 'quota_share')
        self.assertEqual([x['percent'] for x in fact['values']['turns']], [8.0])
        self.assertEqual([v['percent'] for v in payload['quota_shares'].values()], [8.0])

    def test_shared_report_pseudonymizes_a_non_default_limit_id(self):
        import json
        from tokenatlas import report
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10, limit_id='customer-12345')), req('a', iso(10), q=quota(week=12, limit_id='customer-12345'))]
        shared = report.build_report(recs, {}, redact=True, now=datetime(2026, 9, 5, tzinfo=timezone.utc))
        self.assertNotIn('customer-12345', json.dumps(shared))
        self.assertEqual(shared['quota_windows'][0]['account'], 'limit 001')
        private = report.build_report(recs, {}, redact=False, now=datetime(2026, 9, 5, tzinfo=timezone.utc))
        self.assertEqual(private['quota_windows'][0]['account'], 'customer-12345')

    def test_filtered_report_keeps_the_whole_history_shares(self):
        from tokenatlas import report
        recs = history_records()
        for r in recs:
            r['project_id'], r['project_label'] = ('p-' + r['turn_id']), r['turn_id']
        now = datetime(2026, 9, 5, tzinfo=timezone.utc)
        full = report.build_report(recs, {}, now=now)
        sub = [r for r in recs if r['turn_id'] == 't2']  # a project filter: the baselines and competitors are gone
        part = report.build_report(sub, {}, universe=recs, now=now)
        self.assertEqual([v['percent'] for v in part['quota_shares'].values()], [9.0])
        self.assertEqual(part['quota_windows'], full['quota_windows'])
        alone = report.build_report(sub, {}, now=now)
        self.assertNotEqual([v['label'] for v in alone['quota_shares'].values()], ['observed'])  # without the universe it cannot know

    def test_top_json_and_text(self):
        from tokenatlas import prompts
        recs = history_records()
        _, got = qs.compute(recs, TABLE)
        top = prompts.top_prompts(recs, TABLE, 5)['prompts']
        qs.mark_turns(top, got)
        by = {p['turn_id']: p['quota_share'] for p in top}
        self.assertEqual(by['t2'], dict(window_minutes=10080, delta_percent=9.0, label='observed', before=12.0, after=21.0, shared_with=0))
        self.assertEqual(qs.line(by['t2'], 'codex'), '~9% of weekly Codex limit')
        self.assertEqual(by['t0']['label'], 'unknown')
        self.assertEqual(qs.line(by['t0'], 'codex'), 'share of weekly Codex limit: n/a')
        qs.mark_turns([plain := {'harness': 'claude', 'session': 'x', 'turn_id': 'y'}], got)
        self.assertIsNone(plain['quota_share'])

    def test_insights_fact(self):
        from tokenatlas import insights
        recs = history_records()
        got = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)
        fact = next(f for f in insights.cost_facts(recs, TABLE, quota=got)['facts'] if f['id'] == 'quota_share')
        self.assertEqual(([x['percent'] for x in fact['values']['turns']], fact['values']['observed_turns'], fact['provenance']), ([9.0, 2.0], 2, 'computed'))
        self.assertNotIn('total_percent', fact['values'])
        result = insights.cost_facts(recs, TABLE, quota=got)
        self.assertIn('your 2 costliest Codex turns used ~9% and ~2% of their weekly limit windows', insights.render_text(result))
        self.assertFalse(any(f['id'] == 'quota_share' for f in insights.cost_facts(recs, TABLE)['facts']))
        self.assertFalse(any(f['id'] == 'quota_share' for f in insights.cost_facts(recs, TABLE, quota={})['facts']))

    def test_insights_uses_the_weekly_window_even_when_five_hour_moved_more(self):
        from tokenatlas import insights
        recs = [req('0', iso(0), turn='t0', session='s0', q=quota(week=10, five=20)), req('a', iso(10), out=1_000_000, q=quota(week=12, five=26))]
        got = qs.turn_shares(recs, qs.snapshots_from_records(recs), TABLE)
        fact = next(f for f in insights.cost_facts(recs, TABLE, quota=got)['facts'] if f['id'] == 'quota_share')
        self.assertEqual([x['percent'] for x in fact['values']['turns']], [2.0])


if __name__ == '__main__':
    unittest.main()
