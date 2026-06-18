#!/usr/bin/env python3
"""Edge-case tests for the model health / quarantine subsystem in harness.py."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402


class TestHealthEdgeCases(unittest.TestCase):
    """Health/quarantine edge cases beyond the basic threshold tests."""

    def setUp(self):
        with harness._HEALTH_LOCK:
            harness._HEALTH.clear()

    def tearDown(self):
        with harness._HEALTH_LOCK:
            harness._HEALTH.clear()

    def test_unknown_model_is_healthy_optimistic(self):
        """A model never seen before is considered healthy."""
        self.assertTrue(harness.is_healthy('ollama:qwen3-coder:30b'))

    def test_quarantine_after_fail_threshold_sets_until(self):
        """After FAIL_THRESHOLD failures the model is unhealthy and quarantined_until is set."""
        mid = 'opencode:deepseek-v4-flash-free'
        for _ in range(harness.HEALTH_FAIL_THRESHOLD):
            harness._mark_health(mid, ok=False)
        self.assertFalse(harness.is_healthy(mid))
        with harness._HEALTH_LOCK:
            self.assertGreater(harness._HEALTH[mid]['quarantined_until'], time.time())

    def test_future_quarantine_until_is_unhealthy(self):
        """quarantined_until in the future means the model is unhealthy."""
        mid = 'gemini:gemini-2.5-pro'
        with harness._HEALTH_LOCK:
            harness._HEALTH[mid] = {
                'fails': 0,
                'quarantined_until': time.time() + 10_000,
                'calls': 1,
                'oks': 0,
            }
        self.assertFalse(harness.is_healthy(mid))

    def test_past_quarantine_until_expires_and_becomes_healthy(self):
        """quarantined_until in the past means quarantine expired → healthy."""
        mid = 'minimax/MiniMax-M3'
        with harness._HEALTH_LOCK:
            harness._HEALTH[mid] = {
                'fails': 9,
                'quarantined_until': time.time() - 1,
                'calls': 9,
                'oks': 0,
            }
        self.assertTrue(harness.is_healthy(mid))

    def test_success_clears_fail_streak_but_not_active_quarantine(self):
        """ok=True must reset fails but must NOT lift a still-active quarantine."""
        mid = 'claude:claude-haiku-4-5'
        # Put model into active quarantine.
        with harness._HEALTH_LOCK:
            harness._HEALTH[mid] = {
                'fails': harness.HEALTH_FAIL_THRESHOLD,
                'quarantined_until': time.time() + 10_000,
                'calls': harness.HEALTH_FAIL_THRESHOLD,
                'oks': 0,
            }
        harness._mark_health(mid, ok=True)
        with harness._HEALTH_LOCK:
            h = harness._HEALTH[mid]
            self.assertEqual(h['fails'], 0)
            self.assertGreater(h['quarantined_until'], time.time())
        self.assertFalse(harness.is_healthy(mid))

    def test_seed_quarantine_immediate(self):
        """seed_quarantine puts models into quarantine for QUARANTINE_SECS immediately."""
        mid = 'custom:zombie-model'
        harness.seed_quarantine([mid])
        self.assertFalse(harness.is_healthy(mid))
        with harness._HEALTH_LOCK:
            h = harness._HEALTH[mid]
            self.assertEqual(h['fails'], harness.HEALTH_FAIL_THRESHOLD)
            remaining = h['quarantined_until'] - time.time()
            self.assertGreater(remaining, 0)
            self.assertLess(remaining, harness.QUARANTINE_SECS + 1)

    def test_quarantined_models_returns_all_quarantined(self):
        """quarantined_models returns every model with active quarantine."""
        now = time.time()
        with harness._HEALTH_LOCK:
            harness._HEALTH['temp-model'] = {
                'fails': 0,
                'quarantined_until': now + 100,
                'calls': 0,
                'oks': 0,
            }
            harness._HEALTH['perm-model'] = {
                'fails': 99,
                'quarantined_until': now + 200,
                'calls': 99,
                'oks': 0,
            }
            harness._HEALTH['healthy-model'] = {
                'fails': 0,
                'quarantined_until': 0.0,
                'calls': 0,
                'oks': 0,
            }
        q = harness.quarantined_models()
        self.assertIn('temp-model', q)
        self.assertIn('perm-model', q)
        self.assertNotIn('healthy-model', q)

    def test_mark_health_creates_entry_for_unknown_model(self):
        """_mark_health on a previously unknown model creates a new health record."""
        mid = 'new:model'
        self.assertNotIn(mid, harness._HEALTH)
        harness._mark_health(mid, ok=True)
        with harness._HEALTH_LOCK:
            self.assertIn(mid, harness._HEALTH)
            h = harness._HEALTH[mid]
            self.assertEqual(h['calls'], 1)
            self.assertEqual(h['oks'], 1)
            self.assertEqual(h['fails'], 0)

    def test_callcount_and_deadrate_tracking(self):
        """calls and oks are tracked so deadrate can be derived."""
        mid = 'tracked:model'
        for _ in range(7):
            harness._mark_health(mid, ok=True)
        for _ in range(3):
            harness._mark_health(mid, ok=False)
        with harness._HEALTH_LOCK:
            h = harness._HEALTH[mid]
            self.assertEqual(h['calls'], 10)
            self.assertEqual(h['oks'], 7)
        deadrate = (h['calls'] - h['oks']) / h['calls'] if h['calls'] else 0.0
        self.assertAlmostEqual(deadrate, 0.30)


if __name__ == '__main__':
    unittest.main(verbosity=2)
