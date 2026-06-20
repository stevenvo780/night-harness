#!/usr/bin/env python3
"""Tests de regresión para los bugs confirmados en la revisión multi-modelo (jun-2026).

Cada clase reproduce un bug concreto encontrado por la revisión (minimax /
opencode-deepseek / codex) y verificado leyendo el código. Se escriben PRIMERO
(fallan en RED) y luego se aplica el fix mínimo.
"""
import dataclasses
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet  # noqa: E402
import memory  # noqa: E402
import orchestrator  # noqa: E402
import quota  # noqa: E402
from loop_harness import domain, tui_core  # noqa: E402


def _reset_quota_state():
    quota._STATE.by_model_pct.clear()
    quota._STATE.call_log.clear()
    quota._STATE.timestamp = 0.0
    quota._STATE.last_refresh_started = 0.0


# ---------------------------------------------------------------------------
# quota.py — bypass de cuota para modelos cloud con prefijo `provider:`
# ---------------------------------------------------------------------------
class TestQuotaPrefixedCloud(unittest.TestCase):
    """is_model_ok / _remaining_pct deben pelar el prefijo `provider:` para
    encontrar la entrada en by_model_pct (que va SIN prefijo)."""

    def setUp(self):
        _reset_quota_state()
        # Estado fresco para que refresh() devuelva la cache sin subprocess.
        now = time.time()
        quota._STATE.timestamp = now
        quota._STATE.last_refresh_started = now
        quota._STATE.by_model_pct = {'claude-sonnet-4-5': {'pct_used': 99.0}}

    def tearDown(self):
        _reset_quota_state()

    def test_is_model_ok_blocks_prefixed_cloud_over_limit(self):
        # claude-sonnet-4-5 weekly_pct_max = 80; pct_used 99 → NO ok.
        self.assertFalse(quota.is_model_ok('claude:claude-sonnet-4-5'))

    def test_is_model_ok_bare_name_still_blocks(self):
        # Control: el nombre sin prefijo ya funcionaba.
        self.assertFalse(quota.is_model_ok('claude-sonnet-4-5'))

    def test_remaining_pct_prefixed_cloud_reflects_usage(self):
        # 99% usado sobre límite 80 → 0% restante.
        self.assertEqual(quota._remaining_pct('claude:claude-sonnet-4-5'), 0.0)


class TestQuotaRefreshPreservesData(unittest.TestCase):
    """refresh() no debe pisar by_model_pct con {} si el script ai-usage falla."""

    def setUp(self):
        _reset_quota_state()

    def tearDown(self):
        _reset_quota_state()

    def test_refresh_keeps_old_data_when_script_returns_empty(self):
        quota._STATE.by_model_pct = {'claude-sonnet-4-5': {'pct_used': 50.0}}
        with patch('quota._run_ai_usage', return_value={}):
            quota.refresh(force=True)
        self.assertIn('claude-sonnet-4-5', quota._STATE.by_model_pct)
        self.assertEqual(quota._STATE.by_model_pct['claude-sonnet-4-5']['pct_used'], 50.0)


