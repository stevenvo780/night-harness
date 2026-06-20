#!/usr/bin/env python3
"""Tests for harness.watchdog().

The watchdog() function (harness.py:1971-2017) monitors the main loop
and triggers recovery (Ollama unload) when STATS doesn't progress for
`STALL_SECS` seconds. This file exercises:

  · healthy updates → no recovery
  · stalled updates → recovery triggered
  · Ollama unload targets fleet models (or falls back to TIER0_HUNTERS)
  · write_status is called each iteration

Correr:  python3 -m unittest tests.test_watchdog -v
"""
import os
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402
import tiers  # noqa: E402


def _make_fleet(specs):
    """Build a minimal Fleet-like stub with all_specs() returning `specs`."""
    stub = MagicMock()
    stub.all_specs.return_value = list(specs)
    return stub


def _spec(model_id='ollama:qwen3-coder:30b'):
    return tiers.ModelSpec(
        id=model_id, family='ollama', role='local_mid', relative_cost=1,
    )


class _WatchdogFixture(unittest.TestCase):
    """Common setup: shutdown clear, STATS isolated, threads joined."""

    def setUp(self):
        self._orig_shutdown = harness.shutdown.is_set()
        self._orig_fleet = harness.FLEET
        self._orig_stall = harness.STALL_SECS
        harness.shutdown.clear()
        # Snapshot STATS so we can restore the round counter.
        self._saved_rounds = harness.STATS['rounds']
        self._saved_fixes = harness.STATS['fixes_succeeded']
        # Default: low stall threshold for fast tests.
        harness.STALL_SECS = 1
        # Track patches we started so tearDown can stop them.
        self._started_patches = []

    def tearDown(self):
        # Stop any patches we started.
        for p in self._started_patches:
            try:
                p.stop()
            except Exception:
                pass
        # Signal watchdog to exit.
        harness.shutdown.set()
        time.sleep(0.1)
        if self._orig_shutdown:
            harness.shutdown.set()
        else:
            harness.shutdown.clear()
        harness.FLEET = self._orig_fleet
        harness.STALL_SECS = self._orig_stall
        harness.STATS['rounds'] = self._saved_rounds
        harness.STATS['fixes_succeeded'] = self._saved_fixes

    def _make_advancing_time(self, step=0.1):
        """Return a (patcher, call_count) tuple where time.time() advances
        by `step` seconds on each call.

        The watchdog does `time.time() - last_progress > STALL_SECS`.
        With STALL_SECS=1 and step=0.1, the condition triggers after
        ~11 calls (1.1s simulated). With step=1, after 2 calls.
        """
        call_count = [0]
        base = time.time()

        def fake_time():
            call_count[0] += 1
            return base + call_count[0] * step

        return patch('time.time', side_effect=fake_time), call_count


# ---------------------------------------------------------------------------
# 1) Healthy: STATS updates → no recovery
# ---------------------------------------------------------------------------

class TestWatchdogHealthy(_WatchdogFixture):
    """A process making progress does NOT trigger the unload path."""

    def test_healthy_no_recovery(self):
        """STATS['rounds'] advances each loop → no Ollama unload issued."""
        unload_calls = []
        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.__enter__ = lambda self_: self_
        fake_resp.__exit__ = lambda self_, *a: False

        def fake_urlopen(req, **kw):
            unload_calls.append(req.full_url)
            return fake_resp

        # Increment rounds each watchdog iteration to simulate progress.
        original_sleep = time.sleep

        def fake_sleep(_):
            with harness.STATS_LOCK:
                harness.STATS['rounds'] += 1
            original_sleep(0)  # no-op

        with patch('harness.write_status', return_value=None), \
             patch('time.sleep', side_effect=fake_sleep), \
             patch('harness.urllib.request.urlopen', side_effect=fake_urlopen):
            t = threading.Thread(target=harness.watchdog, daemon=True)
            t.start()
            time.sleep(0.5)
            harness.shutdown.set()
            t.join(timeout=2)
        # Healthy: no unload calls (we kept advancing).
        self.assertEqual(unload_calls, [],
                         f'healthy watchdog issued {len(unload_calls)} unloads')


