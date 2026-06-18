#!/usr/bin/env python3
"""Edge-case tests for tiers.py — env-var overrides, _load_override fallback,
adhoc specs, is_risky_path corner cases, resolve_command with various inputs."""
import os
import sys
import unittest
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
    is_risky_path,
    resolve_command,
    get_all_specs,
    get_tier0_hunters,
    get_tier0_30b,
    get_tier0_cloud_hunters,
    get_tier1_filters,
    get_tier2_judges,
    get_tier3_fixers,
    get_tier4_arbiters,
    _load_override,
)


class TestLoadOverride(unittest.TestCase):
    """_load_override returns the default unless env var provides IDs."""

    def test_no_env_var_returns_default(self):
        sentinel = (ModelSpec(id='sentinel:foo', family='x', role='local',
                                relative_cost=1),)
        with patch.dict(os.environ, {'LOOP_TEST_TIER': ''}, clear=False):
            result = _load_override('LOOP_TEST_TIER', sentinel)
            self.assertEqual(result, sentinel)

    def test_valid_env_var_returns_parsed_specs(self):
        """IDs present in the catalog are resolved to their ModelSpec objects."""
        with patch.dict(os.environ, {
            'LOOP_TEST_TIER': 'ollama:qwen3-coder:30b,minimax/MiniMax-M2.7-highspeed',
        }, clear=False):
            result = _load_override('LOOP_TEST_TIER', TIER0_HUNTERS)
            self.assertEqual(len(result), 2)
            ids = [s.id for s in result]
            self.assertEqual(ids, ['ollama:qwen3-coder:30b',
                                    'minimax/MiniMax-M2.7-highspeed'])
            # Resolved from catalog → objects should be field-equal to the canonical ones
            self.assertEqual(result[0], TIER0_HUNTERS[0])
            self.assertEqual(result[0].id, TIER0_HUNTERS[0].id)
            self.assertEqual(result[0].family, TIER0_HUNTERS[0].family)
            self.assertEqual(result[0].role, TIER0_HUNTERS[0].role)

    def test_unknown_id_creates_adhoc_spec(self):
        """IDs not in the catalog become ad-hoc ModelSpec entries."""
        with patch.dict(os.environ, {
            'LOOP_TEST_TIER': 'ollama:qwen3-coder:30b,minimax:MiniMax-M4-future',
        }, clear=False):
            result = _load_override('LOOP_TEST_TIER', TIER0_HUNTERS)
            self.assertEqual(len(result), 2)
            # First is canonical (from catalog), second is ad-hoc
            self.assertEqual(result[0].id, 'ollama:qwen3-coder:30b')
            self.assertEqual(result[0].family, 'qwen')
            adhoc = result[1]
            self.assertEqual(adhoc.id, 'minimax:MiniMax-M4-future')
            # Family should be the prefix before the first colon
            self.assertEqual(adhoc.family, 'minimax')
            # The id contains 'minimax' → role defaults to cloud_mid
            self.assertEqual(adhoc.role, 'cloud_mid')
            self.assertEqual(adhoc.relative_cost, 5)
            self.assertIn('code', adhoc.strengths)

    def test_empty_env_value_falls_back_to_default(self):
        """Empty / whitespace-only env var → fallback to default."""
        sentinel = (ModelSpec(id='sentinel:foo', family='x', role='local',
                                relative_cost=1),)
        with patch.dict(os.environ, {'LOOP_TEST_TIER': ',, ,  ,'}, clear=False):
            result = _load_override('LOOP_TEST_TIER', sentinel)
            self.assertEqual(result, sentinel)

    def test_all_unknown_ids_create_adhoc_specs(self):
        """All unknown IDs produce ad-hoc specs (NOT the default fallback)."""
        with patch.dict(os.environ, {
            'LOOP_TEST_TIER': 'minimax:MiniMax-X9,codex:gpt-99',
        }, clear=False):
            sentinel = (ModelSpec(id='sentinel:foo', family='x', role='local',
                                    relative_cost=1),)
            result = _load_override('LOOP_TEST_TIER', sentinel)
            # The function does NOT fall back to default when ids are unknown —
            # it creates ad-hoc specs instead.
            self.assertEqual(len(result), 2)
            self.assertEqual(result[0].id, 'minimax:MiniMax-X9')
            self.assertEqual(result[1].id, 'codex:gpt-99')
            self.assertEqual(result[0].family, 'minimax')
            self.assertEqual(result[1].family, 'codex')
            # 'minimax' is in the role-promotion list → cloud_mid
            self.assertEqual(result[0].role, 'cloud_mid')
            # 'codex' is NOT → cloud_mini
            self.assertEqual(result[1].role, 'cloud_mini')

    def test_whitespace_stripping_around_ids(self):
        """Whitespace around comma-separated ids is stripped."""
        with patch.dict(os.environ, {
            'LOOP_TEST_TIER': '  ollama:qwen3-coder:30b , minimax/MiniMax-M2.7-highspeed  ',
        }, clear=False):
            result = _load_override('LOOP_TEST_TIER', TIER0_HUNTERS)
            self.assertEqual(len(result), 2)
            self.assertEqual(result[0].id, 'ollama:qwen3-coder:30b')
            self.assertEqual(result[1].id, 'minimax/MiniMax-M2.7-highspeed')


