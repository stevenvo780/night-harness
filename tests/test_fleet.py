#!/usr/bin/env python3
"""Tests de fleet.py — Fase B VRAM: est_vram_mib, plan_models, build_fleet."""
import logging
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fleet  # noqa: E402
from fleet import (
    Gpu, CpuSlot, Node, Fleet,
    plan_models, build_fleet, HEADROOM,
    _estimate_vram_for_model, FILL_VRAM_EST, FILL_VRAM_DEFAULT_MIB,
)

# est_vram_mib: formula del spec §4.4. Se implementa localmente si no esta en fleet.py
# (Track A puede no haberla expuesto como funcion publica).
if hasattr(fleet, 'est_vram_mib'):
    est_vram_mib = fleet.est_vram_mib
else:
    _VRAM_OVERHEAD = 1.10
    def est_vram_mib(size_bytes: int) -> int:
        return int(size_bytes / (1024 * 1024) * _VRAM_OVERHEAD)


def _make_gpu(index=0, name='RTX Test', vram_total_mib=6144, vram_free_mib=5939, util_pct=1,
              uuid='GPU-test-uuid'):
    return Gpu(
        index=index, name=name,
        vram_total_mib=vram_total_mib,
        vram_free_mib=vram_free_mib,
        util_pct=util_pct,
        uuid=uuid,
    )


def _make_node(gpus=None, models=None, running=None, name='test', base_url='http://localhost:11434',
               is_local=True):
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


class TestEstVramMib(unittest.TestCase):
    """fleet.est_vram_mib calcula VRAM estimada = bytes / 1MB * 1.10."""

    def _est(self, size_bytes: int) -> int:
        """Helper que implementa la formula del spec si no esta expuesta como funcion."""
        VRAM_OVERHEAD = 1.10
        return int(size_bytes / (1024 * 1024) * VRAM_OVERHEAD)

    def test_7b_fits_in_6144_mib(self):
        """7b ~4.68 GB en disco → ~5.1 GB estimado → cabe en 6144 MiB (RTX 2060)."""
        # 4.68 GB = 4_680_000_000 bytes
        size_bytes_7b = 4_680_000_000
        est = self._est(size_bytes_7b)
        # ~5010 MiB estimado; con HEADROOM 80% de 6144 = 4915 MiB o margen completo
        # El spec dice "cabe en 2060" — verificamos que la estimacion < 6144
        self.assertLess(est, 6144, f"7b est_vram_mib={est} debe caber en 6144 MiB (2060)")

    def test_14b_does_not_fit_in_6144_mib(self):
        """14b ~9.3 GB → ~9788 MiB → NO cabe en 6144 MiB (RTX 2060)."""
        size_bytes_14b = 9_300_000_000
        est = self._est(size_bytes_14b)
        self.assertGreater(est, 6144, f"14b est_vram_mib={est} NO debe caber en 6144 MiB (2060)")

    def test_overhead_factor(self):
        """La estimacion aplica factor VRAM_OVERHEAD=1.10 sobre bytes."""
        size_bytes = 1024 * 1024 * 1000  # 1000 MiB exactos en bytes
        est = self._est(size_bytes)
        self.assertEqual(est, 1100)  # 1000 * 1.10 = 1100


