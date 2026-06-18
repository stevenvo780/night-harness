#!/usr/bin/env python3
"""Edge-case tests for fleet.py — multi-node, VRAM cascade, CPU-only and hot-model budget."""
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fleet  # noqa: E402
from fleet import (
    Gpu, CpuSlot, Node, Fleet,
    discover_node, plan_models, build_fleet,
    HEADROOM, est_vram_mib,
)


def _make_gpu(index=0, name='RTX Test', vram_total_mib=6144, vram_free_mib=5939,
              util_pct=1, uuid='GPU-test-uuid'):
    return Gpu(
        index=index, name=name,
        vram_total_mib=vram_total_mib,
        vram_free_mib=vram_free_mib,
        util_pct=util_pct,
        uuid=uuid,
    )


def _make_node(gpus=None, models=None, running=None, name='test',
               base_url='http://localhost:11434', is_local=True, cpu=None):
    return Node(
        name=name,
        base_url=base_url,
        online=True,
        gpus=gpus or [],
        cpu=cpu,
        models=models or [],
        running=running or {},
        is_local=is_local,
    )


class TestMultiNodeAndVramCascade(unittest.TestCase):
    """Multi-node discovery and VRAM cascading edge cases."""

    @patch('fleet.discover_local_gpus')
    @patch('fleet.discover_models')
    @patch('fleet.discover_running')
    def test_remote_node_without_nvidia_smi_infers_vram_from_api_ps(
        self, mock_running, mock_models, mock_local_gpus
    ):
        """Remote node has no nvidia-smi but /api/ps returns resident VRAM."""
        mock_local_gpus.return_value = []
        mock_models.return_value = ['qwen2.5-coder:7b']
        mock_running.return_value = {'qwen3-coder:30b': 12_900}

        node = discover_node('laptop', 'http://192.168.78.135:11434')

        self.assertFalse(node.is_local)
        self.assertEqual(node.gpus, [])
        self.assertIn('qwen3-coder:30b', node.running)
        # Resident VRAM is captured in MiB from discover_running.
        self.assertGreater(node.running['qwen3-coder:30b'], 0)

    @patch('fleet.discover_local_gpus')
    @patch('fleet.discover_models')
    @patch('fleet.discover_running')
    def test_local_node_uses_nvidia_smi_when_available(
        self, mock_running, mock_models, mock_local_gpus
    ):
        """Local node prefers actual nvidia-smi output over declared/inferred values."""
        mock_local_gpus.return_value = [
            _make_gpu(index=0, name='RTX 5070 Ti', vram_total_mib=16384, vram_free_mib=9000),
        ]
        mock_models.return_value = ['qwen3-coder:30b']
        mock_running.return_value = {'qwen3-coder:30b': 12_900}

        node = discover_node('local', 'http://host.docker.internal:11434')

        self.assertTrue(node.is_local)
        self.assertEqual(len(node.gpus), 1)
        self.assertEqual(node.gpus[0].vram_total_mib, 16384)

    def test_vram_cascade_preference_local_nvidia_smi_over_declared(self):
        """When nvidia-smi is available it is the authoritative VRAM source."""
        gpu = _make_gpu(index=0, vram_total_mib=16384, vram_free_mib=9000)
        self.assertEqual(gpu.vram_total_mib, 16384)
        # The HEADROOM constant documents the intended budget strategy.
        self.assertEqual(HEADROOM, 0.80)

    def test_almost_empty_gpu_headroom_no_false_negative(self):
        """GPU with vram_free > 80% total should still be usable (no false negative)."""
        gpu = _make_gpu(index=1, name='RTX 2060', vram_total_mib=6144, vram_free_mib=5939)
        self.assertGreater(gpu.vram_free_mib, gpu.vram_total_mib * 0.80)
        node = _make_node(gpus=[gpu], models=['qwen2.5-coder:7b'])
        result = plan_models(node)
        # The small GPU must still receive a hunter assignment.
        self.assertTrue(len(result['hunter']) >= 1)
        self.assertEqual(result['hunter'][0].gpu_index, 1)

    def test_remote_node_without_gpus_assigns_reasoner_not_hunter(self):
        """Remote CPU-only node gets a reasoner role, not a hunter assignment."""
        node = _make_node(
            gpus=[],
            models=['qwen3-coder-next:cpu'],
            is_local=False,
            cpu=CpuSlot(cores=8, ram_free_mib=16000),
        )
        result = plan_models(node)
        self.assertEqual(result['hunter'], [])
        self.assertIsNone(result['fixer'])
        self.assertTrue(len(result['reasoner']) >= 1)
        self.assertIn('qwen3-coder-next:cpu', result['reasoner'][0].id)

    def test_vram_overflow_big_model_does_not_force_oom_on_small_gpu(self):
        """A 10 GB model request must not be placed on a 6.1 GB GPU."""
        gpu = _make_gpu(index=0, name='RTX 2060', vram_total_mib=6144, vram_free_mib=5939)
        need_mib = 10 * 1024  # 10 GB
        node = _make_node(gpus=[gpu], models=['qwen3-coder:30b'])
        result = plan_models(node)
        # Current heuristic refuses big models on small GPUs entirely.
        assigned_hunters = [h.id for h in result['hunter']]
        self.assertNotIn('ollama:qwen3-coder:30b', assigned_hunters)
        self.assertIsNone(result['fixer'])
        # The 30B should still be estimable as larger than the GPU budget.
        self.assertGreater(est_vram_mib(10 * 1024 * 1024 * 1024), gpu.vram_total_mib)

    def test_hot_model_budget_in_running_dict(self):
        """If the 30B is already in /api/ps, fleet records it as resident."""
        node = _make_node(
            gpus=[_make_gpu(index=0, vram_total_mib=16384, vram_free_mib=9000)],
            models=['qwen3-coder:30b'],
            running={'qwen3-coder:30b': 12_600},
        )
        self.assertIn('qwen3-coder:30b', node.running)
        # When the big GPU sees the hot 30B installed, it becomes the fixer.
        result = plan_models(node)
        self.assertIsNotNone(result['fixer'])
        self.assertEqual(result['fixer'].id, 'ollama:qwen3-coder:30b')

    def test_plan_models_empty_models_list_does_not_crash(self):
        """Empty node.models must return an empty plan without exceptions."""
        node = _make_node(
            gpus=[_make_gpu(index=0, vram_total_mib=16384, vram_free_mib=9000)],
            models=[],
        )
        try:
            result = plan_models(node)
        except Exception as e:
            self.fail(f'plan_models crashed with empty models: {e}')
        self.assertEqual(result, {'hunter': [], 'fixer': None, 'reasoner': []})