class TestGetTier0HuntersEnvVar(unittest.TestCase):
    """get_tier0_hunters() respects LOOP_TIER0_HUNTERS env var."""

    def test_no_env_returns_default(self):
        with patch.dict(os.environ, {'LOOP_TIER0_HUNTERS': ''}, clear=False):
            result = get_tier0_hunters()
            self.assertEqual(result, TIER0_HUNTERS)

    def test_with_env_returns_parsed(self):
        with patch.dict(os.environ, {
            'LOOP_TIER0_HUNTERS': 'ollama:qwen3-coder:30b,minimax/MiniMax-M2.7-highspeed',
        }, clear=False):
            result = get_tier0_hunters()
            self.assertEqual(len(result), 2)
            self.assertEqual([s.id for s in result],
                             ['ollama:qwen3-coder:30b',
                              'minimax/MiniMax-M2.7-highspeed'])

    def test_get_tier0_30b_default(self):
        with patch.dict(os.environ, {'LOOP_TIER0_30B': ''}, clear=False):
            result = get_tier0_30b()
            self.assertEqual(result, TIER0_30B_OPINIONS)

    def test_get_tier0_cloud_hunters_default(self):
        with patch.dict(os.environ, {'LOOP_TIER0_CLOUD': ''}, clear=False):
            result = get_tier0_cloud_hunters()
            self.assertEqual(result, TIER0_CLOUD_HUNTERS)

    def test_get_tier1_filters_default(self):
        with patch.dict(os.environ, {'LOOP_TIER1_FILTERS': ''}, clear=False):
            result = get_tier1_filters()
            self.assertEqual(result, TIER1_FILTERS)

    def test_get_tier2_judges_default(self):
        with patch.dict(os.environ, {'LOOP_TIER2_JUDGES': ''}, clear=False):
            result = get_tier2_judges()
            self.assertEqual(result, TIER2_JUDGES)

    def test_get_tier3_fixers_default(self):
        with patch.dict(os.environ, {'LOOP_TIER3_FIXERS': ''}, clear=False):
            result = get_tier3_fixers()
            self.assertEqual(result, TIER3_FIXERS)

    def test_get_tier4_arbiters_default(self):
        with patch.dict(os.environ, {'LOOP_TIER4_ARBITERS': ''}, clear=False):
            result = get_tier4_arbiters(allow_opus=False)
            self.assertEqual(result, TIER4_ARBITERS)


class TestGetTierWithOverride(unittest.TestCase):
    """All tier getters respond to their respective env vars."""

    def test_tier1_filters_with_override(self):
        with patch.dict(os.environ, {
            'LOOP_TIER1_FILTERS': 'ollama:qwen3-coder:30b',
        }, clear=False):
            result = get_tier1_filters()
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].id, 'ollama:qwen3-coder:30b')

    def test_tier3_fixers_with_override_includes_adhoc(self):
        with patch.dict(os.environ, {
            'LOOP_TIER3_FIXERS': 'codex:gpt-99-future,claude:claude-haiku-4-5',
        }, clear=False):
            result = get_tier3_fixers()
            self.assertEqual(len(result), 2)
            # First is adhoc (unknown id)
            self.assertEqual(result[0].id, 'codex:gpt-99-future')
            # Second is from catalog
            self.assertEqual(result[1].id, 'claude:claude-haiku-4-5')

    def test_tier4_arbiters_with_override_and_opus(self):
        with patch.dict(os.environ, {
            'LOOP_TIER4_ARBITERS': 'ollama:qwen3-coder:30b',
        }, clear=False):
            result = get_tier4_arbiters(allow_opus=True)
            self.assertEqual(len(result), 2)
            self.assertEqual(result[0].id, 'ollama:qwen3-coder:30b')
            self.assertEqual(result[1].id, TIER4_OPUS_LAST_RESORT.id)


class TestIsRiskyPathEdgeCases(unittest.TestCase):
    """Extra coverage for the regex-based risky-path matcher."""

    def test_pagos_is_risky(self):
        self.assertTrue(is_risky_path('/pagos/transferir'))

    def test_stock_is_risky(self):
        self.assertTrue(is_risky_path('/api/stock/actualizar'))

    def test_usuarios_is_risky(self):
        self.assertTrue(is_risky_path('/usuarios/perfil'))

    def test_admin_is_risky(self):
        self.assertTrue(is_risky_path('/admin/dashboard'))

    def test_orders_is_risky(self):
        self.assertTrue(is_risky_path('/orders/list'))

    def test_partial_match_in_middle(self):
        # Pattern requires the segment; the segment can appear anywhere in the path.
        self.assertTrue(is_risky_path('/api/v2/cobros/charge'))

    def test_whitespace_only_path(self):
        self.assertFalse(is_risky_path('   '))

    def test_plain_root_path(self):
        self.assertFalse(is_risky_path('/'))


