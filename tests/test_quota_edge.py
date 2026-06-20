#!/usr/bin/env python3
"""Edge-case tests para quota.py: errores de subprocess, parsing límite, etc."""
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import quota  # noqa: E402
from tiers import ModelSpec  # noqa: E402


class TestRunAiUsageEdge(unittest.TestCase):
    """_run_ai_usage: manejo defensivo de errores del subprocess."""

    def test_subprocess_nonzero_returncode_returns_empty(self):
        """returncode != 0 → {} (no parsea stdout)."""
        fake = MagicMock()
        fake.returncode = 1
        fake.stdout = 'algo que no es json'
        with patch('quota.subprocess.run', return_value=fake):
            self.assertEqual(quota._run_ai_usage(['daily']), {})

    def test_subprocess_timeout_returns_empty(self):
        """subprocess.TimeoutExpired → {} (no propaga la excepción)."""
        with patch('quota.subprocess.run',
                   side_effect=subprocess.TimeoutExpired(cmd='ai-usage', timeout=30)):
            self.assertEqual(quota._run_ai_usage(['daily']), {})

    def test_subprocess_not_found_returns_empty(self):
        """FileNotFoundError (script no existe) → {}."""
        with patch('quota.subprocess.run', side_effect=FileNotFoundError):
            self.assertEqual(quota._run_ai_usage(['daily']), {})

    def test_invalid_json_returns_empty(self):
        """JSONDecodeError en stdout → {}."""
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = 'esto no es json {'
        with patch('quota.subprocess.run', return_value=fake):
            self.assertEqual(quota._run_ai_usage(['daily']), {})

    def test_success_parses_json(self):
        """JSON válido → dict parseado y devuelto."""
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = json.dumps({'daily': [{'modelBreakdowns': []}]})
        with patch('quota.subprocess.run', return_value=fake):
            self.assertEqual(
                quota._run_ai_usage(['daily']),
                {'daily': [{'modelBreakdowns': []}]},
            )


class TestExtractPctEdge(unittest.TestCase):
    """_extract_pct: casos límite del parser."""

    def test_daily_field_absent(self):
        """Sin clave 'daily' → {}."""
        self.assertEqual(quota._extract_pct({'weekly': []}), {})

    def test_today_no_breakdowns_returns_empty(self):
        """El último día sin modelBreakdowns → {} (no rompe)."""
        ai_data = {
            'daily': [
                {'modelBreakdowns': [{'modelName': 'X',
                                      'inputTokens': 100,
                                      'outputTokens': 0,
                                      'cacheCreationTokens': 0}]},
                {},
            ],
        }
        result = quota._extract_pct(ai_data)
        self.assertEqual(result, {})

    def test_multiple_models_today_all_present(self):
        """Múltiples modelos en el último día → todos en el resultado."""
        ai_data = {'daily': [{
            'modelBreakdowns': [
                {'modelName': 'a', 'inputTokens': 100,
                 'outputTokens': 0, 'cacheCreationTokens': 0},
                {'modelName': 'b', 'inputTokens': 200,
                 'outputTokens': 0, 'cacheCreationTokens': 0},
            ],
        }]}
        result = quota._extract_pct(ai_data)
        self.assertIn('a', result)
        self.assertIn('b', result)
        self.assertEqual(result['a']['daily_tokens'], 100)
        self.assertEqual(result['b']['daily_tokens'], 200)

    def test_effective_includes_cache_creation(self):
        """effective_tokens = input + cacheCreation + output (no se pierde cache)."""
        ai_data = {'daily': [{
            'modelBreakdowns': [{
                'modelName': 'x',
                'inputTokens': 100,
                'cacheCreationTokens': 50,
                'outputTokens': 25,
            }],
        }]}
        result = quota._extract_pct(ai_data)
        self.assertEqual(result['x']['daily_tokens'], 175)

    def test_missing_token_fields_default_to_zero(self):
        """Si input/output/cache no están en el JSON, se asumen 0."""
        ai_data = {'daily': [{
            'modelBreakdowns': [{'modelName': 'y'}],
        }]}
        result = quota._extract_pct(ai_data)
        self.assertEqual(result['y']['daily_tokens'], 0)
        self.assertEqual(result['y']['weekly_tokens'], 0)

    def test_weekly_sums_across_multiple_days(self):
        """weekly_tokens acumula todos los días, no solo el último."""
        ai_data = {
            'daily': [
                {'modelBreakdowns': [{'modelName': 'm',
                                      'inputTokens': 100,
                                      'outputTokens': 0,
                                      'cacheCreationTokens': 0}]},
                {'modelBreakdowns': [{'modelName': 'm',
                                      'inputTokens': 200,
                                      'outputTokens': 0,
                                      'cacheCreationTokens': 0}]},
                {'modelBreakdowns': [{'modelName': 'm',
                                      'inputTokens': 400,
                                      'outputTokens': 0,
                                      'cacheCreationTokens': 0}]},
            ],
        }
        result = quota._extract_pct(ai_data)
        self.assertEqual(result['m']['weekly_tokens'], 700)
        self.assertEqual(result['m']['daily_tokens'], 400)


