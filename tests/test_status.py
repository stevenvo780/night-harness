#!/usr/bin/env python3
"""Regresión del status schema v2 y _build_roles — corrige los 2 críticos del review B/C/D.

CRITICAL 2: el default role='?' (truthy) hacía que _build_roles saltara TODO thread.
Schema v2: write_status debe emitir schema_version=2 y orchestrator.*_n como int.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402


class TestBuildRoles(unittest.TestCase):
    def setUp(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def test_hunter_bucketed_via_kind_fallback(self):
        # role=None (default) + kind='hunter' → cae en bucket 'hunter' (antes se saltaba).
        harness.update_thread_state('hunter-0', kind='hunter', status='busy',
                                    model='ollama:qwen3-coder:30b', calls=3)
        roles = {r['role']: r for r in harness._build_roles()}
        self.assertEqual(roles['hunter']['live_n'], 1)
        self.assertEqual(roles['hunter']['busy_n'], 1)
        self.assertEqual(roles['hunter']['calls_total'], 3)

    def test_cloud_filter_judge_fixer_buckets(self):
        harness.update_thread_state('cloud-0', kind='cloud', status='idle', model='gemini:flash')
        harness.update_thread_state('filter-0', kind='filter', status='busy', model='minimax/M3')
        harness.update_thread_state('judge-0', kind='judge', status='busy', model='codex:gpt')
        harness.update_thread_state('fixer-x', kind='fixer', status='busy', model='ollama:30b')
        roles = {r['role']: r for r in harness._build_roles()}
        self.assertEqual(roles['cloud']['idle_n'], 1)
        self.assertEqual(roles['filter']['busy_n'], 1)
        self.assertEqual(roles['judge']['busy_n'], 1)
        self.assertEqual(roles['fixer']['busy_n'], 1)

    def test_explicit_role_takes_precedence(self):
        harness.update_thread_state('w-0', role='judge', kind='hunter', status='busy')
        roles = {r['role']: r for r in harness._build_roles()}
        self.assertEqual(roles['judge']['busy_n'], 1)
        self.assertEqual(roles['hunter']['busy_n'], 0)


class TestWriteStatusSchemaV2(unittest.TestCase):
    def test_schema_v2_and_int_counts(self):
        harness.write_status()
        d = json.loads(harness.STATUS_FILE.read_text())
        self.assertEqual(d.get('schema_version'), 2)
        self.assertIn('funnel', d)
        self.assertIsInstance(d['roles'], list)
        # El crash original: la TUI hacía len() sobre estos → ahora son int directos.
        self.assertIsInstance(d['orchestrator']['hunting_prompts_n'], int)
        self.assertIsInstance(d['orchestrator']['e2e_journeys_n'], int)


if __name__ == '__main__':
    unittest.main(verbosity=2)