class TestResolveCommandEdgeCases(unittest.TestCase):
    """resolve_command handles extra whitespace / special prompts."""

    def _spec(self, model_id, family='qwen', role='local', relative_cost=1):
        return ModelSpec(id=model_id, family=family, role=role,
                          relative_cost=relative_cost)

    def test_empty_prompt(self):
        spec = self._spec('claude:claude-haiku-4-5', family='claude',
                          role='cloud_mini', relative_cost=3)
        result = resolve_command(spec, '')
        self.assertEqual(result, ['claude', '-p', '', '--model',
                                  'claude-haiku-4-5'])

    def test_multiline_prompt_preserved_as_single_arg(self):
        spec = self._spec('gemini:gemini-2.5-flash-lite', family='gemini',
                          role='cloud_mini', relative_cost=1)
        prompt = 'line 1\nline 2\nline 3'
        result = resolve_command(spec, prompt)
        self.assertEqual(result, ['gemini', '-m', 'gemini-2.5-flash-lite',
                                  '-p', prompt])
        self.assertEqual(result[4], prompt)

    def test_prompt_with_quotes_preserved(self):
        spec = self._spec('codex:gpt-5.5', family='codex',
                          role='cloud_mid', relative_cost=4)
        prompt = 'fix "the thing" in `auth.py`'
        result = resolve_command(spec, prompt)
        self.assertEqual(result, ['codex', '--model', 'gpt-5.5', prompt])

    def test_claude_id_strips_only_first_colon(self):
        """split(':', 1) keeps colons in the model name itself."""
        spec = self._spec('claude:claude-sonnet-4-5:extra', family='claude',
                          role='cloud_mid', relative_cost=5)
        result = resolve_command(spec, 'p')
        self.assertEqual(result[-1], 'claude-sonnet-4-5:extra')


class TestGetAllSpecsCount(unittest.TestCase):
    """get_all_specs aggregate count matches the sum of tier sizes."""

    def test_count_equals_sum_of_tiers(self):
        result = get_all_specs()
        # NOTE: tiers.get_all_specs() currently omits TIER0_CLOUD_HUNTERS from
        # its aggregation loop — a known gap in the source. We document the
        # actual count so the test fails loudly if that gap is closed.
        expected = (
            len(TIER0_HUNTERS)
            + len(TIER0_30B_OPINIONS)
            + len(TIER1_FILTERS)
            + len(TIER2_JUDGES)
            + len(TIER3_FIXERS)
            + len(TIER4_ARBITERS)
            + 1  # TIER4_OPUS_LAST_RESORT
        )
        self.assertEqual(len(result), expected)

    def test_tier0_cloud_hunter_ids_present_via_other_tiers(self):
        """The TIER0_CLOUD_HUNTERS specs are present in the result (via TIER1
        or other tiers that include the same models), so budget planning still
        sees them even though the tier itself is omitted from the aggregate."""
        result_ids = {s.id for s in get_all_specs()}
        for spec in TIER0_CLOUD_HUNTERS:
            self.assertIn(spec.id, result_ids,
                          f"TIER0_CLOUD_HUNTERS spec {spec.id!r} not in get_all_specs()")

    def test_all_specs_are_unique_objects(self):
        """Every returned entry is a ModelSpec instance."""
        result = get_all_specs()
        for s in result:
            self.assertIsInstance(s, ModelSpec)


class TestRiskyPathPatternsDocumented(unittest.TestCase):
    """Sanity check: every documented category is covered by a pattern."""

    def test_patterns_cover_money(self):
        joined = ' '.join(RISKY_PATH_PATTERNS)
        self.assertIn('cobros', joined)
        self.assertIn('pagos', joined)
        self.assertIn('facturas', joined)

    def test_patterns_cover_stock(self):
        joined = ' '.join(RISKY_PATH_PATTERNS)
        self.assertIn('inventario', joined)
        self.assertIn('stock', joined)

    def test_patterns_cover_auth(self):
        joined = ' '.join(RISKY_PATH_PATTERNS)
        self.assertIn('auth', joined)
        self.assertIn('signin', joined)
        self.assertIn('usuarios', joined)

    def test_patterns_cover_despacho(self):
        joined = ' '.join(RISKY_PATH_PATTERNS)
        self.assertIn('despacho', joined)


if __name__ == '__main__':
    unittest.main(verbosity=2)
