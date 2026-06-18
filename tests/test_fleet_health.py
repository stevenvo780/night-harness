#!/usr/bin/env python3
"""Tests de fleet.py — health loop y status writer.

Cubre:
- _parse_loop_nodes (parsing de LOOP_NODES)
- write_fleet_status (atomic JSON write)
- _spec_info (ModelSpec -> dict)
- constantes HEALTH_INTERVAL_SECS y FLEET_STATUS_PATH
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock, mock_open

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fleet  # noqa: E402
from fleet import (  # noqa: E402
    Gpu, CpuSlot, Node, Fleet,
    _parse_loop_nodes, _spec_info,
    write_fleet_status, FLEET_STATUS_PATH, HEALTH_INTERVAL_SECS,
)


def _make_gpu(index=0, name='RTX Test', vram_total_mib=6144, vram_free_mib=5939, util_pct=1,
              uuid='GPU-test-uuid'):
    return Gpu(
        index=index, name=name,
        vram_total_mib=vram_total_mib,
        vram_free_mib=vram_free_mib,
        util_pct=util_pct,
        uuid=uuid,
    )


def _make_node(gpus=None, models=None, running=None, name='test',
               base_url='http://localhost:11434', is_local=True):
    return Node(
        name=name,
        base_url=base_url,
        online=True,
        gpus=gpus or [],
        cpu=CpuSlot(cores=8, ram_free_mib=16000),
        models=models or [],
        running=running or {},
        is_local=is_local,
    )


class TestParseLoopNodes(unittest.TestCase):
    """_parse_loop_nodes parsea 'name=url,name=url' en tuplas (name, base_url)."""

    def test_empty_string_returns_empty(self):
        self.assertEqual(_parse_loop_nodes(''), [])

    def test_single_node(self):
        result = _parse_loop_nodes('a=http://x')
        self.assertEqual(result, [('a', 'http://x')])

    def test_two_nodes(self):
        result = _parse_loop_nodes('a=http://x,b=http://y')
        self.assertEqual(result, [('a', 'http://x'), ('b', 'http://y')])

    def test_no_equals_skipped(self):
        self.assertEqual(_parse_loop_nodes('a'), [])

    def test_empty_name_skipped(self):
        self.assertEqual(_parse_loop_nodes('=http://x'), [])

    def test_empty_url_skipped(self):
        self.assertEqual(_parse_loop_nodes('a='), [])

    def test_whitespace_stripped(self):
        result = _parse_loop_nodes('  a  =  http://x  ')
        self.assertEqual(result, [('a', 'http://x')])

    def test_empty_entry_between_commas_skipped(self):
        result = _parse_loop_nodes('a=http://x,,b=http://y')
        self.assertEqual(result, [('a', 'http://x'), ('b', 'http://y')])


class TestSpecInfo(unittest.TestCase):
    """_spec_info convierte un ModelSpec en dict con campos clave."""

    def test_none_returns_none(self):
        self.assertIsNone(_spec_info(None))

    def test_returns_dict_with_expected_fields(self):
        import tiers
        spec = tiers.TIER0_HUNTERS[0]
        info = _spec_info(spec)
        self.assertIsInstance(info, dict)
        for key in ('id', 'family', 'role', 'node', 'base_url', 'gpu_index', 'gpu_uuid', 'num_ctx'):
            self.assertIn(key, info, f"_spec_info debe incluir la clave {key!r}")
        self.assertEqual(info['id'], spec.id)
        self.assertEqual(info['family'], spec.family)
        self.assertEqual(info['role'], spec.role)


class TestConstants(unittest.TestCase):
    """Constantes del módulo fleet."""

    def test_health_interval_secs_is_positive_number(self):
        self.assertIsInstance(HEALTH_INTERVAL_SECS, (int, float))
        self.assertGreater(HEALTH_INTERVAL_SECS, 0)

    def test_fleet_status_path_is_string(self):
        self.assertIsInstance(FLEET_STATUS_PATH, str)
        self.assertTrue(len(FLEET_STATUS_PATH) > 0)


class TestWriteFleetStatus(unittest.TestCase):
    """write_fleet_status escribe JSON atómicamente a disco."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, 'fleet.json')

    def tearDown(self):
        for p in (self.path, self.path + '.tmp'):
            try:
                os.unlink(p)
            except OSError:
                pass
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(self.tmpdir)
        except OSError:
            pass

    def _make_empty_fleet(self):
        return Fleet([])

    def test_writes_valid_json_to_path(self):
        fleet_obj = self._make_empty_fleet()
        write_fleet_status(fleet_obj, path=self.path)
        self.assertTrue(os.path.exists(self.path))
        with open(self.path) as f:
            data = json.load(f)
        self.assertIsInstance(data, dict)

    def test_atomic_write_uses_tmp_then_replace(self):
        """write_fleet_status escribe a <path>.tmp y luego hace os.replace()."""
        fleet_obj = self._make_empty_fleet()
        with patch('fleet.os.replace') as mock_replace:
            write_fleet_status(fleet_obj, path=self.path)
            # Tmp file debe haberse creado
            self.assertTrue(os.path.exists(self.path + '.tmp'),
                            "El .tmp debe existir antes del replace (o haber sido movido)")
            # os.replace debe haberse llamado con (tmp, path)
            mock_replace.assert_called_once()
            args, _ = mock_replace.call_args
            self.assertEqual(args[0], self.path + '.tmp')
            self.assertEqual(args[1], self.path)

    def test_payload_includes_required_keys(self):
        fleet_obj = self._make_empty_fleet()
        write_fleet_status(fleet_obj, path=self.path)
        with open(self.path) as f:
            data = json.load(f)
        for key in ('ts', 'schema_version', 'nodes', 'hunter_specs', 'fixer_spec', 'reasoner_specs'):
            self.assertIn(key, data, f"Payload debe incluir {key!r}")
        self.assertEqual(data['schema_version'], 1)
        self.assertIsInstance(data['nodes'], list)
        self.assertIsInstance(data['hunter_specs'], list)
        self.assertIsInstance(data['reasoner_specs'], list)
        # fixer_spec puede ser None o dict
        self.assertTrue(data['fixer_spec'] is None or isinstance(data['fixer_spec'], dict))

    def test_empty_fleet_produces_empty_nodes(self):
        fleet_obj = self._make_empty_fleet()
        write_fleet_status(fleet_obj, path=self.path)
        with open(self.path) as f:
            data = json.load(f)
        self.assertEqual(data['nodes'], [])
        self.assertEqual(data['hunter_specs'], [])

    def test_oserror_on_tmp_write_does_not_crash(self):
        """Si el write inicial falla, write_fleet_status loggea y NO lanza."""
        fleet_obj = self._make_empty_fleet()
        with patch('fleet.open', side_effect=OSError('disk full')):
            try:
                write_fleet_status(fleet_obj, path=self.path)
            except Exception as e:
                self.fail(f"write_fleet_status no debería lanzar OSError: {e}")
        # El path final no debe existir (escribió al tmp que falló)
        self.assertFalse(os.path.exists(self.path),
                         "El path final no debe existir si el tmp write falló")


if __name__ == '__main__':
    unittest.main()