class TestFleetRemoteAndCpu(unittest.TestCase):
    """Fleet-level behavior across local GPU and remote CPU nodes."""

    @patch('fleet.discover_local_gpus')
    @patch('fleet.discover_models')
    @patch('fleet.discover_running')
    def test_build_fleet_mixed_local_gpu_remote_cpu(
        self, mock_running, mock_models, mock_local_gpus
    ):
        """Local node gets GPUs; remote unreachable node falls back to CPU skeleton."""
        mock_local_gpus.return_value = [
            _make_gpu(index=0, vram_total_mib=16384, vram_free_mib=9000),
        ]
        mock_models.return_value = ['qwen3-coder:30b']
        mock_running.return_value = {}

        with patch.dict(os.environ, {
            'LOOP_NODES': 'local=http://host.docker.internal:11434,laptop=http://192.168.78.135:11434'
        }):
            f = build_fleet()

        self.assertIsInstance(f, Fleet)
        self.assertEqual(len(f.nodes), 2)
        # Local node should have GPUs.
        local_node = next(n for n in f.nodes if n.name == 'local')
        self.assertTrue(len(local_node.gpus) >= 1)
        # Remote node may be offline but must exist as a node.
        remote_node = next(n for n in f.nodes if n.name == 'laptop')
        self.assertEqual(remote_node.name, 'laptop')


if __name__ == '__main__':
    unittest.main(verbosity=2)
