"""Limit hits (issue #91): rejected Claude requests and Codex reached-limit observations name the turn that hit a limit."""
import base64
import gzip
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tokenatlas import insights, limits, pricing, report, why
from tokenatlas.history import History, is_limit_event, _clean_quota

UTC = timezone.utc
T0 = datetime(2026, 9, 3, 10, tzinfo=UTC)
START, END = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 10, tzinfo=UTC)
MODEL = 'claude-sonnet-5-5'
TABLE = pricing.load_prices()


def stamp(minutes, seconds=0):
    return (T0 + timedelta(minutes=minutes, seconds=seconds)).strftime('%Y-%m-%dT%H:%M:%SZ')


def user(uuid, minutes, text='PRIVATE PROMPT TEXT', sidechain=False):
    return {'type': 'user', 'uuid': uuid, 'sessionId': 's1', 'cwd': '/work/app', 'timestamp': stamp(minutes),
            'isSidechain': sidechain, 'message': {'role': 'user', 'content': text}}


def call(request, minutes, out=1000, sidechain=False):
    return {'type': 'assistant', 'uuid': 'u-' + request, 'requestId': request, 'sessionId': 's1', 'cwd': '/work/app',
            'timestamp': stamp(minutes), 'isSidechain': sidechain,
            'message': {'id': 'm-' + request, 'model': MODEL, 'stop_reason': 'end_turn',
                        'usage': {'input_tokens': 10, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0, 'output_tokens': out}}}


def rejected(request, minutes, kind='five_hour', resets=None, seconds=0, status='rejected', sidechain=False, **extra):
    resets = int((T0 + timedelta(hours=2)).timestamp()) if resets is None else resets
    quota = {'status': status, 'resetsAt': resets, 'overageStatus': 'rejected'}
    if kind is not None:
        quota['rateLimitType'] = kind
    zero = {'input_tokens': 0, 'output_tokens': 0, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}
    return {'type': 'assistant', 'uuid': 'u-' + request, 'requestId': request, 'sessionId': 's1', 'cwd': '/work/app',
            'timestamp': stamp(minutes, seconds), 'isSidechain': sidechain, 'error': 'rate_limit', 'isApiErrorMessage': True,
            'message': {'id': 'm-' + request, 'model': '<synthetic>', 'usage': zero, 'content': [{'type': 'text', 'text': 'limit'}]},
            'quotaLimits': quota, **extra}


def write(root, rows, name='s1.jsonl'):
    path = Path(root) / 'proj' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    return path


def session_rows():
    """Two turns: a big one (a, b) then a small one (c) that hits the five-hour limit, with two retries."""
    return [user('t1', 0), call('a', 1, 50000), call('b', 2, 20000), user('t2', 30), call('c', 31, 1000),
            rejected('r1', 32), rejected('r2', 32, seconds=5), rejected('r3', 33)]


class ClaudeReader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def collect(self, rows, **kw):
        write(self.root, rows)
        return why.collect_claude(self.root, START, END, **kw)

    def test_rejected_row_is_kept_with_quota_zero_tokens_and_unknown_model(self):
        records = {r.call_id: r for r in self.collect(session_rows())}
        r = records['r1']
        self.assertEqual((r.fresh_input, r.cache_read, r.cache_write, r.output), (0, 0, 0, 0))
        self.assertEqual(r.model, 'unknown')
        self.assertEqual(r.quota, {'limit_id': None, 'plan_type': None, 'reached': 'five_hour', 'status': 'rejected',
                                   'resets_at': (T0 + timedelta(hours=2)).isoformat(), 'windows': [{'slot': 'five_hour', 'minutes': 300, 'used_percent': 100.0,
                                                'resets_at': (T0 + timedelta(hours=2)).isoformat()}]})
        self.assertIsNone(records['a'].quota)

    def test_retries_are_separate_records_attributed_to_the_current_turn(self):
        records = {r.call_id: r for r in self.collect(session_rows())}
        self.assertEqual({'r1', 'r2', 'r3'} <= set(records), True)
        self.assertEqual({records[k].turn_id for k in ('c', 'r1', 'r2', 'r3')}, {'t2'})

    def test_weekly_and_unknown_types(self):
        rows = [user('t1', 0), rejected('w', 1, 'seven_day'), rejected('x', 2, 'mystery_window', resets=None)]
        records = {r.call_id: r for r in self.collect(rows)}
        self.assertEqual(records['w'].quota['windows'][0]['minutes'], 10080)
        self.assertEqual(records['x'].quota['reached'], 'mystery_window')
        self.assertEqual(records['x'].quota['windows'], [])
        # an unknown type keeps its reset time (top level), so rejections are told apart by it
        resets = int((T0 + timedelta(hours=3)).timestamp())
        kept = {r.call_id: r for r in self.collect([user('t1', 0), rejected('y', 1, 'mystery_window', resets=resets)])}
        self.assertEqual(kept['y'].quota['resets_at'], (T0 + timedelta(hours=3)).isoformat())
        self.assertEqual(_clean_quota(kept['y'].quota)['resets_at'], (T0 + timedelta(hours=3)).isoformat())

    def test_rejection_in_a_subagent_file_is_kept(self):
        write(self.root, [user('t1', 0), call('a', 1)])
        path = self.root / 'proj' / 's1' / 'subagents' / 'agent-x1.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(rejected('sub', 5, sidechain=True, agentId='x1')) + '\n')
        records = {r.call_id: r for r in why.collect_claude(self.root, START, END)}
        self.assertEqual(records['sub'].thread_kind, 'subagent')
        self.assertEqual(records['sub'].quota['status'], 'rejected')

    def test_quota_limits_that_is_not_rejected_is_ignored(self):
        records = {r.call_id: r for r in self.collect([user('t1', 0), call('a', 1), rejected('ok', 2, status='allowed')])}
        self.assertNotIn('ok', records)


class HistoryLimitEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write(self.root / 'logs', session_rows())
        self.db = self.root / 'state' / 'h.sqlite3'

    def test_records_exclude_limit_events_and_limit_events_returns_them(self):
        with History(self.db) as h:
            h.refresh('claude', self.root / 'logs')
            self.assertEqual({r['id'] for r in h.records()}, {'a', 'b', 'c'})
            self.assertEqual({r['id'] for r in h.records(include_limit_events=True)}, {'a', 'b', 'c', 'r1', 'r2', 'r3'})
            events = h.limit_events()
            self.assertEqual({r['id'] for r in events}, {'r1', 'r2', 'r3'})
            self.assertTrue(all(is_limit_event(r) and r['model'] is None for r in events))
            self.assertEqual(len(h.limit_events(T0 + timedelta(minutes=33), None)), 1)
            self.assertEqual(h.doctor()['observations'], 6)

    def test_counts_and_costs_are_unchanged_by_limit_events(self):
        other = Path(self.tmp.name) / 'plain'
        write(other, [r for r in session_rows() if 'quotaLimits' not in r])
        with History(self.db) as h, History(Path(self.tmp.name) / 'plain.sqlite3') as p:
            h.refresh('claude', self.root / 'logs')
            p.refresh('claude', other)
            strip = lambda rows: [{k: v for k, v in r.items() if k not in ('machine', 'sources', 'quota')} for r in rows]
            self.assertEqual(strip(h.records()), strip(p.records()))
            self.assertEqual(sum(1 for _ in h.records()), 3)

    def test_rejection_without_a_valid_type_stays_a_limit_event(self):
        self.assertEqual(_clean_quota({'status': 'rejected', 'reached': None, 'windows': []}),
                         {'limit_id': None, 'plan_type': None, 'reached': None, 'windows': [], 'status': 'rejected', 'resets_at': None})
        self.assertIsNone(_clean_quota({'reached': None, 'windows': []}))
        write(self.root / 'untyped', [user('t1', 0), call('a', 1, 5000), rejected('r1', 2, kind=None, resets=0)])
        with History(self.root / 'untyped.sqlite3') as h:
            h.refresh('claude', self.root / 'untyped')
            self.assertEqual([r['id'] for r in h.records()], ['a'])
            events = h.limit_events()
            self.assertEqual([r['id'] for r in events], ['r1'])
            hits = limits.limit_hits(h.records(), events, TABLE)
            self.assertEqual((len(hits), hits[0]['window'], hits[0]['window_minutes']), (1, None, None))

    def test_snapshot_import_keeps_limit_events(self):
        snap = self.root / 'snap.sqlite3'
        with History(self.db) as h:
            h.refresh('claude', self.root / 'logs')
            h.snapshot(snap)
        with History(self.root / 'other.sqlite3') as o:
            o.import_snapshot(snap, 'laptop')
            self.assertEqual({r['id'] for r in o.limit_events()}, {'r1', 'r2', 'r3'})
            self.assertEqual({r['id'] for r in o.records()}, {'a', 'b', 'c'})

    def test_clean_quota_accepts_window_slots_and_rejected_status_only(self):
        q = {'reached': 'five_hour', 'status': 'rejected', 'windows': [
            {'slot': 'five_hour', 'minutes': 300, 'used_percent': 100.0, 'resets_at': '2026-09-03T12:00:00+00:00'},
            {'slot': 'bogus', 'minutes': 300, 'used_percent': 1.0, 'resets_at': None}]}
        cleaned = _clean_quota(q)
        self.assertEqual([w['slot'] for w in cleaned['windows']], ['five_hour'])
        self.assertEqual(cleaned['status'], 'rejected')
        self.assertNotIn('status', _clean_quota({**q, 'status': 'allowed'}))


def obs(id, ts, cost_tokens=0, harness='claude', provider='anthropic', session='s1', turn='t1', quota=None, **extra):
    return dict(id=id, ts=ts, harness=harness, provider=provider, machine='m', session=session, turn_id=turn, thread_kind='main',
                agent='main', model=MODEL if cost_tokens else None, quota=quota, complete=True, sources=[], raw_usage={}, tariff=None,
                tokens=dict(fresh_input=0, cache_read=0, cache_write=0, output=cost_tokens, reasoning=0),
                id_synthetic=False, warnings=[], parent_session=None, origin='cli', effort=None, project_id='/w/app', project_label='app',
                turn_confidence='observed', **extra)