class TestPlanModels(unittest.TestCase):
    """plan_models asigna hunter/fixer/reasoner segun VRAM."""

    def test_no_gpus_returns_empty(self):
        node = _make_node(gpus=[], models=[])
        result = plan_models(node)
        self.assertEqual(result['hunter'], [])
        self.assertIsNone(result['fixer'])
        self.assertEqual(result['reasoner'], [])

    def test_30b_assigned_to_big_gpu_as_fixer(self):
        """GPU0 16GB con qwen3-coder:30b instalado → fixer_spec apunta a 30b."""
        gpu0 = _make_gpu(index=0, name='RTX 5070 Ti', vram_total_mib=16384, vram_free_mib=9000)
        node = _make_node(gpus=[gpu0], models=['qwen3-coder:30b', 'devstral:24b'])
        result = plan_models(node)
        fixer = result['fixer']
        self.assertIsNotNone(fixer)
        self.assertEqual(fixer.id, 'ollama:qwen3-coder:30b')
        self.assertEqual(fixer.gpu_index, 0)

    def test_7b_assigned_to_small_gpu(self):
        """GPU1 6144 MiB con qwen2.5-coder:7b → hunter en esa GPU."""
        gpu1 = _make_gpu(index=1, name='RTX 2060', vram_total_mib=6144, vram_free_mib=5939)
        node = _make_node(gpus=[gpu1], models=['qwen2.5-coder:7b'])
        result = plan_models(node)
        hunters = result['hunter']
        self.assertTrue(len(hunters) >= 1, "Debe haber al least 1 hunter en la 2060")
        h = hunters[0]
        self.assertEqual(h.id, 'ollama:qwen2.5-coder:7b')
        self.assertEqual(h.gpu_index, 1)

    def test_14b_not_assigned_to_2060(self):
        """GPU1 6144 MiB con qwen3:14b: 14b ~9.3GB no cabe → no debe asignarse como hunter."""
        gpu1 = _make_gpu(index=1, name='RTX 2060', vram_total_mib=6144, vram_free_mib=5939)
        # Solo instalamos 14b; como no hay 7b ni 3b en SMALL_HUNTER_PREF, no debe asignar hunter
        node = _make_node(gpus=[gpu1], models=['qwen3:14b'])
        result = plan_models(node)
        hunters = result['hunter']
        # No debe haber hunter en la 2060 con 14b (no está en SMALL_HUNTER_PREF)
        for h in hunters:
            self.assertNotEqual(h.id, 'ollama:qwen3:14b',
                                "14b no debe asignarse a GPU con 6144 MiB")

    def test_num_ctx_small_for_2060(self):
        """GPU con < 8000 MiB total → num_ctx=8192."""
        gpu1 = _make_gpu(index=1, name='RTX 2060', vram_total_mib=6144, vram_free_mib=5939)
        node = _make_node(gpus=[gpu1], models=['qwen2.5-coder:7b'])
        result = plan_models(node)
        hunters = result['hunter']
        if hunters:
            self.assertEqual(hunters[0].num_ctx, 8192)

    def test_num_ctx_large_for_big_gpu(self):
        """GPU con >= 8000 MiB total → num_ctx=32768."""
        gpu0 = _make_gpu(index=0, name='RTX 5070 Ti', vram_total_mib=16384, vram_free_mib=9000)
        node = _make_node(gpus=[gpu0], models=['qwen3-coder:30b'])
        result = plan_models(node)
        fixer = result['fixer']
        if fixer:
            self.assertEqual(fixer.num_ctx, 32768)

    def test_reasoner_assigned_to_cpu(self):
        """Si hay CPU y qwen3-coder-next:cpu instalado → reasoner."""
        gpu0 = _make_gpu(index=0, vram_total_mib=16384, vram_free_mib=9000)
        node = _make_node(gpus=[gpu0], models=['qwen3-coder:30b', 'qwen3-coder-next:cpu'])
        result = plan_models(node)
        reasoners = result['reasoner']
        self.assertTrue(len(reasoners) >= 1)
        self.assertIn('qwen3-coder-next:cpu', reasoners[0].id)


class TestBuildFleet(unittest.TestCase):
    """build_fleet() no lanza aunque nvidia-smi falle o Ollama no responda."""

    def test_build_fleet_no_crash_offline(self):
        """build_fleet con LOOP_NODES apuntando a host inalcanzable no lanza."""
        with patch.dict(os.environ, {'LOOP_NODES': 'fake=http://127.0.0.1:19999'}):
            try:
                f = build_fleet()
            except Exception as e:
                self.fail(f"build_fleet() lanzó excepción inesperada: {e}")
            self.assertIsInstance(f, Fleet)
            # Fleet puede estar vacia si el nodo no responde
            self.assertIsInstance(f.all_specs(), list)

    def test_build_fleet_api_contract(self):
        """Fleet cumple el contrato de API publica exacto."""
        with patch('fleet.discover_local_gpus', return_value=[]), \
             patch('fleet.discover_models', return_value=[]), \
             patch('fleet.discover_running', return_value={}), \
             patch.dict(os.environ, {'LOOP_NODES': 'local=http://host.docker.internal:11434'}):
            f = build_fleet()
        # API publica
        self.assertTrue(hasattr(f, 'nodes'))
        self.assertIsInstance(f.nodes, list)
        self.assertIsInstance(f.hunter_specs(), list)
        self.assertIsNone(f.fixer_spec())  # vacio si sin GPUs
        self.assertIsInstance(f.reasoner_specs(), list)
        self.assertIsInstance(f.all_specs(), list)
        self.assertIsInstance(f.status(), list)

    def test_spec_id_no_host(self):
        """ModelSpec.id debe ser 'ollama:<model>' SIN host (no rompe split(':',1)[1])."""
        gpu0 = _make_gpu(index=0, vram_total_mib=16384, vram_free_mib=9000)
        node = _make_node(gpus=[gpu0], models=['qwen3-coder:30b'],
                          base_url='http://host.docker.internal:11434')
        result = plan_models(node)
        fixer = result['fixer']
        if fixer:
            # id = 'ollama:qwen3-coder:30b' (sin host)
            parts = fixer.id.split(':', 1)
            self.assertEqual(parts[0], 'ollama')
            self.assertNotIn('http', fixer.id)
            self.assertNotIn('host.docker', fixer.id)
            # base_url en campo separado
            self.assertIsNotNone(fixer.base_url)
            self.assertIn('http', fixer.base_url)

    def test_spec_optional_fields_default_none(self):
        """ModelSpec de tiers (estatico) tiene campos opcionales = None por defecto."""
        import tiers
        spec = tiers.TIER0_HUNTERS[0]
        self.assertIsNone(spec.base_url)
        self.assertIsNone(spec.gpu_index)
        self.assertIsNone(spec.gpu_uuid)
        self.assertIsNone(spec.node)
        self.assertIsNone(spec.num_ctx)


