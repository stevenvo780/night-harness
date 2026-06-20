#!/usr/bin/env python3
"""Tests del módulo quota.py — control de costos del Harness v6."""
import concurrent.futures
import contextlib
import io
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import quota  # noqa: E402
from tiers import ModelSpec  # noqa: E402


class TestQuotaConstants(unittest.TestCase):
    """DEFAULT_LIMITS y get_limit — config y lookup."""

    def setUp(self):
        quota._STATE.by_model_pct.clear()
        quota._STATE.call_log.clear()
        quota._STATE.timestamp = 0.0
        quota._STATE.last_refresh_started = 0.0

    def test_default_limits_well_formed(self):
        """Toda entrada en DEFAULT_LIMITS tiene números positivos, nunca None."""
        for model, cfg in quota.DEFAULT_LIMITS.items():
            self.assertIsInstance(cfg, dict, f'{model} cfg no es dict')
            self.assertGreater(len(cfg), 0, f'{model} cfg vacío')
            for period_key, value in cfg.items():
                self.assertIsNotNone(value, f'{model}/{period_key} es None')
                self.assertIsInstance(value, (int, float),
                                      f'{model}/{period_key} no es número')
                self.assertGreater(value, 0, f'{model}/{period_key} no positivo')
                self.assertLessEqual(value, 100, f'{model}/{period_key} > 100')

    def test_get_limit_sonnet_weekly(self):
        """claude-sonnet-4-5 tiene weekly_pct_max = 80."""
        self.assertEqual(quota.get_limit('claude-sonnet-4-5', 'weekly'), 80.0)

    def test_get_limit_opus_weekly(self):
        """claude-opus-4-6 tiene weekly_pct_max = 95."""
        self.assertEqual(quota.get_limit('claude-opus-4-6', 'weekly'), 95.0)

    def test_get_limit_codex_daily(self):
        """codex-mini tiene daily_pct_max = 85."""
        self.assertEqual(quota.get_limit('codex-mini', 'daily'), 85.0)

    def test_get_limit_unknown_model_returns_none(self):
        """Modelo desconocido → None."""
        self.assertIsNone(quota.get_limit('unknown-model', 'daily'))

    def test_get_limit_invalid_period_returns_none(self):
        """Periodo inválido (ej. 'monthly') → None incluso para modelo conocido."""
        self.assertIsNone(quota.get_limit('claude-sonnet-4-5', 'monthly'))
        self.assertIsNone(quota.get_limit('claude-sonnet-4-5', ''))

    def test_get_limit_unconfigured_period_returns_none(self):
        """Periodo no configurado para el modelo → None (sonnet-4-5 sin daily)."""
        self.assertIsNone(quota.get_limit('claude-sonnet-4-5', 'daily'))


