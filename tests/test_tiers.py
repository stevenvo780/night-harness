#!/usr/bin/env python3
"""Tests for tiers.py — public API: ModelSpec, tier tuples, resolve_command,
is_risky_path, get_tier4_arbiters, TARGET_CADENCE, RISKY_PATH_PATTERNS,
get_all_specs, spec invariants."""
import os
import re
import sys
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tiers  # noqa: E402
from tiers import (  # noqa: E402
    ModelSpec,
    TIER0_HUNTERS,
    TIER0_30B_OPINIONS,
    TIER0_CLOUD_HUNTERS,
    TIER1_FILTERS,
    TIER2_JUDGES,
    TIER3_FIXERS,
    TIER4_ARBITERS,
    TIER4_OPUS_LAST_RESORT,
    RISKY_PATH_PATTERNS,
    TARGET_CADENCE,
    is_risky_path,
    resolve_command,
    get_all_specs,
    get_tier4_arbiters,
)


VALID_ROLES = {'local', 'cloud_mini', 'cloud_mid', 'cloud_big'}
VALID_PREFIXES = (
    'ollama:',
    'gemini:',
    'opencode:',
    'opencode-go:',
    'minimax/',
    'codex:',
    'claude:',
)


class TestModelSpecDataclass(unittest.TestCase):
    """ModelSpec is frozen and equality is field-based."""

    def test_modelspec_is_frozen(self):
        spec = ModelSpec(id='claude:claude-haiku-4-5', family='claude',
                          role='cloud_mini', relative_cost=3)
        with self.assertRaises(FrozenInstanceError):
            spec.relative_cost = 99  # type: ignore[misc]

    def test_modelspec_equality_field_based(self):
        a = ModelSpec(id='claude:claude-haiku-4-5', family='claude',
                      role='cloud_mini', relative_cost=3, context_window=200_000)
        b = ModelSpec(id='claude:claude-haiku-4-5', family='claude',
                      role='cloud_mini', relative_cost=3, context_window=200_000)
        c = ModelSpec(id='claude:claude-haiku-4-5', family='claude',
                      role='cloud_mini', relative_cost=4, context_window=200_000)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        # Different ID → not equal
        d = ModelSpec(id='gemini:gemini-2.5-flash-lite', family='gemini',
                      role='cloud_mini', relative_cost=1)
        self.assertNotEqual(a, d)

    def test_modelspec_is_hashable(self):
        """Frozen dataclasses are hashable by default."""
        a = ModelSpec(id='claude:claude-haiku-4-5', family='claude',
                      role='cloud_mini', relative_cost=3)
        b = ModelSpec(id='claude:claude-haiku-4-5', family='claude',
                      role='cloud_mini', relative_cost=3)
        # Hashable + equal → same hash, can be used in sets
        self.assertEqual(hash(a), hash(b))
        self.assertEqual(len({a, b}), 1)


class TestTierTuples(unittest.TestCase):
    """Tier tuples are non-empty and follow the documented topology."""

    def test_tier0_hunters_is_tuple_with_at_least_1(self):
        self.assertIsInstance(TIER0_HUNTERS, tuple)
        self.assertGreaterEqual(len(TIER0_HUNTERS), 1)
        for s in TIER0_HUNTERS:
            self.assertIsInstance(s, ModelSpec)

    def test_tier0_hunters_first_entry_is_local_ollama(self):
        first = TIER0_HUNTERS[0]
        self.assertTrue(first.id.startswith('ollama:'),
                        f"first hunter should be ollama, got {first.id!r}")
        self.assertEqual(first.role, 'local')

    def test_tier1_filters_contains_minimax_m3(self):
        ids = [s.id for s in TIER1_FILTERS]
        self.assertIn('minimax/MiniMax-M3', ids)

    def test_tier2_judges_has_at_least_2_different_families(self):
        families = {s.family for s in TIER2_JUDGES}
        self.assertGreaterEqual(len(families), 2,
                                f"TIER2_JUDGES families: {families}")

    def test_tier3_fixers_starts_with_a_strong_model(self):
        first = TIER3_FIXERS[0]
        # "Strong" = has fix/reasoning in strengths AND not purely local mini.
        strengths = set(first.strengths)
        self.assertTrue('fix' in strengths or 'reasoning' in strengths,
                        f"first fixer should have fix/reasoning, got {first}")
        self.assertNotEqual(first.role, 'local',
                            "first fixer should be a strong cloud model")

    def test_tier4_arbiters_does_not_include_opus_by_default(self):
        ids = [s.id for s in TIER4_ARBITERS]
        self.assertNotIn(TIER4_OPUS_LAST_RESORT.id, ids)
        # Sanity: no entry has 'opus' in its id
        for s in TIER4_ARBITERS:
            self.assertNotIn('opus', s.id.lower(),
                             f"TIER4_ARBITERS unexpectedly contains Opus-like: {s.id}")

    def test_tier4_opus_last_resort_is_opus(self):
        self.assertIn('opus', TIER4_OPUS_LAST_RESORT.id.lower())
        self.assertEqual(TIER4_OPUS_LAST_RESORT.role, 'cloud_big')
        self.assertEqual(TIER4_OPUS_LAST_RESORT.family, 'claude')


