#!/usr/bin/env python3
"""Tests de edge cases de los wrappers CLI/HTTP + call_model dispatcher — A-PR5.

Cubre:
  · _call_minimax_http: comportamiento con/sin API key
  · _call_minimax: CLI success, fallback a HTTP en FileNotFoundError
  · _call_opencode: filtrado de líneas [0m y "build ·"
  · _call_claude_cli / _call_gemini_cli / _call_codex_cli: forma del comando
  · call_model: gating por quota + health, dispatch por prefix, log de
    "modelo desconocido", registro de quota, marcado de salud
"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402
import tiers  # noqa: E402


def _spec(model_id):
    """Helper: ModelSpec inmutable para tests."""
    return tiers.ModelSpec(id=model_id, family='qwen', role='local', relative_cost=2)


class TestCallMinimaxHTTP(unittest.TestCase):
    def test_returns_empty_when_no_api_key(self):
        # Forzamos ausencia de MINIMAX_API_KEY. patch.dict con clear=False
        # preserva el resto del entorno.
        env = {k: v for k, v in os.environ.items() if k != 'MINIMAX_API_KEY'}
        with patch.dict(os.environ, env, clear=True):
            # Re-inyectamos un mínimo para que el módulo siga importable
            # (PATH al menos).
            os.environ['PATH'] = env.get('PATH', '/usr/bin')
            result = harness._call_minimax_http(
                model='MiniMax-M3', prompt='p', max_tokens=100, timeout=30,
            )
        self.assertEqual(result, '')

    def test_parses_choices_content(self):
        resp_body = {
            'choices': [
                {'message': {'content': '  parsed reply  \n'}}
            ]
        }
        mock = MagicMock()
        mock.return_value.__enter__.return_value.read.return_value = (
            json.dumps(resp_body).encode()
        )
        captured = []
        with patch.dict(os.environ, {'MINIMAX_API_KEY': 'test-key'}):
            with patch.object(harness.urllib.request, 'urlopen', mock) as urlopen_mock:
                def _side_effect(req, timeout=None):
                    captured.append({'url': req.full_url, 'auth': req.headers.get('Authorization')})
                    return mock.return_value
                urlopen_mock.side_effect = _side_effect
                result = harness._call_minimax_http(
                    model='MiniMax-M3', prompt='p', max_tokens=512, timeout=20,
                )
        self.assertEqual(result, 'parsed reply')
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0]['url'].endswith('/chat/completions'))
        self.assertEqual(captured[0]['auth'], 'Bearer test-key')


class TestCallMinimax(unittest.TestCase):
    def test_subprocess_success_returns_stripped_stdout(self):
        spec = _spec('minimax/MiniMax-M3')
        proc = MagicMock(stdout='  response text  \n', stderr='', returncode=0)
        with patch('subprocess.run', return_value=proc) as mock_run:
            result = harness._call_minimax(spec, 'p', 's', 100, 30)
        self.assertEqual(result, 'response text')
        # Verifica que el comando se construyó con el spec.id (formato opencode).
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], 'opencode')
        self.assertIn('--model', cmd)
        self.assertIn('minimax/MiniMax-M3', cmd)

    def test_stderr_logged_when_stdout_empty(self):
        spec = _spec('minimax/MiniMax-M3')
        proc = MagicMock(stdout='  \n', stderr='opencode: model not found',
                         returncode=1)
        with patch('subprocess.run', return_value=proc):
            with patch.object(harness, 'log') as mock_log:
                result = harness._call_minimax(spec, 'p', 's', 100, 30)
        self.assertEqual(result, '')
        # log debe haber sido invocado con algo que contenga 'stderr'.
        log_args = [str(c) for c in mock_log.call_args_list]
        self.assertTrue(any('stderr' in s for s in log_args),
                        f'log no registró stderr; calls={log_args}')

    def test_file_not_found_falls_back_to_http(self):
        spec = _spec('minimax/MiniMax-M3')
        resp = MagicMock()
        resp.read.return_value = json.dumps({
            'choices': [{'message': {'content': 'http fallback result'}}]
        }).encode()
        with patch('subprocess.run', side_effect=FileNotFoundError('no opencode')):
            with patch.dict(os.environ, {'MINIMAX_API_KEY': 'k'}):
                with patch.object(harness.urllib.request, 'urlopen') as urlopen_mock:
                    urlopen_mock.return_value.__enter__.return_value = resp
                    result = harness._call_minimax(spec, 'p', 's', 100, 30)
        self.assertEqual(result, 'http fallback result')


class TestCallOpencode(unittest.TestCase):
    def test_filters_ansi_and_build_lines(self):
        spec = _spec('opencode:glm-5.2')
        proc = MagicMock(
            stdout='[0m\nbuild · 1.2.3\nactual response\n[0m',
            stderr='', returncode=0,
        )
        with patch('subprocess.run', return_value=proc):
            result = harness._call_opencode(spec, 'p', 's', 100, 30)
        self.assertIn('actual response', result)
        self.assertNotIn('[0m', result)
        self.assertNotIn('build ·', result)

    def test_file_not_found_returns_empty(self):
        spec = _spec('opencode:glm-5.2')
        with patch('subprocess.run', side_effect=FileNotFoundError):
            with patch.object(harness, 'log'):
                result = harness._call_opencode(spec, 'p', 's', 100, 30)
        self.assertEqual(result, '')


class TestCallClaudeCli(unittest.TestCase):
    def test_passes_model_flag_and_prompt(self):
        spec = _spec('claude:claude-haiku-4-5')
        proc = MagicMock(stdout='reply', stderr='', returncode=0)
        with patch('subprocess.run', return_value=proc) as mock_run:
            result = harness._call_claude_cli(spec, 'myprompt', '', 100, 30)
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], 'claude')
        self.assertIn('-p', cmd)
        self.assertIn('myprompt', cmd)
        self.assertIn('--model', cmd)
        self.assertIn('claude-haiku-4-5', cmd)
        self.assertIn('--output-format', cmd)
        self.assertIn('text', cmd)
        self.assertEqual(result, 'reply')


class TestCallGeminiCli(unittest.TestCase):
    def test_passes_yolo_and_output_format(self):
        spec = _spec('gemini:gemini-2.5-flash')
        proc = MagicMock(stdout='reply', stderr='', returncode=0)
        with patch('subprocess.run', return_value=proc) as mock_run:
            result = harness._call_gemini_cli(spec, 'myprompt', '', 100, 30)
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], 'gemini')
        self.assertIn('-m', cmd)
        self.assertIn('gemini-2.5-flash', cmd)
        self.assertIn('-p', cmd)
        self.assertIn('myprompt', cmd)
        self.assertIn('--yolo', cmd)
        self.assertIn('--output-format', cmd)
        self.assertIn('text', cmd)
        self.assertEqual(result, 'reply')

    def test_429_in_stderr_is_logged(self):
        spec = _spec('gemini:gemini-2.5-flash')
        proc = MagicMock(stdout='', stderr='Error 429 rate limited',
                         returncode=1)
        with patch('subprocess.run', return_value=proc):
            with patch.object(harness, 'log') as mock_log:
                harness._call_gemini_cli(spec, 'p', '', 100, 30)
        log_args = [str(c) for c in mock_log.call_args_list]
        self.assertTrue(any('429' in s for s in log_args),
                        f'no se logueó 429; calls={log_args}')


class TestCallCodexCli(unittest.TestCase):
    def test_uses_exec_and_skip_git_check(self):
        spec = _spec('codex:gpt-5.5')
        proc = MagicMock(stdout='done', stderr='', returncode=0)
        with patch('subprocess.run', return_value=proc) as mock_run:
            result = harness._call_codex_cli(spec, 'myprompt', '', 100, 30)
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], 'codex')
        self.assertIn('exec', cmd)
        self.assertIn('--skip-git-repo-check', cmd)
        self.assertIn('--model', cmd)
        self.assertIn('gpt-5.5', cmd)
        self.assertIn('myprompt', cmd)
        self.assertEqual(result, 'done')


class TestCallModelDispatcher(unittest.TestCase):
    def setUp(self):
        # Estado limpio de salud para los IDs de test.
        self._ids_to_clear = set()
        # Reset semaphore cache (afecta a _call_ollama, pero aquí mockeamos
        # la función entera, así que solo evitamos contaminación entre tests).
        with harness._OLLAMA_SEM_GUARD:
            self._sem_snapshot = dict(harness._OLLAMA_SEMAPHORES)
            harness._OLLAMA_SEMAPHORES.clear()

    def tearDown(self):
        with harness._HEALTH_LOCK:
            for mid in self._ids_to_clear:
                harness._HEALTH.pop(mid, None)
        with harness._OLLAMA_SEM_GUARD:
            harness._OLLAMA_SEMAPHORES.clear()
            harness._OLLAMA_SEMAPHORES.update(self._sem_snapshot)

    def test_blocked_by_quota_returns_none(self):
        spec = _spec('ollama:qwen3-coder:30b')
        with patch.object(harness.quota, 'is_model_ok', return_value=False):
            with patch.object(harness, '_call_ollama') as mock_call:
                result = harness.call_model(spec, 's', 'p')
        self.assertIsNone(result)
        mock_call.assert_not_called()

    def test_blocked_by_health_returns_none(self):
        spec = _spec('ollama:qwen3-coder:30b')
        with patch.object(harness.quota, 'is_model_ok', return_value=True):
            with patch.object(harness, 'is_healthy', return_value=False):
                with patch.object(harness, '_call_ollama') as mock_call:
                    result = harness.call_model(spec, 's', 'p')
        self.assertIsNone(result)
        mock_call.assert_not_called()

    def test_unknown_spec_logs_and_returns_none(self):
        spec_id = 'foo:bar-unknown'
        self._ids_to_clear.add(spec_id)
        spec = _spec(spec_id)
        with patch.object(harness.quota, 'is_model_ok', return_value=True):
            with patch.object(harness, 'is_healthy', return_value=True):
                with patch.object(harness, 'log') as mock_log:
                    result = harness.call_model(spec, 's', 'p')
        self.assertIsNone(result)
        log_args = [str(c) for c in mock_log.call_args_list]
        self.assertTrue(
            any('desconocido' in s for s in log_args),
            f'no se logueó "modelo desconocido"; calls={log_args}',
        )

    def test_records_call_via_quota(self):
        spec = _spec('ollama:qwen3-coder:30b')
        with patch.object(harness.quota, 'is_model_ok', return_value=True):
            with patch.object(harness, 'is_healthy', return_value=True):
                with patch.object(harness, '_call_ollama', return_value='hello output'):
                    with patch.object(harness.quota, 'record') as mock_record:
                        harness.call_model(spec, 'sys', 'prompt',
                                           max_tokens=100, timeout=30)
        # system='sys' (3) + prompt='prompt' (6) = 9 // 4 = 2 → in_tok=2.
        # text='hello output' (12 chars) // 4 = 3 → out_tok=3.
        mock_record.assert_called_once_with('ollama:qwen3-coder:30b', 2, 3)

    def test_marks_health_ok_on_nonempty_response(self):
        spec_id = 'ollama:test-health-ok-edge'
        self._ids_to_clear.add(spec_id)
        spec = _spec(spec_id)
        with patch.object(harness.quota, 'is_model_ok', return_value=True):
            with patch.object(harness, 'is_healthy', return_value=True):
                with patch.object(harness, '_call_ollama', return_value='OK'):
                    harness.call_model(spec, 's', 'p')
        with harness._HEALTH_LOCK:
            h = harness._HEALTH[spec_id]
        self.assertEqual(h['calls'], 1)
        self.assertEqual(h['oks'], 1)
        self.assertEqual(h['fails'], 0)

    def test_marks_health_fail_on_empty_response(self):
        spec_id = 'ollama:test-health-fail-edge'
        self._ids_to_clear.add(spec_id)
        spec = _spec(spec_id)
        with patch.object(harness.quota, 'is_model_ok', return_value=True):
            with patch.object(harness, 'is_healthy', return_value=True):
                with patch.object(harness, '_call_ollama', return_value=''):
                    harness.call_model(spec, 's', 'p')
        with harness._HEALTH_LOCK:
            h = harness._HEALTH[spec_id]
        self.assertEqual(h['calls'], 1)
        self.assertEqual(h['fails'], 1)
        self.assertEqual(h['oks'], 0)

    def test_marks_health_fail_on_whitespace_only(self):
        spec_id = 'ollama:test-health-ws-edge'
        self._ids_to_clear.add(spec_id)
        spec = _spec(spec_id)
        with patch.object(harness.quota, 'is_model_ok', return_value=True):
            with patch.object(harness, 'is_healthy', return_value=True):
                with patch.object(harness, '_call_ollama', return_value='   \n\t'):
                    harness.call_model(spec, 's', 'p')
        with harness._HEALTH_LOCK:
            h = harness._HEALTH[spec_id]
        self.assertEqual(h['fails'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