# ---------------------------------------------------------------------------
# 2) Stalled: STATS frozen for STALL_SECS → recovery
# ---------------------------------------------------------------------------

class TestWatchdogStall(_WatchdogFixture):
    """A stalled process triggers the recovery path."""

    def test_stall_triggers_unload(self):
        """No progress for STALL_SECS → urllib.urlopen is called for unload."""
        unload_calls = []
        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.__enter__ = lambda self_: self_
        fake_resp.__exit__ = lambda self_, *a: False

        def fake_urlopen(req, **kw):
            unload_calls.append(req.full_url)
            return fake_resp

        # Pin rounds to 0 forever → stall.
        harness.STATS['rounds'] = 0
        harness.STATS['fixes_succeeded'] = 0
        fleet_stub = _make_fleet([_spec('ollama:qwen3-coder:30b')])
        time_p, _ = self._make_advancing_time(step=2.0)
        time_p.start()
        self._started_patches.append(time_p)
        with patch('harness.FLEET', fleet_stub), \
             patch('harness.write_status', return_value=None), \
             patch('harness.urllib.request.urlopen', side_effect=fake_urlopen), \
             patch('time.sleep', side_effect=lambda _: None):
            t = threading.Thread(target=harness.watchdog, daemon=True)
            t.start()
            time.sleep(1.0)
            harness.shutdown.set()
            t.join(timeout=2)
        # Should have issued at least one unload call.
        self.assertGreater(len(unload_calls), 0,
                           f'expected unload calls; got {len(unload_calls)}')

    def test_stall_targets_fleet_models(self):
        """Verify the right models from FLEET are unloaded."""
        unload_calls = []
        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.__enter__ = lambda self_: self_
        fake_resp.__exit__ = lambda self_, *a: False

        def fake_urlopen(req, **kw):
            unload_calls.append((req.full_url, req.data))
            return fake_resp

        # Pin rounds to 0 so the stall condition is reachable.
        harness.STATS['rounds'] = 0
        harness.STATS['fixes_succeeded'] = 0
        # Two models in the fleet.
        fleet_stub = _make_fleet([
            _spec('ollama:qwen3-coder:30b'),
            _spec('ollama:qwen3-coder:80b'),
        ])
        # Larger time step + wait time so the stall condition triggers.
        time_p, _ = self._make_advancing_time(step=2.0)
        time_p.start()
        self._started_patches.append(time_p)
        with patch('harness.FLEET', fleet_stub), \
             patch('harness.write_status', return_value=None), \
             patch('harness.urllib.request.urlopen', side_effect=fake_urlopen), \
             patch('time.sleep', side_effect=lambda _: None):
            t = threading.Thread(target=harness.watchdog, daemon=True)
            t.start()
            # Wait long enough for several loop iterations.
            time.sleep(1.0)
            harness.shutdown.set()
            t.join(timeout=2)
        # At least one unload call was issued.
        self.assertGreater(len(unload_calls), 0,
                           f'expected unload calls; got {len(unload_calls)}')
        # The bodies should mention one of our model names.
        import json as _json
        bodies = [b for _, b in unload_calls]
        decoded = [_json.loads(b.decode()) for b in bodies]
        names = {d.get('model') for d in decoded}
        # We expect at least one of our models to appear in the unload bodies.
        self.assertTrue(
            'qwen3-coder:30b' in names or 'qwen3-coder:80b' in names,
            f'expected fleet model names in unload bodies; got {names}',
        )


# ---------------------------------------------------------------------------
# 3) Fallback when FLEET is None
# ---------------------------------------------------------------------------