class TestModelOk(unittest.TestCase):
    """is_model_ok: gating por cuota + casos especiales (locales, opencode)."""

    def setUp(self):
        quota._STATE.by_model_pct.clear()
        quota._STATE.call_log.clear()
        quota._STATE.timestamp = 0.0
        quota._STATE.last_refresh_started = 0.0

    def test_local_ollama_always_ok(self):
        """Modelos ollama:* son SIEMPRE OK (locales, sin quota)."""
        self.assertTrue(quota.is_model_ok('ollama:qwen3-coder:30b'))
        self.assertTrue(quota.is_model_ok('ollama:qwen3-coder-next:cpu'))

    def test_opencode_always_ok(self):
        """Modelos opencode:* son SIEMPRE OK (key del usuario, sin tracking)."""
        self.assertTrue(quota.is_model_ok('opencode:deepseek-v4-flash-free'))

    def test_opencode_go_always_ok(self):
        """Modelos opencode-go:* son SIEMPRE OK."""
        self.assertTrue(quota.is_model_ok('opencode-go:kimi-k2.7-code'))

    @patch('quota._run_ai_usage')
    def test_under_limit_returns_true(self, mock_run):
        """Modelo con tokens por debajo del límite → True."""
        mock_run.return_value = {
            'daily': [{
                'modelBreakdowns': [{
                    'modelName': 'claude-sonnet-4-5',
                    'inputTokens': 10_000_000,
                    'cacheCreationTokens': 0,
                    'outputTokens': 5_000_000,
                }],
            }],
        }
        # 15M tokens / 50M proxy_limit = 30% usado < 80% weekly limit
        self.assertTrue(quota.is_model_ok('claude-sonnet-4-5'))

    @patch('quota._run_ai_usage')
    def test_over_limit_returns_false(self, mock_run):
        """Modelo con tokens por encima del límite → False."""
        mock_run.return_value = {
            'daily': [{
                'modelBreakdowns': [{
                    'modelName': 'claude-sonnet-4-5',
                    'inputTokens': 45_000_000,
                    'cacheCreationTokens': 0,
                    'outputTokens': 0,
                }],
            }],
        }
        # 45M / 50M = 90% usado > 80% weekly limit → blocked
        self.assertFalse(quota.is_model_ok('claude-sonnet-4-5'))

    @patch('quota._run_ai_usage')
    def test_unknown_in_quota_data_returns_true_optimistic(self, mock_run):
        """Si el modelo no está en la data → True (deja pasar)."""
        mock_run.return_value = {
            'daily': [{
                'modelBreakdowns': [{
                    'modelName': 'gemini-2.5-pro',
                    'inputTokens': 1_000,
                    'outputTokens': 0,
                    'cacheCreationTokens': 0,
                }],
            }],
        }
        # 'unknown-model' no aparece en la data
        self.assertTrue(quota.is_model_ok('unknown-model'))

    @patch('quota._run_ai_usage')
    def test_normalizes_dated_model_name_under_limit(self, mock_run):
        """Variante con fecha -YYYYMMDD: normaliza y deja pasar bajo el límite."""
        mock_run.return_value = {
            'daily': [{
                'modelBreakdowns': [{
                    'modelName': 'claude-sonnet-4-5',
                    'inputTokens': 10_000_000,
                    'outputTokens': 5_000_000,
                    'cacheCreationTokens': 0,
                }],
            }],
        }
        # 'claude-sonnet-4-5-20250514' → 'claude-sonnet-4-5' (normaliza)
        self.assertTrue(quota.is_model_ok('claude-sonnet-4-5-20250514'))

    @patch('quota._run_ai_usage')
    def test_normalizes_dated_model_name_over_limit(self, mock_run):
        """Variante con fecha -YYYYMMDD sobre el límite → blocked (normaliza)."""
        mock_run.return_value = {
            'daily': [{
                'modelBreakdowns': [{
                    'modelName': 'claude-sonnet-4-5',
                    'inputTokens': 45_000_000,
                    'outputTokens': 0,
                    'cacheCreationTokens': 0,
                }],
            }],
        }
        # Aunque la fecha cambia el id, la normalización debe encontrar la cfg.
        self.assertFalse(quota.is_model_ok('claude-sonnet-4-5-20250514'))


class TestNormalizeModelName(unittest.TestCase):
    """_normalize_model_name: stripping de sufijos de versión."""

    def test_strips_yyyymmdd_suffix(self):
        """Quita el sufijo -YYYYMMDD del final."""
        self.assertEqual(
            quota._normalize_model_name('claude-opus-4-6-20260101'),
            'claude-opus-4-6',
        )

    def test_no_suffix_unchanged(self):
        """Sin sufijo -YYYYMMDD, devuelve el nombre tal cual."""
        self.assertEqual(
            quota._normalize_model_name('qwen3-coder:30b'),
            'qwen3-coder:30b',
        )

    def test_does_not_strip_short_numeric_suffix(self):
        """Solo -DDDDDDDD (8 dígitos) se quita; '-4-5' se preserva."""
        self.assertEqual(
            quota._normalize_model_name('claude-sonnet-4-5'),
            'claude-sonnet-4-5',
        )