class TestGetAllSpecs(unittest.TestCase):
    """get_all_specs() returns all known ModelSpecs."""

    def test_get_all_specs_returns_list(self):
        result = get_all_specs()
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        for s in result:
            self.assertIsInstance(s, ModelSpec)

    def test_get_all_specs_includes_every_tier(self):
        """Every entry of every tier tuple must be present in get_all_specs()."""
        result = get_all_specs()
        result_ids = [s.id for s in result]
        for tier in (TIER0_HUNTERS, TIER0_30B_OPINIONS, TIER0_CLOUD_HUNTERS,
                     TIER1_FILTERS, TIER2_JUDGES, TIER3_FIXERS, TIER4_ARBITERS):
            for spec in tier:
                self.assertIn(spec.id, result_ids,
                              f"{spec.id!r} from tier missing in get_all_specs()")

    def test_get_all_specs_includes_opus(self):
        result = get_all_specs()
        self.assertIn(TIER4_OPUS_LAST_RESORT.id, [s.id for s in result])


class TestIsRiskyPath(unittest.TestCase):
    """is_risky_path matches money/stock/state/auth paths."""

    def test_cobros_is_risky(self):
        self.assertTrue(is_risky_path('/cobros/nuevo'))

    def test_despacho_is_risky(self):
        self.assertTrue(is_risky_path('/despacho/recibidos'))

    def test_inventario_is_risky(self):
        self.assertTrue(is_risky_path('/inventario/stock'))

    def test_auth_is_risky(self):
        self.assertTrue(is_risky_path('/auth/signin'))

    def test_pedidos_is_not_risky(self):
        # pedidos is not in the documented risky patterns
        self.assertFalse(is_risky_path('/pedidos/list'))

    def test_dashboard_is_not_risky(self):
        self.assertFalse(is_risky_path('/dashboard'))

    def test_empty_string_is_not_risky(self):
        self.assertFalse(is_risky_path(''))

    def test_risky_path_patterns_is_tuple_of_strings(self):
        self.assertIsInstance(RISKY_PATH_PATTERNS, tuple)
        self.assertGreater(len(RISKY_PATH_PATTERNS), 0)
        for p in RISKY_PATH_PATTERNS:
            self.assertIsInstance(p, str)
            # Each pattern should compile as a valid regex
            re.compile(p)


class TestResolveCommand(unittest.TestCase):
    """resolve_command translates a ModelSpec into an argv list."""

    def _spec(self, model_id, family='qwen', role='local', relative_cost=1):
        return ModelSpec(id=model_id, family=family, role=role,
                          relative_cost=relative_cost)

    def test_ollama_raises_value_error(self):
        spec = self._spec('ollama:qwen3-coder:30b')
        with self.assertRaises(ValueError):
            resolve_command(spec, 'hello')

    def test_minimax_returns_opencode_invoke(self):
        spec = self._spec('minimax:MiniMax-M3', family='minimax',
                          role='cloud_mini', relative_cost=2)
        result = resolve_command(spec, 'find bugs')
        self.assertEqual(result, ['opencode', '--model', 'MiniMax-M3', 'find bugs'])

    def test_opencode_returns_run_stdin(self):
        spec = self._spec('opencode:deepseek-v4-flash-free', family='opencode',
                          role='cloud_mini', relative_cost=1)
        result = resolve_command(spec, 'audit')
        self.assertEqual(result, ['opencode', 'run', '--model',
                                  'opencode:deepseek-v4-flash-free', '-'])

    def test_opencode_go_returns_run_stdin(self):
        spec = self._spec('opencode-go:qwen3.7-max', family='opencode_go',
                          role='cloud_big', relative_cost=5)
        result = resolve_command(spec, 'rebuild')
        self.assertEqual(result, ['opencode', 'run', '--model',
                                  'opencode-go:qwen3.7-max', '-'])

    def test_gemini_returns_m_p(self):
        spec = self._spec('gemini:gemini-2.5-flash-lite', family='gemini',
                          role='cloud_mini', relative_cost=1)
        result = resolve_command(spec, 'review')
        self.assertEqual(result, ['gemini', '-m', 'gemini-2.5-flash-lite',
                                  '-p', 'review'])

    def test_claude_returns_p_model(self):
        spec = self._spec('claude:claude-haiku-4-5', family='claude',
                          role='cloud_mini', relative_cost=3)
        result = resolve_command(spec, 'audit login')
        # Order: claude, -p, prompt, --model, modelname
        self.assertEqual(result, ['claude', '-p', 'audit login', '--model',
                                  'claude-haiku-4-5'])

    def test_codex_returns_model_prompt(self):
        spec = self._spec('codex:gpt-5.5', family='codex',
                          role='cloud_mid', relative_cost=4)
        result = resolve_command(spec, 'fix the bug')
        self.assertEqual(result, ['codex', '--model', 'gpt-5.5', 'fix the bug'])

    def test_unknown_prefix_raises_value_error(self):
        spec = self._spec('mystery:model-x', family='unknown',
                          role='cloud_mid', relative_cost=3)
        with self.assertRaises(ValueError):
            resolve_command(spec, 'go')

    def test_system_param_accepted(self):
        """resolve_command accepts the system keyword but does not crash."""
        spec = self._spec('claude:claude-haiku-4-5', family='claude',
                          role='cloud_mini', relative_cost=3)
        result = resolve_command(spec, 'hi', system='be terse')
        # system is part of the signature; the argv is unchanged
        self.assertEqual(result, ['claude', '-p', 'hi', '--model',
                                  'claude-haiku-4-5'])