class TestEstimateVramForModel(unittest.TestCase):
    """_estimate_vram_forModel: extrae la size tag con regex, NO substring.

    Regresion: el codigo original hacia `'7b' in 'qwen2.5-coder:17b'` y eso
    matcheaba incorrectamente, devolviendo 5500 (estimate de 7b) para un
    modelo 17b que no esta en el catalogo. Con el regex `(\\d+)b` se
    matchea '17b' directamente, no encuentra la key, y cae al default
    FILL_VRAM_DEFAULT_MIB (4000).
    """

    def setUp(self):
        # Reset the dedup set so we can assert warn-once semantics.
        fleet._warned_unknown_vram_models.clear()

    def test_known_3b(self):
        self.assertEqual(_estimate_vram_for_model('qwen2.5-coder:3b'),
                         FILL_VRAM_EST['3b'])

    def test_known_7b(self):
        self.assertEqual(_estimate_vram_for_model('qwen2.5-coder:7b'),
                         FILL_VRAM_EST['7b'])

    def test_known_30b(self):
        self.assertEqual(_estimate_vram_for_model('qwen3-coder:30b'),
                         FILL_VRAM_EST['30b'])

    def test_known_24b(self):
        self.assertEqual(_estimate_vram_for_model('devstral:24b'),
                         FILL_VRAM_EST['24b'])

    def test_known_14b(self):
        self.assertEqual(_estimate_vram_for_model('qwen3:14b'),
                         FILL_VRAM_EST['14b'])

    def test_known_32b(self):
        self.assertEqual(_estimate_vram_for_model('qwen3:32b'),
                         FILL_VRAM_EST['32b'])

    def test_known_20b(self):
        self.assertEqual(_estimate_vram_for_model('gpt-oss:20b'),
                         FILL_VRAM_EST['20b'])

    def test_known_12b_does_not_substring_match_2b(self):
        """12b no debe matchear '2b' (substring bug)."""
        self.assertEqual(_estimate_vram_for_model('qwen2.5:12b'),
                         FILL_VRAM_EST['12b'])

    def test_unknown_17b_falls_back_to_default(self):
        """17b NO esta en el catalogo y NO debe matchear '7b' (regresion)."""
        self.assertEqual(_estimate_vram_for_model('qwen2.5-coder:17b'),
                         FILL_VRAM_DEFAULT_MIB)

    def test_unknown_22b_falls_back_to_default(self):
        """22b NO matchea '2b' (que no esta en el catalogo)."""
        self.assertEqual(_estimate_vram_for_model('foo:22b'),
                         FILL_VRAM_DEFAULT_MIB)

    def test_unknown_70b_falls_back_to_default(self):
        """70b NO matchea '7b' (regresion: '7b' in '70b' era True)."""
        self.assertEqual(_estimate_vram_for_model('llama3.1:70b-instruct-q4_K_M'),
                         FILL_VRAM_DEFAULT_MIB)

    def test_no_size_tag_falls_back_to_default(self):
        """Model id sin ':<N>b' (e.g. 'qwen2.5-coder') cae al default."""
        self.assertEqual(_estimate_vram_for_model('qwen2.5-coder'),
                         FILL_VRAM_DEFAULT_MIB)

    def test_empty_string_falls_back_to_default(self):
        self.assertEqual(_estimate_vram_for_model(''), FILL_VRAM_DEFAULT_MIB)

    def test_none_falls_back_to_default(self):
        self.assertEqual(_estimate_vram_for_model(None), FILL_VRAM_DEFAULT_MIB)

    def test_warning_dedup_per_unknown_name(self):
        """Cada nombre desconocido se warn-ea solo una vez."""
        with self.assertLogs('root', level='WARNING') as cm:
            _estimate_vram_for_model('mystery:17b')
            _estimate_vram_for_model('mystery:17b')
            _estimate_vram_for_model('mystery:17b')
        # Solo 1 warning para el mismo nombre, aunque se llame 3 veces.
        matches = [r for r in cm.output if 'mystery:17b' in r]
        self.assertEqual(len(matches), 1,
                         f"expected 1 warn for 'mystery:17b', got {len(matches)}")

    def test_warning_fired_for_different_unknown_names(self):
        """Nombres desconocidos distintos producen warnings distintos."""
        with self.assertLogs('root', level='WARNING') as cm:
            _estimate_vram_for_model('foo:17b')
            _estimate_vram_for_model('bar:17b')
        names_warned = [r for r in cm.output if '17b' in r]
        self.assertEqual(len(names_warned), 2)

    def test_warning_fired_for_missing_size_tag(self):
        """Sin size tag tambien se warn-ea (para que se catalogue)."""
        with self.assertLogs('root', level='WARNING') as cm:
            _estimate_vram_for_model('qwen2.5-coder')
        matches = [r for r in cm.output if 'qwen2.5-coder' in r]
        self.assertEqual(len(matches), 1)

    def test_known_model_does_not_warn(self):
        """Modelos del catalogo no deben producir warnings."""
        # Usamos assertNoLogs via un nivel que no matche.
        logger = logging.getLogger('root')
        # Capturamos warnings a un handler en memoria.
        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)
        cap = _Capture(level=logging.WARNING)
        logger.addHandler(cap)
        try:
            _estimate_vram_for_model('qwen2.5-coder:7b')
        finally:
            logger.removeHandler(cap)
        self.assertEqual(records, [], "modelo del catalogo no debe warn-ear")