class TestFilterTier(unittest.TestCase):
    """filter_tier: filtrar + ordenar por relative_cost."""

    def setUp(self):
        quota._STATE.by_model_pct.clear()
        quota._STATE.call_log.clear()
        quota._STATE.timestamp = 0.0
        quota._STATE.last_refresh_started = 0.0

    def _specs(self):
        return (
            ModelSpec(id='claude-opus-4-6', family='claude',
                      role='cloud_big', relative_cost=9),
            ModelSpec(id='ollama:qwen3-coder:30b', family='qwen',
                      role='local', relative_cost=2),
            ModelSpec(id='gemini-2.5-flash', family='gemini',
                      role='cloud_mini', relative_cost=3),
        )

    @patch('quota._run_ai_usage')
    def test_returns_subset_sorted_by_relative_cost(self, mock_run):
        """Devuelve los que pasan is_model_ok, ordenados por coste ascendente."""
        mock_run.return_value = {'daily': []}
        result = quota.filter_tier(self._specs(), 'any')
        ids = [s.id for s in result]
        self.assertEqual(ids, [
            'ollama:qwen3-coder:30b',
            'gemini-2.5-flash',
            'claude-opus-4-6',
        ])

    @patch('quota._run_ai_usage')
    def test_all_ok_returns_all(self, mock_run):
        """Si todo pasa is_model_ok, devuelve la lista completa."""
        mock_run.return_value = {'daily': []}
        specs = self._specs()
        result = quota.filter_tier(specs, 'any')
        self.assertEqual(len(result), 3)
        self.assertEqual(set(s.id for s in result), set(s.id for s in specs))

    @patch('quota._run_ai_usage')
    def test_all_blocked_returns_empty(self, mock_run):
        """Si todos están bloqueados, devuelve []."""
        specs = (
            ModelSpec(id='claude-sonnet-4-5', family='claude',
                      role='cloud_mid', relative_cost=5),
            ModelSpec(id='gemini-2.5-flash', family='gemini',
                      role='cloud_mini', relative_cost=3),
        )
        mock_run.return_value = {
            'daily': [{
                'modelBreakdowns': [
                    {'modelName': 'claude-sonnet-4-5',
                     'inputTokens': 49_000_000, 'outputTokens': 0,
                     'cacheCreationTokens': 0},
                    {'modelName': 'gemini-2.5-flash',
                     'inputTokens': 199_000_000, 'outputTokens': 0,
                     'cacheCreationTokens': 0},
                ],
            }],
        }
        self.assertEqual(quota.filter_tier(specs, 'any'), [])


class TestSmartPick(unittest.TestCase):
    """smart_pick: selector de abundancia."""

    def setUp(self):
        quota._STATE.by_model_pct.clear()
        quota._STATE.call_log.clear()
        quota._STATE.timestamp = 0.0
        quota._STATE.last_refresh_started = 0.0

    def test_returns_single_spec(self):
        """Con un solo spec, lo devuelve."""
        spec = ModelSpec(id='ollama:qwen3-coder:30b', family='qwen',
                         role='local', relative_cost=2)
        self.assertEqual(quota.smart_pick((spec,), 'any'), spec)

    def test_returns_none_for_empty(self):
        """tuple vacío → None."""
        self.assertIsNone(quota.smart_pick((), 'any'))

    @patch('quota._run_ai_usage')
    def test_picks_ok_when_some_blocked(self, mock_run):
        """Si uno está bloqueado y otro OK, devuelve el OK."""
        mock_run.return_value = {
            'daily': [{
                'modelBreakdowns': [{
                    'modelName': 'claude-sonnet-4-5',
                    'inputTokens': 45_000_000,
                    'outputTokens': 0,
                    'cacheCreationTokens': 0,
                }],
            }],
        }
        blocked = ModelSpec(id='claude-sonnet-4-5', family='claude',
                            role='cloud_mid', relative_cost=5)
        ok = ModelSpec(id='ollama:qwen3-coder:30b', family='qwen',
                       role='local', relative_cost=2)
        self.assertEqual(quota.smart_pick((blocked, ok), 'any'), ok)

    @patch('quota._run_ai_usage')
    def test_all_blocked_returns_a_spec_from_input(self, mock_run):
        """Si todos están bloqueados, devuelve uno (rotación de gracia)."""
        mock_run.return_value = {
            'daily': [{
                'modelBreakdowns': [
                    {'modelName': 'claude-sonnet-4-5',
                     'inputTokens': 45_000_000, 'outputTokens': 0,
                     'cacheCreationTokens': 0},
                    {'modelName': 'gemini-2.5-flash',
                     'inputTokens': 199_000_000, 'outputTokens': 0,
                     'cacheCreationTokens': 0},
                ],
            }],
        }
        specs = (
            ModelSpec(id='claude-sonnet-4-5', family='claude',
                      role='cloud_mid', relative_cost=5),
            ModelSpec(id='gemini-2.5-flash', family='gemini',
                      role='cloud_mini', relative_cost=3),
        )
        result = quota.smart_pick(specs, 'any')
        self.assertIsNotNone(result)
        self.assertIn(result, specs)


