#!/usr/bin/env python3
"""Schema v2 status edge-case tests — _build_nodes, thread location/node/role fields,
funnel counters and orchestrator int counts.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402


class TestBuildNodesSchemaV2(unittest.TestCase):
    """_build_nodes contract: at least one node, correct GPU/model shape."""

    def setUp(self):
        self._fleet_path = Path('/tmp/harness-fleet.json')
        if self._fleet_path.exists():
            self._existing = self._fleet_path.read_text()
            self._fleet_path.unlink()
        else:
            self._existing = None

    def tearDown(self):
        if self._existing is not None:
            self._fleet_path.write_text(self._existing)
        elif self._fleet_path.exists():
            self._fleet_path.unlink()

    def test_build_nodes_returns_at_least_local(self):
        """_build_nodes always returns a non-empty list containing at least local."""
        nodes = harness._build_nodes()
        self.assertIsInstance(nodes, list)
        self.assertGreaterEqual(len(nodes), 1)
        ids = [n['id'] for n in nodes]
        self.assertIn('local', ids)

    def test_each_node_has_required_fields(self):
        """Every node exposes id, host, alive, latency_ms, gpus, ollama_models."""
        for node in harness._build_nodes():
            for key in ('id', 'host', 'alive', 'latency_ms', 'gpus', 'ollama_models'):
                self.assertIn(key, node)
            self.assertIsInstance(node['gpus'], list)
            self.assertIsInstance(node['ollama_models'], list)

    def test_gpu_fields_required(self):
        """Every GPU in nodes[].gpus has the required schema fields."""
        for node in harness._build_nodes():
            for gpu in node['gpus']:
                for key in ('index', 'name', 'vram_used_mib', 'vram_total_mib',
                            'util_pct', 'loaded_models'):
                    self.assertIn(key, gpu)
                self.assertIsInstance(gpu['loaded_models'], list)

    def test_build_nodes_from_fleet_file(self):
        """When /tmp/harness-fleet.json exists, it drives the nodes output."""
        self._fleet_path.write_text(json.dumps({
            'nodes': [{
                'name': 'laptop',
                'base_url': 'http://192.168.78.135:11434',
                'online': True,
                'gpus': [{
                    'index': 0,
                    'name': 'RTX 3060',
                    'vram_total_mib': 12288,
                    'vram_free_mib': 10240,
                    'util_pct': 5,
                    'assigned_model': 'qwen2.5-coder:7b',
                }],
                'running': {'qwen2.5-coder:7b': 5_300},
            }]
        }))
        nodes = harness._build_nodes()
        ids = [n['id'] for n in nodes]
        self.assertIn('laptop', ids)
        laptop = next(n for n in nodes if n['id'] == 'laptop')
        self.assertTrue(laptop['alive'])
        self.assertEqual(len(laptop['gpus']), 1)
        self.assertEqual(laptop['gpus'][0]['name'], 'RTX 3060')
        self.assertEqual(laptop['ollama_models'][0]['name'], 'qwen2.5-coder:7b')


class TestWriteStatusThreadsAndSchema(unittest.TestCase):
    """write_status schema v2 invariants around threads, orchestrator and funnel."""

    def setUp(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def tearDown(self):
        with harness.THREAD_STATES_LOCK:
            harness.THREAD_STATES.clear()

    def test_threads_have_location_node_role(self):
        """Each emitted thread has location, node and role fields with valid values."""
        harness.update_thread_state(
            'hunter-0', kind='hunter', role='hunter',
            location='local', node='gpu0',
            model='ollama:qwen3-coder:30b', status='busy', calls=1
        )
        harness.update_thread_state(
            'filter-0', kind='filter', role='filter',
            location='remote', node='laptop',
            model='minimax/M3', status='idle', calls=0
        )
        harness.update_thread_state(
            'judge-0', kind='judge', role='judge',
            location='remote', node='cloud',
            model='codex:gpt', status='busy', calls=2
        )
        harness.update_thread_state(
            'fixer-0', kind='fixer', role='fixer',
            location='local', node='gpu0',
            model='ollama:qwen3-coder:30b', status='busy', calls=1
        )
        harness.update_thread_state(
            'arbiter-0', kind='arbiter', role='arbiter',
            location='remote', node='cloud',
            model='claude:opus', status='idle', calls=0
        )

        harness.write_status()
        payload = json.loads(harness.STATUS_FILE.read_text())
        threads = payload['threads']
        self.assertEqual(len(threads), 5)

        valid_roles = {'hunter', 'filter', 'judge', 'fixer', 'arbiter'}
        valid_locations = {'local', 'remote'}
        for t in threads:
            self.assertIn(t['role'], valid_roles)
            self.assertIn(t['location'], valid_locations)
            self.assertIsInstance(t['node'], str)
            self.assertTrue(t['node'])

    def test_empty_threads_nodes_not_empty(self):
        """Even with zero threads, nodes[] is never empty."""
        harness.write_status()
        payload = json.loads(harness.STATUS_FILE.read_text())
        self.assertEqual(payload['threads'], [])
        self.assertIsInstance(payload['nodes'], list)
        self.assertGreaterEqual(len(payload['nodes']), 1)

    def test_schema_version_always_two(self):
        """schema_version is always present and equals 2."""
        harness.write_status()
        payload = json.loads(harness.STATUS_FILE.read_text())
        self.assertEqual(payload.get('schema_version'), 2)

    def test_orchestrator_counts_are_int(self):
        """orchestrator.hunting_prompts_n and e2e_journeys_n are ints."""
        harness.write_status()
        payload = json.loads(harness.STATUS_FILE.read_text())
        orch = payload['orchestrator']
        self.assertIsInstance(orch['hunting_prompts_n'], int)
        self.assertIsInstance(orch['e2e_journeys_n'], int)

    def test_funnel_has_nine_counters(self):
        """Funnel contains the nine required counters."""
        harness.write_status()
        payload = json.loads(harness.STATUS_FILE.read_text())
        funnel = payload['funnel']
        required = {
            'suspects_produced',
            'suspects_refuted',
            'suspects_known_fp',
            'bugs_confirmed',
            'fixes_attempted',
            'fixes_succeeded',
            'fixes_reverted',
            'fixes_to_review',
            'e2e_findings',
        }
        for key in required:
            self.assertIn(key, funnel)
            self.assertIsInstance(funnel[key], int)


if __name__ == '__main__':
    unittest.main(verbosity=2)