def iso(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat()


def five_hour(resets_min, status='rejected', reached='five_hour'):
    return {'limit_id': None, 'plan_type': None, 'reached': reached, 'status': status,
            'windows': [{'slot': 'five_hour', 'minutes': 300, 'used_percent': 100.0, 'resets_at': iso(resets_min)}]}


class LimitHits(unittest.TestCase):
    def test_no_hits(self):
        self.assertEqual(limits.limit_hits([obs('a', iso(0), 100)], [], TABLE), [])

    def test_claude_retries_are_one_hit_at_the_earliest_time(self):
        recs = [obs('a', iso(1), 100000)]
        events = [obs(f'r{i}', iso(30 + i), quota=five_hour(120)) for i in range(3)]
        hits = limits.limit_hits(recs, list(reversed(events)), TABLE)
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertEqual((h['harness'], h['at'], h['retries'], h['window_minutes'], h['reached']), ('claude', iso(30), 3, 300, 'five_hour'))
        self.assertEqual(h['turn'], ('claude', 's1', 't1'))
        # a different reset moment is another hit
        self.assertEqual(len(limits.limit_hits(recs, [*events, obs('z', iso(400), quota=five_hour(700))], TABLE)), 2)

    def test_window_ranks_turns_with_shares(self):
        recs = [obs('a', iso(-400), 900000, turn='old'),  # before the window: [120-300, 30] = [-180, 30]
                obs('b', iso(0), 100000, turn='big'), obs('c', iso(5), 20000, turn='mid'), obs('d', iso(6), 10000, turn='small'),
                obs('e', iso(7), 1000, turn='tiny'), obs('f', iso(8), 5, provider='openai', harness='codex', turn='x')]
        hits = limits.limit_hits(recs, [obs('r', iso(30), turn='tiny', quota=five_hour(120))], TABLE)
        w = hits[0]['window']
        self.assertEqual([t['turn'][2] for t in w['top']], ['big', 'mid', 'small'])
        self.assertAlmostEqual(w['cost'], sum(t['cost'] for t in w['top']) + pricing_cost(recs[4]), places=9)
        self.assertAlmostEqual(sum(t['share'] for t in w['top']) + pricing_cost(recs[4]) / w['cost'], 1.0, places=9)
        self.assertEqual(w['requests'], 4)
        self.assertEqual(w['unpriced_requests'], 0)
        self.assertTrue(0 < w['top'][0]['share'] < 1)
        self.assertEqual(hits[0]['turn'], ('claude', 's1', 'tiny'))

    def test_unpriced_requests_are_counted_not_guessed(self):
        unknown = obs('u', iso(1), 5000, turn='odd')
        unknown['model'] = 'no-such-model'
        hits = limits.limit_hits([unknown, obs('b', iso(2), 1000, turn='b')], [obs('r', iso(30), quota=five_hour(120))], TABLE)
        self.assertEqual(hits[0]['window']['unpriced_requests'], 1)
        self.assertEqual([t['turn'][2] for t in hits[0]['window']['top']], ['b'])

    def test_unknown_window_type_has_no_ranking(self):
        quota = {'limit_id': None, 'plan_type': None, 'reached': 'mystery', 'status': 'rejected', 'windows': []}
        hits = limits.limit_hits([obs('a', iso(1), 1000)], [obs('r', iso(30), quota=quota)], TABLE)
        self.assertEqual(len(hits), 1)
        self.assertIsNone(hits[0]['window'])
        self.assertIsNone(hits[0]['window_minutes'])

    def test_events_without_reset_time_group_only_within_a_gap(self):
        quota = {'limit_id': None, 'plan_type': None, 'reached': 'mystery', 'status': 'rejected', 'resets_at': None, 'windows': []}
        events = [obs('r1', iso(0), quota=quota), obs('r2', iso(5), quota=quota), obs('r3', iso(60 * 24 * 2), quota=quota)]
        hits = limits.limit_hits([], events, TABLE)
        self.assertEqual([(h['at'], h['retries']) for h in hits], [(iso(0), 2), (iso(60 * 24 * 2), 1)])
        withreset = dict(quota, resets_at=iso(500))
        hits = limits.limit_hits([], [obs('a', iso(0), quota=withreset), obs('b', iso(60 * 24 * 2), quota=withreset)], TABLE)
        self.assertEqual((len(hits), hits[0]['resets_at'], hits[0]['window']), (1, iso(500), None))

    def test_ambiguous_identity_is_excluded_and_incomplete_is_a_lower_bound(self):
        clean = obs('a', iso(1), 1000, turn='a')
        ambiguous = dict(obs('b', iso(2), 9000000, turn='b'), id_synthetic=True)
        hits = limits.limit_hits([clean, ambiguous], [obs('r', iso(30), quota=five_hour(120))], TABLE)
        w = hits[0]['window']
        self.assertEqual((w['requests'], w['lower_bound']), (1, False))
        self.assertEqual([t['turn'][2] for t in w['top']], ['a'])
        partial = dict(obs('c', iso(3), 1000, turn='c'), complete=False)
        hits = limits.limit_hits([clean, partial], [obs('r', iso(30), quota=five_hour(120))], TABLE)
        self.assertTrue(hits[0]['window']['lower_bound'])

    def test_codex_consecutive_state_is_per_harness_and_limit(self):
        def cx(id, minute, reached):
            quota = {'limit_id': 'codex', 'plan_type': 'plus', 'reached': reached, 'windows': []}
            return obs(id, iso(minute), 1000, harness='codex', provider='openai', session='c1', turn='ct', quota=quota)
        recs = [cx('1', 1, 'workspace_owner_credits_depleted'), obs('mid', iso(2), 1000, turn='other'), cx('3', 3, 'workspace_owner_credits_depleted')]
        self.assertEqual(len(limits.limit_hits(recs, [], TABLE)), 1)

    def cx(self, id, minute, reached, windows, limit_id='codex'):
        quota = {'limit_id': limit_id, 'plan_type': 'plus', 'reached': reached, 'windows': windows}
        return obs(id, iso(minute), 1000, harness='codex', provider='openai', session='c1', turn='ct', quota=quota)

    @staticmethod
    def win(percent, resets, slot='primary', minutes=300):
        return {'slot': slot, 'minutes': minutes, 'used_percent': percent, 'resets_at': iso(resets)}

    def test_codex_episode_survives_drifting_reset_and_restarts_after_recovery(self):
        drift = [self.cx(str(i), i, 'rate_limit_reached', [self.win(100.0, 100 + i)]) for i in range(1, 5)]
        self.assertEqual([h['at'] for h in limits.limit_hits(drift, [], TABLE)], [iso(1)])
        again = [self.cx('1', 1, 'rate_limit_reached', [self.win(100.0, 100)]), self.cx('2', 2, None, [self.win(10.0, 100)]),
                 self.cx('3', 3, 'rate_limit_reached', [self.win(100.0, 100)])]
        self.assertEqual([h['at'] for h in limits.limit_hits(again, [], TABLE)], [iso(1), iso(3)])

    def test_codex_episode_expires_after_a_window_without_recovery(self):
        day = [self.cx('1', 1, 'rate_limit_reached', [self.win(100.0, 100)]), self.cx('2', 1 + 24 * 60, 'rate_limit_reached', [self.win(100.0, 100 + 24 * 60)])]
        self.assertEqual([h['at'] for h in limits.limit_hits(day, [], TABLE)], [iso(1), iso(1 + 24 * 60)])
        seconds = [self.cx(str(i), i, 'rate_limit_reached', [dict(self.win(100.0, 100), resets_at=(T0 + timedelta(minutes=100, seconds=7 * i)).isoformat())])
                   for i in range(1, 4)]
        self.assertEqual(len(limits.limit_hits(seconds, [], TABLE)), 1)

    def test_codex_reset_movement_alone_does_not_start_a_hit(self):
        recs = [self.cx('1', 1, 'rate_limit_reached', [self.win(100.0, 100)]), self.cx('2', 2, 'rate_limit_reached', [self.win(100.0, 130)])]
        self.assertEqual(len(limits.limit_hits(recs, [], TABLE)), 1)

    def test_codex_episode_ends_when_the_named_window_changes(self):
        weekly = self.win(100.0, 100, 'secondary', 10080)
        recs = [self.cx('1', 1, 'rate_limit_reached', [self.win(100.0, 100), self.win(20.0, 100, 'secondary', 10080)]),
                self.cx('2', 2, 'rate_limit_reached', [self.win(50.0, 100), weekly])]
        hits = limits.limit_hits(recs, [], TABLE)
        self.assertEqual([h['window_minutes'] for h in hits], [300, 10080])

    def test_provider_aliases_count_in_the_window(self):
        def codex(id, minute, provider, turn):
            r = obs(id, iso(minute), 5000, harness='codex', provider=provider, session='c1', turn=turn)
            r['model'] = 'gpt-5.6-luna'
            return r
        hit = self.cx('hit', 100, 'rate_limit_reached', [self.win(100.0, 400)])
        alias_only = limits.limit_hits([codex('a', 10, 'openai-codex', 'ta'), hit], [], TABLE)[0]['window']
        self.assertEqual(alias_only['requests'], 1 + 1)  # the alias record and the hit's own observation (provider openai)
        mixed = limits.limit_hits([codex('a', 10, 'openai-codex', 'ta'), codex('b', 11, 'openai', 'tb'), hit], [], TABLE)[0]['window']
        self.assertEqual(mixed['requests'], 3)
        self.assertEqual({t['turn'][2] for t in mixed['top']} >= {'ta', 'tb'}, True)

    def test_subagent_rejection_takes_the_turn_of_its_thread(self):
        main = obs('m', iso(1), 1000, turn='t1')
        sub = dict(obs('s', iso(5), 90000, turn=None), thread_kind='subagent', parent_session='s1', agent='a1')
        event = dict(obs('r', iso(6), turn=None, quota=five_hour(120)), thread_kind='subagent', agent='a1')
        hits = limits.limit_hits([main, sub], [event], TABLE)
        self.assertEqual(hits[0]['turn'], ('claude', 's1', 't1'))
        item = {'harness': 'claude', 'session': 's1', 'turn_id': 't1'}
        limits.mark_turns([item], hits)
        self.assertIn('limit_hit', item)
        early = dict(event, ts=iso(0))  # before any usage of the thread: the earliest after it
        self.assertEqual(limits.limit_hits([main, sub], [early], TABLE)[0]['turn'], ('claude', 's1', 't1'))

    def test_session_prefix_enforces_the_harness(self):
        recs = [obs('a', iso(1), 1000, turn='t1'), self.cx('c', 5, 'rate_limit_reached', [self.win(100.0, 100)])]
        hits = limits.limit_hits(recs, [obs('r', iso(30), turn='t1', quota=five_hour(120))], TABLE)
        self.assertEqual(sorted(h['harness'] for h in hits), ['claude', 'codex'])
        self.assertEqual([h['harness'] for h in limits.scope_hits(hits, recs, session='claude:s1')], ['claude'])
        self.assertEqual([h['harness'] for h in limits.scope_hits(hits, recs, session='codex:c1')], ['codex'])
        self.assertEqual(limits.scope_hits(hits, recs, harness='codex', session='claude:s1'), [])

    def test_malformed_window_durations_are_dropped_without_crashing(self):
        huge = {'used_percent': 100, 'window_minutes': 10 ** 400, 'resets_at': 1790000000}
        quota = why._codex_quota({'limit_id': 'codex', 'primary': huge, 'secondary': dict(huge, window_minutes=527041),
                                  'rate_limit_reached_type': 'rate_limit_reached'})
        self.assertEqual(quota['windows'], [])
        edge = why._codex_quota({'primary': dict(huge, window_minutes=527040)})
        self.assertEqual(edge['windows'][0]['minutes'], 527040)
        cleaned = _clean_quota({'reached': 'x', 'windows': [{'slot': 'primary', 'minutes': 10 ** 400, 'used_percent': 100.0, 'resets_at': None},
                                                              {'slot': 'primary', 'minutes': 0, 'used_percent': 100.0, 'resets_at': None}]})
        self.assertEqual(cleaned['windows'], [])
        hits = limits.limit_hits([obs('a', iso(1), 1000, harness='codex', provider='openai', quota=quota)], [], TABLE)
        self.assertEqual((len(hits), hits[0]['window']), (1, None))

    def cxs(self, id, minute, session, reached):
        r = self.cx(id, minute, reached, [self.win(100.0 if reached else 10.0, 100)])
        r['session'] = session
        return r

    def test_recovery_only_counts_from_a_session_that_reported_the_episode(self):
        stale = [self.cxs('1', 1, 'A', 'rate_limit_reached'), self.cxs('2', 2, 'B', None), self.cxs('3', 3, 'A', 'rate_limit_reached')]
        self.assertEqual(len(limits.limit_hits(stale, [], TABLE)), 1)
        own = [self.cxs('1', 1, 'A', 'rate_limit_reached'), self.cxs('2', 2, 'A', None), self.cxs('3', 3, 'A', 'rate_limit_reached')]
        self.assertEqual(len(limits.limit_hits(own, [], TABLE)), 2)
        overlap = [self.cxs('1', 1, 'A', 'rate_limit_reached'), self.cxs('2', 2, 'B', 'rate_limit_reached'), self.cxs('3', 3, 'B', None),
                   self.cxs('4', 4, 'A', 'rate_limit_reached')]
        hits = limits.limit_hits(overlap, [], TABLE)
        self.assertEqual([h['at'] for h in hits], [iso(1), iso(4)])  # B's recovery ends the episode B joined, A's later report starts the next
        late = [self.cxs('1', 1, 'A', 'rate_limit_reached'), self.cxs('2', 1 + 400, 'B', None), self.cxs('3', 2 + 400, 'A', 'rate_limit_reached')]
        self.assertEqual(len(limits.limit_hits(late, [], TABLE)), 2)  # a gap longer than the window ends it

    def test_window_counts_only_the_hits_own_harness(self):
        claude = obs('c', iso(1), 5000, turn='tc')
        opencode = obs('o', iso(2), 90000, harness='opencode', provider='anthropic', session='oc', turn='to')  # API key usage, a different pool
        w = limits.limit_hits([claude, opencode], [obs('r', iso(30), quota=five_hour(120))], TABLE)[0]['window']
        self.assertEqual(w['requests'], 1)
        self.assertEqual([t['turn'][2] for t in w['top']], ['tc'])

    def test_scope_maps_rolled_up_subagents_through_the_full_assignment(self):
        main = obs('m', iso(1), 1000, turn='t1')
        sub = dict(obs('s', iso(5), 9000, turn=None), thread_kind='subagent', parent_session='s1', agent='a1')
        sub['model'] = 'sub-model'
        recs = [main, sub]
        hits = limits.limit_hits(recs, [obs('r', iso(30), turn='t1', quota=five_hour(120))], TABLE)
        self.assertEqual(hits[0]['turn'], ('claude', 's1', 't1'))
        self.assertEqual(len(limits.scope_hits(hits, [sub], agent='a1', universe=recs)), 1)
        self.assertEqual(len(limits.scope_hits(hits, [sub], model='sub-model', universe=recs)), 1)
        self.assertEqual(limits.scope_hits(hits, [], model='other', universe=recs), [])

    def test_codex_window_is_rolling_from_the_hit(self):
        # resets at +400 min: a reset-anchored window would start at +100 and miss the call at +20; the rolling window [hit-300, hit] has it
        recs = [obs('early', iso(20), 5000, harness='codex', provider='openai', session='c1', turn='early'),
                self.cx('hit', 250, 'rate_limit_reached', [self.win(100.0, 400)])]
        recs[0]['model'] = 'gpt-5.6-luna'
        h = limits.limit_hits(recs, [], TABLE)[0]
        self.assertEqual(h['window']['start'], iso(250 - 300))
        self.assertIn('early', [t['turn'][2] for t in h['window']['top']])

    def test_codex_window_selection(self):
        unknown = limits.limit_hits([self.cx('1', 1, 'something_new', [self.win(40.0, 100), self.win(20.0, 900, 'secondary', 10080)])], [], TABLE)[0]
        self.assertEqual((unknown['window_minutes'], unknown['window']), (None, None))
        both = limits.limit_hits([self.cx('1', 1, 'rate_limit_reached', [self.win(100.0, 100), self.win(100.0, 900, 'secondary', 10080)])], [], TABLE)[0]
        self.assertIsNone(both['window_minutes'])
        one = limits.limit_hits([self.cx('1', 1, 'rate_limit_reached', [self.win(100.0, 100), self.win(20.0, 900, 'secondary', 10080)])], [], TABLE)[0]
        self.assertEqual(one['window_minutes'], 300)

    def test_scope_hits(self):
        recs = [obs('a', iso(1), 1000, turn='t1'), self.cx('c', 5, 'rate_limit_reached', [self.win(100.0, 100)])]
        hits = limits.limit_hits(recs, [obs('r', iso(30), turn='t1', quota=five_hour(120))], TABLE)
        self.assertEqual({h['harness'] for h in hits}, {'claude', 'codex'})
        self.assertEqual([h['harness'] for h in limits.scope_hits(hits, recs, harness='codex')], ['codex'])
        self.assertEqual([h['harness'] for h in limits.scope_hits(hits, recs, start=T0 + timedelta(minutes=10))], ['claude'])
        self.assertEqual([h['harness'] for h in limits.scope_hits(hits, recs, end=T0 + timedelta(minutes=10))], ['codex'])
        self.assertEqual([h['harness'] for h in limits.scope_hits(hits, [recs[1]], session='c1')], ['codex'])
        self.assertEqual(len(limits.scope_hits(hits, recs)), 2)

    def test_codex_consecutive_reached_is_one_hit(self):
        def cx(id, minute, reached, used=100.0, resets=100):
            quota = {'limit_id': 'codex', 'plan_type': 'plus', 'reached': reached,
                     'windows': [{'slot': 'primary', 'minutes': 300, 'used_percent': used, 'resets_at': iso(resets)},
                                 {'slot': 'secondary', 'minutes': 10080, 'used_percent': 20.0, 'resets_at': iso(9000)}]}
            return obs(id, iso(minute), 1000, harness='codex', provider='openai', session='c1', turn='ct', quota=quota)
        recs = [cx('1', 1, None, 50.0), cx('2', 2, 'rate_limit_reached'), cx('3', 3, 'rate_limit_reached'), cx('4', 4, None, 5.0, resets=400),
                cx('5', 5, 'rate_limit_reached', resets=400)]
        hits = limits.limit_hits(recs, [], TABLE)
        self.assertEqual([(h['at'], h['window_minutes'], h['harness']) for h in hits], [(iso(2), 300, 'codex'), (iso(5), 300, 'codex')])
        self.assertEqual(hits[0]['turn'], ('codex', 'c1', 'ct'))

    def test_codex_depleted_credits_is_a_hit_without_a_window(self):
        quota = {'limit_id': 'codex', 'plan_type': 'team', 'reached': 'workspace_owner_credits_depleted',
                 'windows': [{'slot': 'primary', 'minutes': 10080, 'used_percent': 40.0, 'resets_at': iso(9000)}]}
        hits = limits.limit_hits([obs('1', iso(1), 1000, harness='codex', provider='openai', session='c1', turn='ct', quota=quota)], [], TABLE)
        self.assertEqual([(h['reached'], h['window_minutes'], h['window']) for h in hits], [('workspace_owner_credits_depleted', None, None)])
        self.assertEqual(limits.badge(hits[0]), 'Hit a limit (workspace_owner_credits_depleted)')

    def test_mark_turns_and_badge(self):
        hits = limits.limit_hits([obs('a', iso(1), 1000)], [obs('r', iso(30), quota=five_hour(120))], TABLE)
        item = {'harness': 'claude', 'session': 's1', 'turn_id': 't1'}
        other = {'harness': 'claude', 'session': 's1', 'turn_id': 'zz'}
        limits.mark_turns([item, other], hits)
        self.assertEqual(item['limit_hit'], {'reached': 'five_hour', 'window_minutes': 300, 'at': iso(30)})
        self.assertNotIn('limit_hit', other)
        self.assertEqual(limits.badge(hits[0]), 'Hit the 5-hour limit')


def pricing_cost(record):
    from tokenatlas import prompts
    return prompts._cost(record, TABLE)


class Surfaces(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        write(root / 'logs', session_rows())
        with History(root / 'h.sqlite3') as h:
            h.refresh('claude', root / 'logs')
            self.records = h.records()
            self.events = h.limit_events()
            self.status = h.doctor()
        self.hits = limits.limit_hits(self.records, self.events, TABLE)

    def build(self, **kw):
        return report.build_report(self.records, self.status, limit_hits=self.hits, now=datetime(2026, 9, 5, tzinfo=UTC), **kw)

    def test_report_payload_and_page(self):
        payload = self.build()
        self.assertEqual(len(payload['limit_hits']), 1)
        h = payload['limit_hits'][0]
        self.assertEqual((h['window_minutes'], h['retries']), (300, 3))
        self.assertIsInstance(h['prompt'], int)
        self.assertEqual(h['window']['top'][0]['prompt'], 0)
        page = report.render_report(payload)
        i18n = json.loads(gzip.decompress(base64.b64decode(re.search(r'id="report-i18n"[^>]*>([^<]+)<', page).group(1))).decode())
        for lang, words in (('sv', ('Gränsträffar', 'Slog i 5-timmarsgränsen', 'andel av det tokenatlas såg')),
                            ('en', ('Limit hits', 'Hit the 5-hour limit', 'share of what tokenatlas saw'))):
            blob = json.dumps(i18n[lang], ensure_ascii=False)
            for w in words:
                self.assertIn(w, blob)
            self.assertNotIn('primary', i18n[lang]['lh_head'])
        self.assertIn('claude.ai', i18n['en']['lh_cover'])
        self.assertIn('claude.ai', i18n['sv']['lh_cover'])
        self.assertIn('id="limit-hits" class="panel section hidden"', page)

    def test_no_hits_no_section_data(self):
        payload = report.build_report(self.records, self.status, now=datetime(2026, 9, 5, tzinfo=UTC))
        self.assertNotIn('limit_hits', payload)
        self.assertFalse(any(f['id'] == 'limit_hits' for w in payload['insights']['windows'] for f in w['facts']))

    def test_shared_report_has_no_session_ids_or_prompt_text(self):
        payload = self.build(redact=True)
        blob = json.dumps(payload['limit_hits'])
        for secret in ('s1', 'PRIVATE', '/work/app', 't1', 't2'):
            self.assertNotIn(f'"{secret}"', blob)
        self.assertNotIn('PRIVATE', json.dumps(payload))
        self.assertEqual(set(payload['limit_hits'][0]), {'harness', 'at', 'reached', 'window_minutes', 'resets_at', 'retries', 'prompt', 'label', 'window'})

    def test_insights_fact(self):
        result = insights.cost_facts(self.records, TABLE, hits=self.hits)
        fact = next(f for f in result['facts'] if f['id'] == 'limit_hits')
        self.assertEqual(fact['values']['count'], 1)
        self.assertEqual(fact['values']['limits'], [{'limit': 'five_hour', 'harness': 'claude', 'count': 1}])
        self.assertIn('5-hour limit', insights.render_text(result))
        self.assertFalse(any(f['id'] == 'limit_hits' for f in insights.cost_facts(self.records, TABLE)['facts']))
        late = insights.cost_facts(self.records, TABLE, start=datetime(2026, 9, 4, tzinfo=UTC), hits=self.hits)
        self.assertFalse(any(f['id'] == 'limit_hits' for f in late['facts']))


class Privacy(unittest.TestCase):
    def setUp(self):
        self.recs = [obs('a', iso(1), 100000, turn='t1')]
        self.quota = {'limit_id': None, 'plan_type': None, 'reached': 'private-session-xyz', 'status': 'rejected', 'resets_at': iso(120), 'windows': []}
        self.events = [obs('r', iso(30), turn='t2', quota=self.quota)]
        self.hits = limits.limit_hits(self.recs, self.events, TABLE)
        self.now = datetime(2026, 9, 5, tzinfo=UTC)

    def test_unknown_limit_type_is_neutral_in_shared_reports_and_insights(self):
        shared = report.build_report(self.recs, {}, redact=True, limit_hits=self.hits, now=self.now)
        self.assertNotIn('private-session-xyz', json.dumps(shared))
        self.assertEqual(shared['limit_hits'][0]['reached'], 'other')
        self.assertNotIn('private-session-xyz', json.dumps(insights.cost_facts(self.recs, TABLE, hits=self.hits)))
        private = report.build_report(self.recs, {}, redact=False, limit_hits=self.hits, now=self.now)
        self.assertEqual(private['limit_hits'][0]['reached'], 'private-session-xyz')
        known = limits.limit_hits(self.recs, [obs('r', iso(30), quota=five_hour(120))], TABLE)
        self.assertEqual(report.build_report(self.recs, {}, redact=True, limit_hits=known, now=self.now)['limit_hits'][0]['reached'], 'five_hour')

    def test_rejection_only_turn_gets_a_safe_label(self):
        for redact in (True, False):
            payload = report.build_report(self.recs, {}, redact=redact, limit_hits=self.hits, now=self.now,
                                          **({} if redact else {'prompt_texts': {('claude', 's1', 't2'): 'Refactor it'}}))
            h = payload['limit_hits'][0]
            self.assertIsNone(h['prompt'])  # no successful call: no card
            self.assertEqual((h['label']['at'], h['label']['harness']), (iso(30), 'claude'))
            self.assertEqual(h['label']['text'], None if redact else 'Refactor it')
            self.assertNotIn('t2', json.dumps(h))

    def test_turn_outside_the_card_list_keeps_a_label(self):
        recs = [obs(f'o{i}', iso(i), 1000 * (i + 1), turn=f'turn{i}') for i in range(12)]
        hits = limits.limit_hits(recs, [obs('r', iso(40), turn='turn11', quota=five_hour(120))], TABLE)
        top = report.build_report(recs, {}, redact=True, limit_hits=hits, now=self.now)['limit_hits'][0]['window']['top']
        self.assertTrue(all(t['label'] and t['label']['at'] and 'text' in t['label'] for t in top))
        self.assertTrue(all(t['prompt'] is not None for t in top))


class SingleAssignment(unittest.TestCase):
    def test_rejection_only_parent_turn_and_subagent_usage_agree_with_the_cards(self):
        sub = dict(obs('sub', iso(40), 90000, turn=None), thread_kind='subagent', parent_session='s1', session='s1', agent='a1')
        recs = [obs('a', iso(1), 1000, turn='t1'), sub]
        hits = limits.limit_hits(recs, [obs('r', iso(30), turn='t2', quota=five_hour(120))], TABLE)
        self.assertEqual(hits[0]['turn'], ('claude', 's1', 't2'))  # a rejection carries its own turn
        top = hits[0]['window']['top'][0]
        payload = report.build_report(recs, {}, redact=True, limit_hits=hits, now=datetime(2026, 9, 5, tzinfo=UTC))
        columns = payload['columns']
        ordinal = columns['prompt'][[i for i, x in enumerate(columns['id']) if x == 2][0]]  # the subagent row (second observation)
        self.assertEqual(payload['limit_hits'][0]['window']['top'][0]['prompt'], ordinal)
        self.assertEqual(top['turn'][2], 't1')  # usage-only assignment: the same as top_prompts and the cards
        self.assertIsNone(payload['limit_hits'][0]['prompt'])  # the rejection-only turn has no card


class Template(unittest.TestCase):
    def test_turn_row_variables_are_declared(self):
        # The script is strict: assigning to an undeclared name throws on the first attributed turn and the report never initializes.
        page = (Path(report.__file__).with_name('report_template.html')).read_text(encoding='utf-8')
        m = re.search(r"list\.forEach\(\(p,i\)=>\{(const tr=.*?);\[String\(i\+1\)", page)
        self.assertIsNotNone(m)
        statement = m.group(1)
        self.assertRegex(statement, r"^const tr=el\('tr',undefined,'prompt-row'\),c=.*,n=.*;tr\.id=")


class Cli(unittest.TestCase):
    def test_top_marks_the_turn_that_hit_a_limit(self):
        import contextlib
        import io
        from tokenatlas.__main__ import main
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / 'logs', session_rows())
            db = str(root / 'state' / 'h.sqlite3')
            def run(*args):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    code = main(['--db', db, *args])
                return code, out.getvalue()
            self.assertEqual(run('refresh', '--harness', 'claude', '--root', str(root / 'logs'))[0], 0)
            code, text = run('top', '--json')
            prompts = {p['turn_id']: p for p in json.loads(text)['prompts']}
            self.assertEqual(prompts['t2']['limit_hit']['reached'], 'five_hour')
            self.assertEqual(prompts['t2']['limit_hit']['window_minutes'], 300)
            self.assertNotIn('limit_hit', prompts['t1'])
            self.assertEqual(prompts['t2']['requests'], 1)  # the three rejected retries are not requests
            self.assertIn('[Hit the 5-hour limit]', run('top')[1])
            def payload(*extra):
                out = root / 'r.html'
                run('report', '--html', str(out), *extra)
                page = out.read_text(encoding='utf-8')
                found = re.search(r'id="report-data"[^>]*>([^<]+)<', page).group(1)
                return json.loads(gzip.decompress(base64.b64decode(found)).decode())
            self.assertEqual(len(payload().get('limit_hits', [])), 1)
            self.assertEqual(len(payload('--harness', 'claude').get('limit_hits', [])), 1)
            self.assertNotIn('limit_hits', payload('--harness', 'codex'))
            self.assertNotIn('limit_hits', payload('--start', '2026-09-05T00:00:00+00:00'))
            self.assertEqual(len(payload('--start', '2026-09-01T00:00:00+00:00', '--end', '2026-09-10T00:00:00+00:00').get('limit_hits', [])), 1)
            code, text = run('insights', '--json')
            self.assertTrue(any(f['id'] == 'limit_hits' for f in json.loads(text)['facts']))


class CliScope(unittest.TestCase):
    def test_scoped_reports_keep_rejection_only_turns_and_filter_by_provider(self):
        import contextlib
        import io
        from tokenatlas.__main__ import main
        from test_why_codex import _limits, _meta, _quota_call, _window, _write_rollout
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / 'logs', [user('t1', 0), call('a', 1, 5000), user('t2', 30), rejected('r1', 31)])
            reset = int((T0 + timedelta(hours=2)).timestamp())
            _write_rollout(root / 'codex' / 'rollout-c.jsonl', [_meta('c1'), _quota_call(stamp(5), 1, _limits(
                primary=_window(100.0, 300, reset), rate_limit_reached_type='rate_limit_reached'))])
            db = str(root / 'state' / 'h.sqlite3')
            def run(*args):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    return main(['--db', db, *args])
            self.assertEqual(run('refresh', '--harness', 'claude', '--root', str(root / 'logs')), 0)
            self.assertEqual(run('refresh', '--harness', 'codex', '--root', str(root / 'codex')), 0)
            def hits(*extra):
                out = root / 'r.html'
                run('report', '--html', str(out), *extra)
                found = re.search(r'id="report-data"[^>]*>([^<]+)<', out.read_text(encoding='utf-8')).group(1)
                return [h['harness'] for h in json.loads(gzip.decompress(base64.b64decode(found)).decode()).get('limit_hits', [])]
            self.assertEqual(sorted(hits()), ['claude', 'codex'])
            for extra in (['--project', '/work/app'], ['--session', 's1'], ['--session', 'claude:s1'], ['--turn', 't2']):
                self.assertEqual(hits(*extra), ['claude'], extra)
            self.assertEqual(hits('--provider', 'anthropic'), ['claude'])
            self.assertEqual(hits('--provider', 'openai'), ['codex'])


if __name__ == '__main__':
    unittest.main()