class TestRecordAndSpend(unittest.TestCase):
    """record y local_spend: tracking local del run."""

    def setUp(self):
        quota._STATE.by_model_pct.clear()
        quota._STATE.call_log.clear()
        quota._STATE.timestamp = 0.0
        quota._STATE.last_refresh_started = 0.0

    def test_record_appends_to_call_log(self):
        """record añade una entrada con ts, model, in, out, eff."""
        quota.record('claude-sonnet-4-5', 100, 50)
        self.assertEqual(len(quota._STATE.call_log), 1)
        entry = quota._STATE.call_log[0]
        self.assertEqual(entry['model'], 'claude-sonnet-4-5')
        self.assertEqual(entry['in_tokens'], 100)
        self.assertEqual(entry['out_tokens'], 50)
        self.assertEqual(entry['effective_tokens'], 150)
        self.assertIsInstance(entry['ts'], float)

    def test_local_spend_aggregates_by_model(self):
        """local_spend agrupa por modelo: in, out, calls, eff."""
        quota.record('claude-sonnet-4-5', 100, 50)
        quota.record('claude-sonnet-4-5', 200, 100)
        quota.record('gemini-2.5-flash', 50, 25)
        spend = quota.local_spend()
        self.assertEqual(len(spend), 2)
        self.assertEqual(spend['claude-sonnet-4-5']['in'], 300)
        self.assertEqual(spend['claude-sonnet-4-5']['out'], 150)
        self.assertEqual(spend['claude-sonnet-4-5']['calls'], 2)
        self.assertEqual(spend['claude-sonnet-4-5']['eff'], 450)
        self.assertEqual(spend['gemini-2.5-flash']['in'], 50)
        self.assertEqual(spend['gemini-2.5-flash']['out'], 25)
        self.assertEqual(spend['gemini-2.5-flash']['calls'], 1)
        self.assertEqual(spend['gemini-2.5-flash']['eff'], 75)

    def test_local_spend_empty_log_returns_empty_dict(self):
        """local_spend con call_log vacío → {}."""
        self.assertEqual(quota.local_spend(), {})


class TestExtractPct(unittest.TestCase):
    """_extract_pct: parsing del JSON de ai-usage.sh."""

    def test_empty_daily_returns_empty_dict(self):
        """daily ausente o [] → {}."""
        self.assertEqual(quota._extract_pct({}), {})
        self.assertEqual(quota._extract_pct({'daily': []}), {})

    def test_daily_with_breakdowns_computes_tokens(self):
        """Calcula daily_tokens (último día) y weekly_tokens (suma)."""
        ai_data = {
            'daily': [
                {'modelBreakdowns': [{
                    'modelName': 'claude-sonnet-4-5',
                    'inputTokens': 1_000_000,
                    'cacheCreationTokens': 200_000,
                    'outputTokens': 500_000,
                }]},
                {'modelBreakdowns': [{
                    'modelName': 'claude-sonnet-4-5',
                    'inputTokens': 3_000_000,
                    'cacheCreationTokens': 100_000,
                    'outputTokens': 900_000,
                }]},
            ],
        }
        result = quota._extract_pct(ai_data)
        self.assertIn('claude-sonnet-4-5', result)
        info = result['claude-sonnet-4-5']
        # effective = input + cacheCreation + output
        self.assertEqual(info['daily_tokens'], 3_000_000 + 100_000 + 900_000)
        self.assertEqual(info['weekly_tokens'],
                         (1_000_000 + 200_000 + 500_000) +
                         (3_000_000 + 100_000 + 900_000))
        self.assertEqual(info['daily'], 0.0)
        self.assertEqual(info['weekly'], 0.0)