class TestRemainingPctEdge(unittest.TestCase):
    """_remaining_pct: casos especiales (locales, sin tracking)."""

    def setUp(self):
        quota._STATE.by_model_pct.clear()
        quota._STATE.call_log.clear()
        quota._STATE.timestamp = 0.0
        quota._STATE.last_refresh_started = 0.0

    def test_ollama_returns_100(self):
        """Ollama → 100% (no consume quota cloud)."""
        self.assertEqual(quota._remaining_pct('ollama:qwen3-coder:30b'), 100.0)

    def test_opencode_returns_100(self):
        """opencode → 100% (sin tracking)."""
        self.assertEqual(
            quota._remaining_pct('opencode:deepseek-v4-flash-free'),
            100.0,
        )

    @patch('quota._run_ai_usage')
    def test_unknown_model_returns_100_optimistic(self, mock_run):
        """Modelo no en quota data → 100% (deja pasar)."""
        mock_run.return_value = {'daily': []}
        self.assertEqual(quota._remaining_pct('some-unknown-model'), 100.0)

    @patch('quota._run_ai_usage')
    def test_over_limit_returns_zero(self, mock_run):
        """Sobre el límite → 0% (agotado)."""
        mock_run.return_value = {
            'daily': [{
                'modelBreakdowns': [{
                    'modelName': 'claude-sonnet-4-5',
                    'inputTokens': 49_000_000,
                    'outputTokens': 0,
                    'cacheCreationTokens': 0,
                }],
            }],
        }
        # 49M/50M = 98% usado > 80% limit → remaining = 0
        self.assertEqual(quota._remaining_pct('claude-sonnet-4-5'), 0.0)


class TestFilterTierEdge(unittest.TestCase):
    """filter_tier: casos límite de input."""

    def setUp(self):
        quota._STATE.by_model_pct.clear()
        quota._STATE.call_log.clear()
        quota._STATE.timestamp = 0.0
        quota._STATE.last_refresh_started = 0.0

    @patch('quota._run_ai_usage')
    def test_empty_tuple_returns_empty_list(self, mock_run):
        """tuple vacío → []."""
        mock_run.return_value = {'daily': []}
        self.assertEqual(quota.filter_tier((), 'any'), [])


class TestSmartPickEdge(unittest.TestCase):
    """smart_pick: casos límite."""

    def setUp(self):
        quota._STATE.by_model_pct.clear()
        quota._STATE.call_log.clear()
        quota._STATE.timestamp = 0.0
        quota._STATE.last_refresh_started = 0.0

    @patch('quota._run_ai_usage')
    def test_empty_tuple_returns_none(self, mock_run):
        """tuple vacío → None (nada que elegir)."""
        mock_run.return_value = {'daily': []}
        self.assertIsNone(quota.smart_pick((), 'any'))


class TestModelIdToAiUsageId(unittest.TestCase):
    """_model_id_to_ai_usage_id: normalización de prefijos provider:."""

    def test_colon_prefix_stripped(self):
        """'provider:name' → 'name' (la parte tras el ':')."""
        self.assertEqual(
            quota._model_id_to_ai_usage_id('minimax:MiniMax-M3'),
            'MiniMax-M3',
        )

    def test_duplicate_provider_prefix_stripped(self):
        """'claude:claude-sonnet-4-6' → 'claude-sonnet-4-6' (sin duplicar)."""
        self.assertEqual(
            quota._model_id_to_ai_usage_id('claude:claude-sonnet-4-6'),
            'claude-sonnet-4-6',
        )

    def test_no_colon_unchanged(self):
        """Sin ':' → devuelve el id igual."""
        self.assertEqual(
            quota._model_id_to_ai_usage_id('claude-sonnet-4-6'),
            'claude-sonnet-4-6',
        )


class TestRecordEdge(unittest.TestCase):
    """record: casos extremos del log."""

    def setUp(self):
        quota._STATE.by_model_pct.clear()
        quota._STATE.call_log.clear()
        quota._STATE.timestamp = 0.0
        quota._STATE.last_refresh_started = 0.0

    def test_zero_tokens_still_appends(self):
        """Tokens en 0 también se registran (no se filtran)."""
        quota.record('claude:test', 0, 0)
        self.assertEqual(len(quota._STATE.call_log), 1)
        self.assertEqual(quota._STATE.call_log[0]['effective_tokens'], 0)

    def test_record_does_not_touch_quota_state(self):
        """record no consulta cuota ni modifica by_model_pct."""
        before_keys = set(quota._STATE.by_model_pct.keys())
        quota.record('claude:test', 10, 5)
        self.assertEqual(set(quota._STATE.by_model_pct.keys()), before_keys)


if __name__ == '__main__':
    unittest.main(verbosity=2)
