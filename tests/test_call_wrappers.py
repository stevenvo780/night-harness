#!/usr/bin/env python3
"""Tests de los wrappers de invocación de modelos — A-PR5 (happy paths + payload).

Cubre:
  · _cli_cwd(): resolución de cwd desde WT_SUBDIR
  · _is_cpu_model(): heurística de modelos CPU (cpu/80b/next, case-insensitive)
  · _ollama_sem(): cache módulo-level + respeto de LOOP_OLLAMA_GPU_CONC
  · _call_ollama(): payload HTTP (num_ctx, keep_alive, main_gpu hint, timeout floor)
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402
import tiers  # noqa: E402


def _spec(model_id='ollama:qwen3-coder:30b', gpu_index=None, num_ctx=None,
          base_url=None):
    """Helper: construye un ModelSpec inmutable para los tests."""
    return tiers.ModelSpec(
        id=model_id, family='qwen', role='local', relative_cost=2,
        gpu_index=gpu_index, num_ctx=num_ctx, base_url=base_url,
    )


def _mock_urlopen(json_body, capture_payload=None):
    """Mockea `urllib.request.urlopen` con respuesta JSON controlable.

    Si `capture_payload` es una lista mutable, se le appende la Request
    recibida para que el test inspeccione el payload enviado.
    """
    mock = MagicMock()
    mock.return_value.__enter__.return_value.read.return_value = (
        json.dumps(json_body).encode()
    )
    if capture_payload is not None:
        def _side_effect(req, timeout=None):
            capture_payload.append({'req': req, 'timeout': timeout})
            return mock.return_value
        mock.side_effect = _side_effect
    return mock


class TestCliCwd(unittest.TestCase):
    def setUp(self):
        self._orig = harness.WT_SUBDIR

    def tearDown(self):
        harness.WT_SUBDIR = self._orig

    def test_returns_none_when_subdir_missing(self):
        # Forzamos un path que NO existe en disco.
        harness.WT_SUBDIR = self._orig.parent / '__definitely_not_a_real_dir_42__'
        self.assertFalse(harness.WT_SUBDIR.is_dir())
        self.assertIsNone(harness._cli_cwd())

    def test_returns_str_when_subdir_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness.WT_SUBDIR = self._orig.__class__(tmp)
            self.assertTrue(harness.WT_SUBDIR.is_dir())
            result = harness._cli_cwd()
            self.assertEqual(result, str(harness.WT_SUBDIR))
            self.assertIsInstance(result, str)


class TestIsCpuModel(unittest.TestCase):
    def test_cpu_keyword(self):
        self.assertTrue(harness._is_cpu_model('qwen3-coder-next:cpu'))

    def test_80b_keyword(self):
        self.assertTrue(harness._is_cpu_model('qwen3-coder-next:80b'))

    def test_normal_30b_is_not_cpu(self):
        self.assertFalse(harness._is_cpu_model('qwen3-coder:30b'))

    def test_next_is_case_insensitive(self):
        self.assertTrue(harness._is_cpu_model('something-Next'))

    def test_unrelated_model_is_false(self):
        self.assertFalse(harness._is_cpu_model('llama3.1:8b'))


class TestOllamaSem(unittest.TestCase):
    def setUp(self):
        with harness._OLLAMA_SEM_GUARD:
            harness._OLLAMA_SEMAPHORES.clear()

    def tearDown(self):
        with harness._OLLAMA_SEM_GUARD:
            harness._OLLAMA_SEMAPHORES.clear()

    def test_returns_semaphore_instance(self):
        sem = harness._ollama_sem('qwen3-coder:30b')
        self.assertIsInstance(sem, threading.Semaphore)

    def test_cpu_model_has_limit_1(self):
        sem = harness._ollama_sem('qwen3-coder-next:cpu')
        # Acquire debe poder tomar UNA vez; la segunda falla sin timeout.
        self.assertTrue(sem.acquire(blocking=False))
        # Re-balanceamos para no contaminar otros tests.
        sem.release()

    def test_gpu_conc_env_var(self):
        # LOOP_OLLAMA_GPU_CONC=3 → el semáforo GPU debe aceptar 3 acquisitions.
        with patch.dict(os.environ, {'LOOP_OLLAMA_GPU_CONC': '3'}):
            sem = harness._ollama_sem('qwen3-coder:30b')
            self.assertTrue(sem.acquire(blocking=False))
            self.assertTrue(sem.acquire(blocking=False))
            self.assertTrue(sem.acquire(blocking=False))
            # 4ª falla: confirma que el límite es 3.
            self.assertFalse(sem.acquire(blocking=False))
            sem.release()
            sem.release()
            sem.release()

    def test_cache_returns_same_instance(self):
        a = harness._ollama_sem('qwen3-coder:30b')
        b = harness._ollama_sem('qwen3-coder:30b')
        self.assertIs(a, b)


class TestCallOllama(unittest.TestCase):
    def setUp(self):
        with harness._OLLAMA_SEM_GUARD:
            harness._OLLAMA_SEMAPHORES.clear()

    def tearDown(self):
        with harness._OLLAMA_SEM_GUARD:
            harness._OLLAMA_SEMAPHORES.clear()

    def test_builds_correct_payload(self):
        captured = []
        mock = _mock_urlopen({'response': 'hi'}, capture_payload=captured)
        with patch.object(harness.urllib.request, 'urlopen', mock):
            harness._call_ollama(
                _spec('ollama:qwen3-coder:30b'),
                prompt='P', system='S', max_tokens=512, timeout=60,
            )
        self.assertEqual(len(captured), 1)
        req = captured[0]['req']
        self.assertTrue(req.full_url.endswith('/api/generate'))
        self.assertEqual(req.headers['Content-type'], 'application/json')
        body = json.loads(req.data.decode())
        self.assertEqual(body['model'], 'qwen3-coder:30b')
        self.assertEqual(body['prompt'], 'P')
        self.assertEqual(body['system'], 'S')
        self.assertEqual(body['stream'], False)
        self.assertEqual(body['keep_alive'], '10m')
        self.assertEqual(body['options']['num_predict'], 512)
        self.assertEqual(body['options']['num_ctx'], 8192)
        self.assertEqual(body['options']['temperature'], 0.2)

    def test_response_is_stripped(self):
        mock = _mock_urlopen({'response': '  hello world  \n'})
        with patch.object(harness.urllib.request, 'urlopen', mock):
            result = harness._call_ollama(
                _spec(), 'p', 's', 100, 30,
            )
        self.assertEqual(result, 'hello world')

    def test_no_main_gpu_when_index_is_none(self):
        captured = []
        mock = _mock_urlopen({'response': 'ok'}, capture_payload=captured)
        with patch.object(harness.urllib.request, 'urlopen', mock):
            harness._call_ollama(_spec(), 'p', 's', 100, 30)
        body = json.loads(captured[0]['req'].data.decode())
        self.assertNotIn('main_gpu', body['options'])

    def test_main_gpu_set_when_index_provided(self):
        captured = []
        mock = _mock_urlopen({'response': 'ok'}, capture_payload=captured)
        with patch.object(harness.urllib.request, 'urlopen', mock):
            harness._call_ollama(_spec(gpu_index=1), 'p', 's', 100, 30)
        body = json.loads(captured[0]['req'].data.decode())
        self.assertEqual(body['options']['main_gpu'], 1)

    def test_cpu_model_floors_timeout_to_300(self):
        captured = []
        mock = _mock_urlopen({'response': 'ok'}, capture_payload=captured)
        # CPU model + timeout=60 → urlopen debe recibir timeout=300.
        with patch.object(harness.urllib.request, 'urlopen', mock):
            harness._call_ollama(
                _spec('ollama:qwen3-coder-next:cpu'),
                'p', 's', 100, 60,
            )
        self.assertEqual(captured[0]['timeout'], 300)

    def test_gpu_model_uses_provided_timeout(self):
        captured = []
        mock = _mock_urlopen({'response': 'ok'}, capture_payload=captured)
        with patch.object(harness.urllib.request, 'urlopen', mock):
            harness._call_ollama(_spec(), 'p', 's', 100, 45)
        self.assertEqual(captured[0]['timeout'], 45)


if __name__ == '__main__':
    unittest.main(verbosity=2)
