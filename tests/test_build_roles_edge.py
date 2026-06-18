#!/usr/bin/env python3
"""Edge-case tests for harness._build_roles / update_thread_state / get_thread_states.

Covers concurrent updates, time-derived fields, configured_n mapping,
models-set edge cases, and a few role-precedence variants that go beyond
the basic 5-role shape tested in test_build_roles.py.
"""
import concurrent.futures
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402


class TestRaceConditions(unittest.TestCase):
    """Concurrent updates to THREAD_STATES must not lose data."""

    def setUp(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def tearDown(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def test_update_thread_state_race_same_name_no_data_loss(self):
        """10 threads incrementing the same name → final calls = 10."""
        def worker(_i):
            with harness.THREAD_STATES_LOCK:
                cur = harness.THREAD_STATES.get('race-same', {}).get('calls', 0)
            harness.update_thread_state('race-same', calls=cur + 1)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            list(ex.map(worker, range(10)))

        with harness.THREAD_STATES_LOCK:
            entry = harness.THREAD_STATES['race-same']
        self.assertEqual(entry['calls'], 10)
        # started_at and last_update must both be valid floats.
        self.assertIsInstance(entry['started_at'], float)
        self.assertIsInstance(entry['last_update'], float)
        self.assertGreaterEqual(entry['last_update'], entry['started_at'])

    def test_build_roles_race_different_threads_all_counted(self):
        """10 threads adding 10 different names → all 10 visible to _build_roles."""
        def worker(i):
            harness.update_thread_state(
                f'race-diff-{i}',
                kind='hunter', role='hunter', status='busy',
                calls=i + 1, model=f'ollama:model-{i}',
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            list(ex.map(worker, range(10)))

        with patch('harness.quarantined_models', return_value=[]):
            roles = harness._build_roles()
        hunter = next(r for r in roles if r['role'] == 'hunter')
        self.assertEqual(hunter['live_n'], 10)
        self.assertEqual(hunter['busy_n'], 10)
        # 1+2+…+10 = 55
        self.assertEqual(hunter['calls_total'], 55)
        self.assertEqual(len(hunter['models']), 10)


class TestGetThreadStatesTimeFields(unittest.TestCase):
    """age_sec / idle_sec reflect time since started_at / last_update."""

    def setUp(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def tearDown(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def test_age_sec_reflects_time_since_started_at(self):
        """After a sleep, age_sec is at least the elapsed seconds (rounded to int)."""
        harness.update_thread_state('age-1', kind='hunter', status='busy')
        time.sleep(1.1)
        out = harness.get_thread_states()
        entry = next(e for e in out if e['name'] == 'age-1')
        # age_sec is `int(time.time() - started_at)`; allow 0/1 for clock granularity.
        self.assertGreaterEqual(entry['age_sec'], 1)

    def test_idle_sec_reflects_time_since_last_update(self):
        """If we stop updating, idle_sec grows even when age_sec grows."""
        harness.update_thread_state('idle-1', kind='hunter', status='busy')
        # Trigger one more update so last_update is fresh; then wait.
        harness.update_thread_state('idle-1', calls=1)
        time.sleep(1.1)
        out = harness.get_thread_states()
        entry = next(e for e in out if e['name'] == 'idle-1')
        self.assertGreaterEqual(entry['idle_sec'], 1)
        # age_sec and idle_sec are computed from different timestamps; they can differ.
        self.assertIsInstance(entry['idle_sec'], int)


class TestBuildRolesConfiguredN(unittest.TestCase):
    """configured_n matches the HUNTER_HILOS / FILTER_HILOS / etc. constants."""

    def setUp(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def tearDown(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def test_configured_n_reflects_env_constants(self):
        """Each role's configured_n matches the corresponding HILOS constant."""
        with patch('harness.quarantined_models', return_value=[]):
            roles = harness._build_roles()
        by_role = {r['role']: r for r in roles}
        self.assertEqual(by_role['hunter']['configured_n'],
                         harness.HUNTER_HILOS + harness.SECOND_HILOS)
        self.assertEqual(by_role['filter']['configured_n'], harness.FILTER_HILOS)
        self.assertEqual(by_role['judge']['configured_n'], harness.JUDGE_HILOS)
        self.assertEqual(by_role['fixer']['configured_n'], harness.FIXER_HILOS)
        self.assertEqual(by_role['cloud']['configured_n'], harness.CLOUD_HUNTER_HILOS)


class TestBuildRolesModelFiltering(unittest.TestCase):
    """models[] excludes empty / '?' / calls=0 entries (per spec)."""

    def setUp(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def tearDown(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def test_empty_model_excluded_from_models(self):
        """A thread with model='' is NOT in the role's models[]."""
        harness.update_thread_state('em-0', kind='hunter', role='hunter',
                                    status='busy', model='', calls=5)
        with patch('harness.quarantined_models', return_value=[]):
            roles = harness._build_roles()
        hunter = next(r for r in roles if r['role'] == 'hunter')
        self.assertEqual(hunter['models'], [])
        # calls_total still aggregates regardless of model.
        self.assertEqual(hunter['calls_total'], 5)

    def test_question_mark_model_excluded_from_models(self):
        """A thread with model='?' is NOT in the role's models[]."""
        harness.update_thread_state('qm-0', kind='hunter', role='hunter',
                                    status='busy', model='?', calls=5)
        harness.update_thread_state('qm-1', kind='hunter', role='hunter',
                                    status='busy', model='ollama:real', calls=3)
        with patch('harness.quarantined_models', return_value=[]):
            roles = harness._build_roles()
        hunter = next(r for r in roles if r['role'] == 'hunter')
        self.assertEqual(hunter['models'], ['ollama:real'])


class TestBuildRolesCloudBucketSemantics(unittest.TestCase):
    """Cloud bucket accepts threads whose role/kind resolves to 'cloud'."""

    def setUp(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def tearDown(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def test_cloud_bucket_via_role_or_kind(self):
        """role='cloud' and role=None+kind='cloud' both end up in 'cloud' bucket."""
        harness.update_thread_state('c-role', kind='hunter', role='cloud',
                                    status='busy', model='gemini:flash', calls=4)
        harness.update_thread_state('c-kind', kind='cloud', role=None,
                                    status='idle', model='opencode:glm', calls=1)
        # And one explicit hunter that must NOT leak into the cloud bucket.
        harness.update_thread_state('h-pure', kind='hunter', role='hunter',
                                    status='busy', model='ollama:qwen3', calls=2)
        with patch('harness.quarantined_models', return_value=[]):
            roles = harness._build_roles()
        cloud = next(r for r in roles if r['role'] == 'cloud')
        hunter = next(r for r in roles if r['role'] == 'hunter')
        self.assertEqual(cloud['busy_n'], 1)
        self.assertEqual(cloud['idle_n'], 1)
        self.assertEqual(cloud['calls_total'], 5)
        self.assertEqual(sorted(cloud['models']), ['gemini:flash', 'opencode:glm'])
        # The pure hunter is isolated from the cloud bucket.
        self.assertEqual(hunter['calls_total'], 2)
        self.assertNotIn('gemini:flash', hunter['models'])

    def test_explicit_role_hunter_overrides_kind_cloud(self):
        """role='hunter' + kind='cloud' → bucket 'hunter' (role wins)."""
        harness.update_thread_state('rh-0', kind='cloud', role='hunter',
                                    status='busy', model='ollama:qwen3', calls=2)
        with patch('harness.quarantined_models', return_value=[]):
            roles = harness._build_roles()
        hunter = next(r for r in roles if r['role'] == 'hunter')
        cloud = next(r for r in roles if r['role'] == 'cloud')
        self.assertEqual(hunter['calls_total'], 2)
        self.assertEqual(cloud['calls_total'], 0)


class TestBuildRolesQuarantineSums(unittest.TestCase):
    """quarantined_n sums quarantined models per role (cross-role independence)."""

    def setUp(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def tearDown(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def test_quarantined_n_per_role_independent(self):
        """A model quarantined in one role does not affect another's count."""
        # hunter: 2 models, 1 quarantined
        harness.update_thread_state('qh-0', kind='hunter', role='hunter',
                                    status='busy', model='ollama:zombie-1', calls=1)
        harness.update_thread_state('qh-1', kind='hunter', role='hunter',
                                    status='busy', model='ollama:healthy-1', calls=1)
        # filter: 1 model, also quarantined
        harness.update_thread_state('qf-0', kind='filter', role='filter',
                                    status='busy', model='ollama:zombie-1', calls=1)
        with patch('harness.quarantined_models', return_value=['ollama:zombie-1']):
            roles = harness._build_roles()
        by_role = {r['role']: r for r in roles}
        self.assertEqual(by_role['hunter']['quarantined_n'], 1)
        self.assertEqual(by_role['filter']['quarantined_n'], 1)
        self.assertEqual(by_role['judge']['quarantined_n'], 0)
        self.assertEqual(by_role['fixer']['quarantined_n'], 0)
        self.assertEqual(by_role['cloud']['quarantined_n'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