class TestTargetCadence(unittest.TestCase):
    """TARGET_CADENCE is a dict with expected keys and numeric values."""

    def test_target_cadence_is_dict(self):
        self.assertIsInstance(TARGET_CADENCE, dict)
        self.assertGreater(len(TARGET_CADENCE), 0)

    def test_target_cadence_has_expected_keys(self):
        expected = {
            'tier0_scanner_per_min',
            'tier1_filter_per_min',
            'tier2_judge_per_min',
            'tier3_fix_per_min',
            'tier4_arbiter_per_min',
        }
        self.assertEqual(set(TARGET_CADENCE.keys()), expected)
        for k, v in TARGET_CADENCE.items():
            self.assertIsInstance(v, (int, float),
                                  f"{k} should be numeric, got {type(v)}")
            self.assertGreater(v, 0, f"{k} should be > 0")


class TestSpecInvariants(unittest.TestCase):
    """Catalog-wide invariants: prefix format, cost range, role set."""

    def test_all_spec_ids_match_known_prefix(self):
        result = get_all_specs()
        for s in result:
            self.assertTrue(
                s.id.startswith(VALID_PREFIXES),
                f"spec id {s.id!r} does not start with any known prefix",
            )

    def test_all_spec_relative_cost_in_range(self):
        for s in get_all_specs():
            self.assertIsInstance(s.relative_cost, int)
            self.assertGreaterEqual(s.relative_cost, 1,
                                    f"{s.id}: relative_cost < 1")
            self.assertLessEqual(s.relative_cost, 10,
                                 f"{s.id}: relative_cost > 10")

    def test_all_spec_roles_are_valid(self):
        for s in get_all_specs():
            self.assertIn(s.role, VALID_ROLES,
                          f"{s.id}: role {s.role!r} not in {VALID_ROLES}")


class TestGetTier4Arbiters(unittest.TestCase):
    """get_tier4_arbiters honours the allow_opus flag."""

    def setUp(self):
        # Ensure no override env var leaks in from the host.
        self._env = patch.dict(os.environ, {
            'LOOP_TIER4_ARBITERS': '',
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_allow_opus_false_excludes_opus(self):
        result = get_tier4_arbiters(allow_opus=False)
        ids = [s.id for s in result]
        self.assertNotIn(TIER4_OPUS_LAST_RESORT.id, ids)

    def test_allow_opus_true_appends_opus_at_end(self):
        result = get_tier4_arbiters(allow_opus=True)
        self.assertGreater(len(result), 0)
        self.assertEqual(result[-1], TIER4_OPUS_LAST_RESORT)
        self.assertEqual(result[-1].id, TIER4_OPUS_LAST_RESORT.id)

    def test_default_allow_opus_false(self):
        # Calling without arg must default to allow_opus=False.
        result = get_tier4_arbiters()
        ids = [s.id for s in result]
        self.assertNotIn(TIER4_OPUS_LAST_RESORT.id, ids)


if __name__ == '__main__':
    unittest.main(verbosity=2)