# ---------------------------------------------------------------------------
# memory.py — FalsePositive.last_seen fantasma + load robusto
# ---------------------------------------------------------------------------
class TestFalsePositiveLastSeen(unittest.TestCase):
    """record_false_positive asigna rec.last_seen; debe ser un campo real que persiste."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memory-fp-')
        self.path = Path(self.tmpdir) / 'memory.json'

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_false_positive_dataclass_declares_last_seen(self):
        names = {f.name for f in dataclasses.fields(memory.FalsePositive)}
        self.assertIn('last_seen', names)

    def test_last_seen_persists_through_save_load(self):
        mem = memory.Memory()
        mem.record_false_positive('b.py', 5, 'noise pattern', reason='flaky')
        mem.save(path=self.path)
        loaded = memory.Memory.load(path=self.path)
        fp = next(iter(loaded.false_positives.values()))
        self.assertGreaterEqual(fp.last_seen, fp.first_seen)


class TestMemoryLoadRobust(unittest.TestCase):
    """load() debe tolerar raíz no-dict y poner en cuarentena el archivo corrupto."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memory-load-')
        self.path = Path(self.tmpdir) / 'memory.json'

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_non_dict_root_returns_empty(self):
        self.path.write_text('[1, 2, 3]')
        mem = memory.Memory.load(path=self.path)  # no debe lanzar
        self.assertEqual(mem.bugs, {})
        self.assertEqual(mem.false_positives, {})

    def test_load_corrupt_quarantines_file(self):
        self.path.write_text('{ not valid json')
        memory.Memory.load(path=self.path)
        corrupt = self.path.with_name(self.path.name + '.corrupt')
        self.assertTrue(corrupt.exists(), 'el archivo corrupto no fue archivado')


# ---------------------------------------------------------------------------
# orchestrator.py — _scan_schema con candidatos idénticos (sin fallback real)
# ---------------------------------------------------------------------------
class TestScanSchemaAltPaths(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='orch-schema-')
        self.root = Path(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_scan_schema_finds_alternative_db_path(self):
        # Schema NO en lib/db, sino en db/ → debe encontrarlo por fallback.
        target = self.root / 'db' / 'schema.ts'
        target.parent.mkdir(parents=True)
        target.write_text('export const users = {};\n')
        self.assertNotEqual(orchestrator._scan_schema(str(self.root)), '')


# ---------------------------------------------------------------------------
# fleet.py — estimación VRAM de 30B igual a 20B (riesgo OOM en budget-fill)
# ---------------------------------------------------------------------------
class TestFillVram30b(unittest.TestCase):
    def test_30b_estimate_above_20b(self):
        self.assertGreater(fleet.FILL_VRAM_EST['30b'], fleet.FILL_VRAM_EST['20b'])

    def test_vram_estimates_monotonic_by_size(self):
        order = ['12b', '14b', '20b', '24b', '30b', '32b']
        vals = [fleet.FILL_VRAM_EST[k] for k in order]
        self.assertEqual(vals, sorted(vals), f'no monotónico: {dict(zip(order, vals))}')


# ---------------------------------------------------------------------------
# tui_core.py — host null en nodos tumba el draw loop
# ---------------------------------------------------------------------------
class TestNodeHostLabel(unittest.TestCase):
    def test_null_host_falls_back_to_id(self):
        self.assertEqual(tui_core.node_host_label({'host': None, 'id': 'n1'}), 'n1')

    def test_missing_host_falls_back_to_id(self):
        self.assertEqual(tui_core.node_host_label({'id': 'n2'}), 'n2')

    def test_present_host_used(self):
        self.assertEqual(tui_core.node_host_label({'host': 'h.local', 'id': 'n'}), 'h.local')

    def test_no_host_no_id_returns_placeholder(self):
        self.assertEqual(tui_core.node_host_label({}), '?')


# ---------------------------------------------------------------------------
# domain.py — contención de path (la salida del modelo no debe escapar el worktree)
# ---------------------------------------------------------------------------
class TestSafeSubpath(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='safe-sub-')
        self.base = Path(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_normal_subpath_allowed(self):
        got = domain.safe_subpath(self.base, 'app/page.tsx')
        self.assertIsNotNone(got)
        self.assertEqual(got, (self.base / 'app/page.tsx').resolve())

    def test_traversal_rejected(self):
        self.assertIsNone(domain.safe_subpath(self.base, 'src/../../../etc/passwd'))

    def test_absolute_path_rejected(self):
        self.assertIsNone(domain.safe_subpath(self.base, '/etc/passwd'))

    def test_empty_rejected(self):
        self.assertIsNone(domain.safe_subpath(self.base, ''))


if __name__ == '__main__':
    unittest.main()