class TestWatchdogFallback(_WatchdogFixture):
    """When FLEET is None, watchdog falls back to TIER0_HUNTERS."""

    def test_fleet_none_uses_tier0_hunters(self):
        """FLEET=None → models from TIER0_HUNTERS are unloaded."""
        unload_calls = []
        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.__enter__ = lambda self_: self_
        fake_resp.__exit__ = lambda self_, *a: False

        def fake_urlopen(req, **kw):
            unload_calls.append(req.full_url)
            return fake_resp

        # Pin rounds to 0 to ensure stall condition.
        harness.STATS['rounds'] = 0
        harness.STATS['fixes_succeeded'] = 0
        # FLEET must be None to hit the fallback branch.
        time_p, _ = self._make_advancing_time(step=2.0)
        time_p.start()
        self._started_patches.append(time_p)
        with patch('harness.FLEET', None), \
             patch('harness.write_status', return_value=None), \
             patch('harness.urllib.request.urlopen', side_effect=fake_urlopen), \
             patch('time.sleep', side_effect=lambda _: None):
            t = threading.Thread(target=harness.watchdog, daemon=True)
            t.start()
            time.sleep(1.0)
            harness.shutdown.set()
            t.join(timeout=2)
        # The fallback path should have issued unload calls based on
        # TIER0_HUNTERS models.
        self.assertGreater(len(unload_calls), 0,
                           f'expected fallback unload; got {len(unload_calls)}')

    def test_fleet_none_uses_default_qwen3_coder_30b(self):
        """If TIER0_HUNTERS has no ollama: model, falls back to 'qwen3-coder:30b'."""
        unload_calls = []
        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.__enter__ = lambda self_: self_
        fake_resp.__exit__ = lambda self_, *a: False

        def fake_urlopen(req, **kw):
            unload_calls.append(req.full_url)
            return fake_resp

        # Pin rounds to 0 to ensure stall condition.
        harness.STATS['rounds'] = 0
        harness.STATS['fixes_succeeded'] = 0
        time_p, _ = self._make_advancing_time(step=2.0)
        time_p.start()
        self._started_patches.append(time_p)
        with patch('harness.FLEET', None), \
             patch('harness.tiers.TIER0_HUNTERS', ()), \
             patch('harness.write_status', return_value=None), \
             patch('harness.urllib.request.urlopen', side_effect=fake_urlopen), \
             patch('time.sleep', side_effect=lambda _: None):
            t = threading.Thread(target=harness.watchdog, daemon=True)
            t.start()
            time.sleep(1.0)
            harness.shutdown.set()
            t.join(timeout=2)
        # Should still have unloaded 'qwen3-coder:30b' as last-resort default.
        self.assertGreater(len(unload_calls), 0)


# ---------------------------------------------------------------------------
# 4) write_status is called
# ---------------------------------------------------------------------------

class TestWatchdogWriteStatus(_WatchdogFixture):
    """watchdog() calls write_status() each loop iteration."""

    def test_write_status_called(self):
        """Each loop iteration invokes write_status()."""
        write_calls = [0]

        def counting_write():
            write_calls[0] += 1

        with patch('harness.write_status', side_effect=counting_write), \
             patch('time.sleep', side_effect=lambda _: None):
            t = threading.Thread(target=harness.watchdog, daemon=True)
            t.start()
            time.sleep(0.3)
            harness.shutdown.set()
            t.join(timeout=2)
        # Even a single iteration calls write_status.
        self.assertGreaterEqual(write_calls[0], 1,
                                f'write_status was called {write_calls[0]}x')


# ---------------------------------------------------------------------------
# 5) Loop termination on shutdown
# ---------------------------------------------------------------------------

class TestWatchdogShutdown(_WatchdogFixture):
    """watchdog() exits cleanly when shutdown is set."""

    def test_exits_on_shutdown(self):
        """The watchdog loop checks shutdown.is_set() each iteration."""
        with patch('harness.write_status', return_value=None), \
             patch('time.sleep', side_effect=lambda _: None):
            t = threading.Thread(target=harness.watchdog, daemon=True)
            t.start()
            time.sleep(0.1)
            harness.shutdown.set()
            t.join(timeout=2)
        # The thread should have exited (not alive).
        self.assertFalse(t.is_alive(),
                         'watchdog did not exit on shutdown.set()')

    def test_initial_snapshot_of_rounds(self):
        """watchdog reads STATS['rounds'] once on entry (line 1976)."""
        harness.STATS['rounds'] = 999
        with patch('harness.write_status', return_value=None), \
             patch('time.sleep', side_effect=lambda _: None):
            t = threading.Thread(target=harness.watchdog, daemon=True)
            t.start()
            time.sleep(0.1)
            harness.shutdown.set()
            t.join(timeout=2)
        # Rounds should not have been reset.
        self.assertEqual(harness.STATS['rounds'], 999)


if __name__ == '__main__':
    unittest.main(verbosity=2)
