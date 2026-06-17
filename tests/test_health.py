#!/usr/bin/env python3
"""Tests del registro de salud / cuarentena de modelos — A-PR4."""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402


class TestHealth(unittest.TestCase):
    def setUp(self):
        with harness._HEALTH_LOCK:
            harness._HEALTH.clear()

    def test_unknown_is_healthy(self):
        self.assertTrue(harness.is_healthy('claude:claude-haiku-4-5'))

    def test_quarantine_after_threshold(self):
        mid = 'opencode:north-mini-code-free'
        for _ in range(harness.HEALTH_FAIL_THRESHOLD):
            harness._mark_health(mid, ok=False)
        self.assertFalse(harness.is_healthy(mid))
        self.assertIn(mid, harness.quarantined_models())

    def test_below_threshold_still_healthy(self):
        mid = 'minimax/MiniMax-M3'
        for _ in range(harness.HEALTH_FAIL_THRESHOLD - 1):
            harness._mark_health(mid, ok=False)
        self.assertTrue(harness.is_healthy(mid))

    def test_success_resets_fails(self):
        mid = 'gemini:gemini-2.5-pro'
        harness._mark_health(mid, ok=False)
        harness._mark_health(mid, ok=False)
        harness._mark_health(mid, ok=True)        # resetea
        harness._mark_health(mid, ok=False)        # 1 de nuevo, no cuarentena
        self.assertTrue(harness.is_healthy(mid))

    def test_seed_quarantine(self):
        harness.seed_quarantine(['codex:gpt-5.5'])
        self.assertFalse(harness.is_healthy('codex:gpt-5.5'))

    def test_quarantine_expires(self):
        mid = 'opencode:mimo-v2.5-free'
        with harness._HEALTH_LOCK:
            harness._HEALTH[mid] = {'fails': 9, 'quarantined_until': time.time() - 1,
                                    'calls': 9, 'oks': 0}
        self.assertTrue(harness.is_healthy(mid))   # ya expiró


if __name__ == '__main__':
    unittest.main(verbosity=2)