class TestReportAndBanner(unittest.TestCase):
    """report() y maybe_print_opus_banner()."""

    def setUp(self):
        quota._STATE.by_model_pct.clear()
        quota._STATE.call_log.clear()
        quota._STATE.timestamp = 0.0
        quota._STATE.last_refresh_started = 0.0
        quota._OPUS_BANNER_PRINTED = False

    @patch('quota._run_ai_usage')
    def test_report_returns_nonempty_string(self, mock_run):
        """report() devuelve string no vacío incluso sin quota data."""
        mock_run.return_value = {'daily': []}
        result = quota.report()
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)
        self.assertIn('Estado de cuotas', result)

    @patch('quota._run_ai_usage')
    def test_report_includes_local_spend(self, mock_run):
        """report() muestra el gasto local registrado."""
        mock_run.return_value = {'daily': []}
        quota.record('ollama:test', 1000, 500)
        result = quota.report()
        self.assertIn('ollama:test', result)
        self.assertIn('Gasto del run actual', result)

    def test_maybe_print_opus_banner_prints_once(self):
        """El banner se imprime una sola vez; llamadas posteriores no producen output."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            quota.maybe_print_opus_banner()
            first_output = buf.getvalue()
            quota.maybe_print_opus_banner()
            second_output = buf.getvalue()
        self.assertIn('OPUS', first_output)
        self.assertEqual(second_output, first_output)


class TestRefresh(unittest.TestCase):
    """refresh: cache de 60s + force."""

    def setUp(self):
        quota._STATE.by_model_pct.clear()
        quota._STATE.call_log.clear()
        quota._STATE.timestamp = 0.0
        quota._STATE.last_refresh_started = 0.0

    @patch('quota._run_ai_usage')
    def test_respects_cache_when_fresh(self, mock_run):
        """Estado fresh (< 60s): refresh NO llama a _run_ai_usage.

        El sentinel de frescura es `last_refresh_started` (no `timestamp`):
        refleja cuándo arrancó el último refresh, no cuándo terminó.
        """
        mock_run.return_value = {'daily': []}
        quota._STATE.last_refresh_started = time.time()
        quota.refresh()
        mock_run.assert_not_called()

    @patch('quota._run_ai_usage')
    def test_refresh_when_stale(self, mock_run):
        """Estado stale (> 60s): refresh sí llama a _run_ai_usage."""
        mock_run.return_value = {'daily': []}
        quota._STATE.timestamp = 0.0
        quota.refresh()
        self.assertGreaterEqual(mock_run.call_count, 1)

    @patch('quota._run_ai_usage')
    def test_force_always_refreshes(self, mock_run):
        """force=True ignora el cache y refresca igual."""
        mock_run.return_value = {'daily': []}
        quota._STATE.timestamp = time.time()
        quota.refresh(force=True)
        self.assertGreaterEqual(mock_run.call_count, 1)

    @patch('quota._run_ai_usage')
    def test_refresh_updates_timestamp(self, mock_run):
        """Después de refresh, timestamp está actualizado."""
        mock_run.return_value = {'daily': []}
        before = time.time()
        quota.refresh(force=True)
        self.assertGreaterEqual(quota._STATE.timestamp, before)


class TestThreadSafety(unittest.TestCase):
    """Concurrencia: record() no pierde updates con N threads."""

    def setUp(self):
        quota._STATE.by_model_pct.clear()
        quota._STATE.call_log.clear()
        quota._STATE.timestamp = 0.0
        quota._STATE.last_refresh_started = 0.0

    def test_record_thread_safe(self):
        """10 threads × 100 records = 1000 entries sin pérdida."""
        def worker(i):
            for _ in range(100):
                quota.record(f'model-{i % 3}', 10, 5)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            futures = [ex.submit(worker, i) for i in range(10)]
            for f in futures:
                f.result()

        self.assertEqual(len(quota._STATE.call_log), 1000)
        spend = quota.local_spend()
        total_calls = sum(s['calls'] for s in spend.values())
        self.assertEqual(total_calls, 1000)
        self.assertEqual(len(spend), 3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