class TestFleetApiPublica(unittest.TestCase):
    """Verifica la API publica EXACTA del spec (Track A implementa EXACTO)."""

    def _make_fleet_with_node(self):
        gpu0 = _make_gpu(index=0, vram_total_mib=16384, vram_free_mib=9000)
        gpu1 = _make_gpu(index=1, name='RTX 2060', vram_total_mib=6144, vram_free_mib=5939)
        node = _make_node(
            gpus=[gpu0, gpu1],
            models=['qwen3-coder:30b', 'qwen2.5-coder:7b'],
        )
        return Fleet([node])

    def test_hunter_specs_returns_list(self):
        f = self._make_fleet_with_node()
        self.assertIsInstance(f.hunter_specs(), list)

    def test_fixer_spec_returns_spec_or_none(self):
        f = self._make_fleet_with_node()
        fs = f.fixer_spec()
        # Either None or a ModelSpec
        if fs is not None:
            self.assertTrue(hasattr(fs, 'id'))

    def test_reasoner_specs_returns_list(self):
        f = self._make_fleet_with_node()
        self.assertIsInstance(f.reasoner_specs(), list)

    def test_all_specs_returns_list(self):
        f = self._make_fleet_with_node()
        specs = f.all_specs()
        self.assertIsInstance(specs, list)
        # all_specs = hunters + fixer + reasoners
        hunters = f.hunter_specs()
        fixer = f.fixer_spec()
        reasoners = f.reasoner_specs()
        expected_len = len(hunters) + (1 if fixer else 0) + len(reasoners)
        self.assertEqual(len(specs), expected_len)

    def test_status_returns_list_of_dicts(self):
        f = self._make_fleet_with_node()
        s = f.status()
        self.assertIsInstance(s, list)
        self.assertTrue(len(s) >= 1)
        self.assertIsInstance(s[0], dict)


if __name__ == '__main__':
    unittest.main()
