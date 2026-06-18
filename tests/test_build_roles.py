#!/usr/bin/env python3
"""Tests for harness.update_thread_state, get_thread_states and _build_roles.

Covers creation/update semantics, default values, timestamp behaviour, the
public read API, and the per-role aggregation rules of _build_roles.
"""
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402


class TestUpdateThreadState(unittest.TestCase):
    """update_thread_state: create / update / timestamp semantics."""

    def setUp(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def tearDown(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def test_creates_entry_with_defaults(self):
        """A brand-new entry starts with kind='?', role=None, status='idle', etc."""
        harness.update_thread_state('new-thread')
        with harness.THREAD_STATES_LOCK:
            entry = harness.THREAD_STATES['new-thread']
        self.assertEqual(entry['kind'], '?')
        self.assertIsNone(entry['role'])
        self.assertEqual(entry['location'], 'local')
        self.assertEqual(entry['node'], 'local')
        self.assertEqual(entry['model'], '?')
        self.assertEqual(entry['status'], 'idle')
        self.assertEqual(entry['current_file'], '')
        self.assertEqual(entry['current_task'], '')
        self.assertEqual(entry['calls'], 0)
        self.assertEqual(entry['errors'], 0)
        self.assertEqual(entry['avg_ms'], 0)
        self.assertIsInstance(entry['started_at'], float)
        self.assertIsInstance(entry['last_update'], float)

    def test_updates_existing_entry(self):
        """Calling update_thread_state again merges kwargs into the existing entry."""
        harness.update_thread_state('t-1', kind='hunter', status='busy', calls=3,
                                    model='ollama:qwen3')
        harness.update_thread_state('t-1', calls=7, errors=2)
        with harness.THREAD_STATES_LOCK:
            entry = harness.THREAD_STATES['t-1']
        self.assertEqual(entry['kind'], 'hunter')
        self.assertEqual(entry['status'], 'busy')
        self.assertEqual(entry['model'], 'ollama:qwen3')
        self.assertEqual(entry['calls'], 7)
        self.assertEqual(entry['errors'], 2)

    def test_updates_last_update_timestamp(self):
        """last_update is bumped to time.time() on every call."""
        harness.update_thread_state('t-ts', kind='hunter')
        with harness.THREAD_STATES_LOCK:
            first = harness.THREAD_STATES['t-ts']['last_update']
        time.sleep(0.05)
        harness.update_thread_state('t-ts', calls=1)
        with harness.THREAD_STATES_LOCK:
            second = harness.THREAD_STATES['t-ts']['last_update']
        self.assertGreater(second, first)

    def test_keeps_started_at_from_first_call(self):
        """started_at is set on the first call and NEVER overwritten later."""
        harness.update_thread_state('t-start', kind='hunter')
        with harness.THREAD_STATES_LOCK:
            original = harness.THREAD_STATES['t-start']['started_at']
        time.sleep(0.05)
        harness.update_thread_state('t-start', calls=99, status='busy')
        with harness.THREAD_STATES_LOCK:
            after = harness.THREAD_STATES['t-start']['started_at']
        self.assertEqual(original, after)


class TestGetThreadStates(unittest.TestCase):
    """get_thread_states: shape, derived fields, copy semantics."""

    def setUp(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def tearDown(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def test_returns_list_of_dicts(self):
        """get_thread_states returns a list whose elements are dicts."""
        harness.update_thread_state('g-0', kind='hunter', status='busy')
        harness.update_thread_state('g-1', kind='filter', status='idle')
        out = harness.get_thread_states()
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 2)
        for entry in out:
            self.assertIsInstance(entry, dict)

    def test_adds_name_age_idle_fields(self):
        """Every returned entry carries name, age_sec, idle_sec."""
        harness.update_thread_state('ga-0', kind='hunter', status='busy')
        harness.update_thread_state('ga-1', kind='filter', status='idle')
        out = harness.get_thread_states()
        names = sorted(e['name'] for e in out)
        self.assertEqual(names, ['ga-0', 'ga-1'])
        for entry in out:
            self.assertIn('name', entry)
            self.assertIn('age_sec', entry)
            self.assertIn('idle_sec', entry)
            self.assertIsInstance(entry['age_sec'], int)
            self.assertIsInstance(entry['idle_sec'], int)
            self.assertGreaterEqual(entry['age_sec'], 0)
            self.assertGreaterEqual(entry['idle_sec'], 0)

    def test_is_a_copy_mutations_dont_leak(self):
        """Mutating the returned list/dicts must not affect THREAD_STATES."""
        harness.update_thread_state('gcp-0', kind='hunter', status='busy', calls=5)
        out = harness.get_thread_states()
        out.clear()
        out.append({'sentinel': True})
        # Now re-read; the real state must be intact.
        out2 = harness.get_thread_states()
        self.assertEqual(len(out2), 1)
        self.assertEqual(out2[0]['calls'], 5)
        out2[0]['calls'] = 9999
        with harness.THREAD_STATES_LOCK:
            self.assertEqual(harness.THREAD_STATES['gcp-0']['calls'], 5)


class TestBuildRolesBasics(unittest.TestCase):
    """_build_roles: shape, default 5 roles, empty-state behaviour."""

    def setUp(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def tearDown(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def test_returns_five_default_roles(self):
        """_build_roles always returns the 5 canonical roles in fixed order."""
        roles = harness._build_roles()
        self.assertEqual(len(roles), 5)
        self.assertEqual([r['role'] for r in roles],
                         ['hunter', 'filter', 'judge', 'fixer', 'cloud'])
        for r in roles:
            for key in ('role', 'label', 'configured_n', 'live_n', 'busy_n',
                        'idle_n', 'done_n', 'quarantined_n', 'calls_total',
                        'calls_per_min', 'timeouts', 'errors', 'models'):
                self.assertIn(key, r)
            self.assertIsInstance(r['models'], list)

    def test_empty_states_yields_all_zero_counts(self):
        """With THREAD_STATES empty, every role has all-numeric counts of 0."""
        roles = harness._build_roles()
        for r in roles:
            self.assertEqual(r['live_n'], 0)
            self.assertEqual(r['busy_n'], 0)
            self.assertEqual(r['idle_n'], 0)
            self.assertEqual(r['done_n'], 0)
            self.assertEqual(r['calls_total'], 0)
            self.assertEqual(r['timeouts'], 0)
            self.assertEqual(r['errors'], 0)
            self.assertEqual(r['quarantined_n'], 0)
            self.assertEqual(r['models'], [])


class TestBuildRolesAggregation(unittest.TestCase):
    """_build_roles: aggregating calls, errors, models and status counters."""

    def setUp(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def tearDown(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def test_aggregates_calls_total(self):
        """calls_total is the sum of all thread calls in the role."""
        harness.update_thread_state('h-0', kind='hunter', role='hunter',
                                    status='busy', calls=10)
        harness.update_thread_state('h-1', kind='hunter', role='hunter',
                                    status='busy', calls=5)
        harness.update_thread_state('h-2', kind='hunter', role='hunter',
                                    status='busy', calls=3)
        roles = harness._build_roles()
        hunter = next(r for r in roles if r['role'] == 'hunter')
        self.assertEqual(hunter['calls_total'], 18)

    def test_aggregates_errors(self):
        """errors are summed per role."""
        harness.update_thread_state('e-0', kind='hunter', role='hunter',
                                    status='busy', errors=1)
        harness.update_thread_state('e-1', kind='hunter', role='hunter',
                                    status='busy', errors=4)
        roles = harness._build_roles()
        hunter = next(r for r in roles if r['role'] == 'hunter')
        self.assertEqual(hunter['errors'], 5)

    def test_aggregates_models_set_deduped_sorted(self):
        """models[] is the union of thread models, deduped and sorted."""
        harness.update_thread_state('m-0', kind='hunter', role='hunter',
                                    status='busy', model='ollama:zeta')
        harness.update_thread_state('m-1', kind='hunter', role='hunter',
                                    status='busy', model='ollama:alpha')
        harness.update_thread_state('m-2', kind='hunter', role='hunter',
                                    status='busy', model='ollama:zeta')  # duplicate
        roles = harness._build_roles()
        hunter = next(r for r in roles if r['role'] == 'hunter')
        self.assertEqual(hunter['models'], ['ollama:alpha', 'ollama:zeta'])


class TestBuildRolesStatusCounters(unittest.TestCase):
    """_build_roles: busy / idle / done counters and live_n semantics."""

    def setUp(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def tearDown(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def test_busy_thread_increments_busy_and_live(self):
        """status='busy' → busy_n and live_n both go up by 1."""
        harness.update_thread_state('bs-0', kind='hunter', role='hunter', status='busy')
        roles = harness._build_roles()
        hunter = next(r for r in roles if r['role'] == 'hunter')
        self.assertEqual(hunter['busy_n'], 1)
        self.assertEqual(hunter['live_n'], 1)
        self.assertEqual(hunter['idle_n'], 0)
        self.assertEqual(hunter['done_n'], 0)

    def test_idle_thread_increments_idle_and_live(self):
        """status='idle' → idle_n and live_n both go up by 1."""
        harness.update_thread_state('id-0', kind='filter', role='filter', status='idle')
        roles = harness._build_roles()
        f = next(r for r in roles if r['role'] == 'filter')
        self.assertEqual(f['idle_n'], 1)
        self.assertEqual(f['live_n'], 1)
        self.assertEqual(f['busy_n'], 0)
        self.assertEqual(f['done_n'], 0)

    def test_done_thread_increments_done_only(self):
        """status='done' → done_n=1, live_n=0 (thread is no longer 'live')."""
        harness.update_thread_state('dn-0', kind='judge', role='judge', status='done')
        roles = harness._build_roles()
        j = next(r for r in roles if r['role'] == 'judge')
        self.assertEqual(j['done_n'], 1)
        self.assertEqual(j['live_n'], 0)
        self.assertEqual(j['busy_n'], 0)
        self.assertEqual(j['idle_n'], 0)


class TestBuildRolesQuarantine(unittest.TestCase):
    """_build_roles: quarantined_n reflects quarantined_models() output."""

    def setUp(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def tearDown(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def test_quarantined_n_counts_overlap_with_models(self):
        """quarantined_n = #models in role that appear in quarantined_models()."""
        harness.update_thread_state('q-0', kind='hunter', role='hunter',
                                    status='busy', model='ollama:dead-1', calls=1)
        harness.update_thread_state('q-1', kind='hunter', role='hunter',
                                    status='busy', model='ollama:alive-1', calls=1)
        harness.update_thread_state('q-2', kind='hunter', role='hunter',
                                    status='busy', model='ollama:dead-2', calls=1)
        with patch('harness.quarantined_models',
                   return_value=['ollama:dead-1', 'ollama:dead-2']):
            roles = harness._build_roles()
        hunter = next(r for r in roles if r['role'] == 'hunter')
        self.assertEqual(hunter['quarantined_n'], 2)
        # Healthy model must not be counted.
        self.assertEqual(sorted(hunter['models']),
                         ['ollama:alive-1', 'ollama:dead-1', 'ollama:dead-2'])

    def test_quarantined_n_zero_when_no_models_quarantined(self):
        """quarantined_n=0 when quarantined_models() is empty."""
        harness.update_thread_state('qn-0', kind='hunter', role='hunter',
                                    status='busy', model='ollama:alive', calls=1)
        with patch('harness.quarantined_models', return_value=[]):
            roles = harness._build_roles()
        hunter = next(r for r in roles if r['role'] == 'hunter')
        self.assertEqual(hunter['quarantined_n'], 0)


class TestBuildRolesThroughput(unittest.TestCase):
    """_build_roles: calls_per_min is derived from uptime and calls_total."""

    def setUp(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()
        with harness.STATS_LOCK:
            self._original_started_at = harness.STATS['started_at']
        self._original_quarantine = list(harness.INITIAL_QUARANTINE)

    def tearDown(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()
        with harness.STATS_LOCK:
            harness.STATS['started_at'] = self._original_started_at
        harness.INITIAL_QUARANTINE = tuple(self._original_quarantine)

    def test_calls_per_min_with_known_uptime(self):
        """calls_per_min = calls_total / (uptime_minutes) rounded to 2 decimals."""
        harness.update_thread_state('cpm-0', kind='hunter', role='hunter',
                                    status='busy', calls=600)
        with harness.STATS_LOCK:
            # 2 minutes ago → 600 calls / 2 min = 300 calls/min
            harness.STATS['started_at'] = time.time() - 120
        with patch('harness.quarantined_models', return_value=[]):
            roles = harness._build_roles()
        hunter = next(r for r in roles if r['role'] == 'hunter')
        self.assertEqual(hunter['calls_per_min'], 300.0)
        self.assertEqual(hunter['calls_total'], 600)

    def test_calls_per_min_zero_when_no_calls(self):
        """With zero calls, calls_per_min=0 regardless of uptime."""
        harness.update_thread_state('cpm-1', kind='hunter', role='hunter',
                                    status='busy', calls=0)
        with harness.STATS_LOCK:
            harness.STATS['started_at'] = time.time()  # just started
        with patch('harness.quarantined_models', return_value=[]):
            roles = harness._build_roles()
        hunter = next(r for r in roles if r['role'] == 'hunter')
        self.assertEqual(hunter['calls_per_min'], 0.0)
        self.assertEqual(hunter['calls_total'], 0)


class TestBuildRolesBucketing(unittest.TestCase):
    """_build_roles: how role / kind values map to buckets."""

    def setUp(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def tearDown(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def test_role_arbiter_excluded(self):
        """role='arbiter' is not in ROLE_ORDER → thread is skipped entirely."""
        harness.update_thread_state('arb-0', kind='arbiter', role='arbiter',
                                    status='busy', calls=5)
        with patch('harness.quarantined_models', return_value=[]):
            roles = harness._build_roles()
        # No role in the output should reflect this thread.
        for r in roles:
            self.assertEqual(r['calls_total'], 0)
            self.assertEqual(r['busy_n'], 0)

    def test_excludes_unknown_role_values(self):
        """role values outside the canonical set are silently skipped."""
        harness.update_thread_state('unk-0', kind='mystery', role='zzz',
                                    status='busy', calls=99)
        harness.update_thread_state('unk-1', kind='mystery', role='zzz',
                                    status='busy', calls=1)
        with patch('harness.quarantined_models', return_value=[]):
            roles = harness._build_roles()
        for r in roles:
            self.assertEqual(r['calls_total'], 0)
            self.assertEqual(r['busy_n'], 0)

    def test_role_none_kind_cloud_buckets_as_cloud(self):
        """role=None + kind='cloud' → bucket 'cloud' (kind is the fallback)."""
        harness.update_thread_state('nkc-0', kind='cloud', status='busy',
                                    model='gemini:flash', calls=2)
        with patch('harness.quarantined_models', return_value=[]):
            roles = harness._build_roles()
        cloud = next(r for r in roles if r['role'] == 'cloud')
        self.assertEqual(cloud['busy_n'], 1)
        self.assertEqual(cloud['calls_total'], 2)
        self.assertIn('gemini:flash', cloud['models'])
        # And NOT in hunter.
        hunter = next(r for r in roles if r['role'] == 'hunter')
        self.assertEqual(hunter['calls_total'], 0)

    def test_role_none_kind_hunter_buckets_as_hunter(self):
        """role=None + kind='hunter' → bucket 'hunter' (kind is the fallback)."""
        harness.update_thread_state('nkh-0', kind='hunter', status='busy',
                                    model='ollama:30b', calls=4)
        with patch('harness.quarantined_models', return_value=[]):
            roles = harness._build_roles()
        hunter = next(r for r in roles if r['role'] == 'hunter')
        self.assertEqual(hunter['busy_n'], 1)
        self.assertEqual(hunter['calls_total'], 4)
        cloud = next(r for r in roles if r['role'] == 'cloud')
        self.assertEqual(cloud['calls_total'], 0)

    def test_explicit_role_wins_over_kind(self):
        """role='judge' + kind='hunter' → bucket 'judge' (role takes precedence)."""
        harness.update_thread_state('xp-0', kind='hunter', role='judge',
                                    status='busy', calls=2)
        with patch('harness.quarantined_models', return_value=[]):
            roles = harness._build_roles()
        judge = next(r for r in roles if r['role'] == 'judge')
        self.assertEqual(judge['busy_n'], 1)
        self.assertEqual(judge['calls_total'], 2)
        hunter = next(r for r in roles if r['role'] == 'hunter')
        self.assertEqual(hunter['busy_n'], 0)
        self.assertEqual(hunter['calls_total'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
