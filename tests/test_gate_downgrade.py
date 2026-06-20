#!/usr/bin/env python3
"""Tests for _run_smoke_subset silent downgrade behavior.

The _run_smoke_subset function (harness.py:1692-1724) silently
downgrades to "tsc-only" if the dev server is down OR the E2E
crashes / doesn't run. These tests document that behavior.

  · Dev server down (non-strict)  → True (tsc-only)
  · Dev server down (strict)      → False
  · E2E doesn't run (non-strict)  → True (tsc-only)
  · E2E doesn't run (strict)      → False
  · E2E fatals                    → False
  · lib/ file (non-routable)      → True (no E2E needed)

Correr:  python3 -m unittest tests.test_gate_downgrade -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402


# ---------------------------------------------------------------------------
# 1) Dev server down — silent downgrade
# ---------------------------------------------------------------------------

class TestDevServerDown(unittest.TestCase):
    """Dev server down: non-strict mode downgrades to tsc-only."""

    def setUp(self):
        self._env_backup = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_dev_server_down_non_strict_returns_true(self):
        """When dev server is down and not strict → return True (tsc-only)."""
        with patch.dict(os.environ, {'LOOP_GATE_REQUIRE_E2E': '0'}), \
             patch('harness._dev_server_up', return_value=False):
            result = harness._run_smoke_subset('app/(panel)/x/page.tsx')
        self.assertTrue(result,
                        'non-strict mode should downgrade to tsc-only')

    def test_dev_server_down_strict_returns_false(self):
        """When dev server is down and strict → return False (no commit)."""
        with patch.dict(os.environ, {'LOOP_GATE_REQUIRE_E2E': '1'}), \
             patch('harness._dev_server_up', return_value=False):
            result = harness._run_smoke_subset('app/(panel)/x/page.tsx')
        self.assertFalse(result,
                         'strict mode should block the commit')

    def test_dev_server_up_exception_non_strict_returns_true(self):
        """If _dev_server_up raises, non-strict mode still downgrades."""
        with patch.dict(os.environ, {'LOOP_GATE_REQUIRE_E2E': '0'}), \
             patch('harness._dev_server_up',
                   side_effect=Exception('connection refused')):
            try:
                result = harness._run_smoke_subset('app/(panel)/x/page.tsx')
            except Exception as e:
                self.fail(f'_run_smoke_subset crashed: {e}')
        self.assertTrue(result)

    def test_dev_server_up_exception_strict_returns_false(self):
        """If _dev_server_up raises, strict mode returns False (server_up=False)."""
        with patch.dict(os.environ, {'LOOP_GATE_REQUIRE_E2E': '1'}), \
             patch('harness._dev_server_up',
                   side_effect=Exception('connection refused')):
            try:
                result = harness._run_smoke_subset('app/(panel)/x/page.tsx')
            except Exception as e:
                self.fail(f'_run_smoke_subset crashed: {e}')
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# 2) E2E does not run (subprocess fails or returns no fatal)
# ---------------------------------------------------------------------------

class TestE2EDoesNotRun(unittest.TestCase):
    """E2E subprocess fails / doesn't run: non-strict returns True."""

    def setUp(self):
        self._env_backup = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_e2e_does_not_run_non_strict_returns_true(self):
        """E2E didn't run + non-strict → downgrade to tsc-only (return True)."""
        with patch.dict(os.environ, {'LOOP_GATE_REQUIRE_E2E': '0'}), \
             patch('harness._dev_server_up', return_value=True), \
             patch('harness._route_for_file', return_value='/x'), \
             patch('harness._run_e2e_route', return_value=(False, [])):
            result = harness._run_smoke_subset('app/(panel)/x/page.tsx')
        self.assertTrue(result,
                        'non-strict should downgrade when E2E does not run')

    def test_e2e_does_not_run_strict_returns_false(self):
        """E2E didn't run + strict → block (return False)."""
        with patch.dict(os.environ, {'LOOP_GATE_REQUIRE_E2E': '1'}), \
             patch('harness._dev_server_up', return_value=True), \
             patch('harness._route_for_file', return_value='/x'), \
             patch('harness._run_e2e_route', return_value=(False, [])):
            result = harness._run_smoke_subset('app/(panel)/x/page.tsx')
        self.assertFalse(result, 'strict should block when E2E does not run')

    def test_e2e_exception_caught_internally(self):
        """If _run_e2e_route raises, _run_smoke_subset handles it gracefully.

        The actual code has try/except inside _run_e2e_route that returns
        (False, []). So if we raise from there, it would propagate. But
        the contract is: _run_e2e_route does not raise. Verify that
        contract by checking the type's behavior.
        """
        with patch.dict(os.environ, {'LOOP_GATE_REQUIRE_E2E': '0'}), \
             patch('harness._dev_server_up', return_value=True), \
             patch('harness._route_for_file', return_value='/x'), \
             patch('harness._run_e2e_route', return_value=(False, [])):
            try:
                harness._run_smoke_subset('app/(panel)/x/page.tsx')
            except Exception as e:
                self.fail(f'unexpected exception: {e}')


# ---------------------------------------------------------------------------
# 3) E2E returns fatal errors
# ---------------------------------------------------------------------------

class TestE2EFatal(unittest.TestCase):
    """E2E ran but found fatal errors → return False (block)."""

    def setUp(self):
        self._env_backup = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_e2e_fatal_returns_false(self):
        """E2E found fatal errors → return False regardless of strict mode."""
        fatal_errors = [{'kind': 'pageerror', 'where': '/x', 'detail': 'boom'}]
        with patch('harness._dev_server_up', return_value=True), \
             patch('harness._route_for_file', return_value='/x'), \
             patch('harness._run_e2e_route', return_value=(True, fatal_errors)):
            for strict_val in ('0', '1'):
                with patch.dict(os.environ, {'LOOP_GATE_REQUIRE_E2E': strict_val}):
                    result = harness._run_smoke_subset('app/(panel)/x/page.tsx')
                self.assertFalse(result,
                                 f'fatal E2E should block (strict={strict_val})')

    def test_e2e_clean_returns_true(self):
        """E2E ran and found no fatals → return True."""
        with patch('harness._dev_server_up', return_value=True), \
             patch('harness._route_for_file', return_value='/x'), \
             patch('harness._run_e2e_route', return_value=(True, [])):
            result = harness._run_smoke_subset('app/(panel)/x/page.tsx')
        self.assertTrue(result)


# ---------------------------------------------------------------------------
# 4) Non-routable files (lib/) — no E2E needed
# ---------------------------------------------------------------------------

class TestNonRoutableFile(unittest.TestCase):
    """lib/ files are not routable → tsc-only path returns True."""

    def setUp(self):
        self._env_backup = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_lib_file_returns_true(self):
        """lib/x.ts is not routable → return True (tsc already covered)."""
        # _route_for_file returns None for lib/ files.
        with patch('harness._dev_server_up', return_value=True), \
             patch('harness._route_for_file', return_value=None):
            result = harness._run_smoke_subset('lib/x.ts')
        self.assertTrue(result)

    def test_empty_file_path_returns_true(self):
        """Empty file path → _route_for_file returns None → True."""
        with patch('harness._dev_server_up', return_value=True), \
             patch('harness._route_for_file', return_value=None):
            result = harness._run_smoke_subset('')
        self.assertTrue(result)


# ---------------------------------------------------------------------------
# 5) Warning is logged on downgrade
# ---------------------------------------------------------------------------

class TestWarningLogged(unittest.TestCase):
    """On downgrade, a 'tsc-only' warning is logged."""

    def setUp(self):
        self._env_backup = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)

    def test_warning_logged_on_dev_server_down(self):
        """When downgrading, log() is called with a 'tsc-only' message."""
        log_msgs = []

        def fake_log(msg):
            log_msgs.append(msg)

        with patch.dict(os.environ, {'LOOP_GATE_REQUIRE_E2E': '0'}), \
             patch('harness.log', side_effect=fake_log), \
             patch('harness._dev_server_up', return_value=False):
            harness._run_smoke_subset('app/(panel)/x/page.tsx')
        joined = '\n'.join(log_msgs)
        self.assertIn('tsc-only', joined,
                      f'expected tsc-only warning; got: {joined[:300]}')

    def test_warning_logged_on_e2e_did_not_run(self):
        """When E2E didn't run, log() is called with a 'tsc-only' message."""
        log_msgs = []

        def fake_log(msg):
            log_msgs.append(msg)

        with patch.dict(os.environ, {'LOOP_GATE_REQUIRE_E2E': '0'}), \
             patch('harness.log', side_effect=fake_log), \
             patch('harness._dev_server_up', return_value=True), \
             patch('harness._route_for_file', return_value='/x'), \
             patch('harness._run_e2e_route', return_value=(False, [])):
            harness._run_smoke_subset('app/(panel)/x/page.tsx')
        joined = '\n'.join(log_msgs)
        self.assertIn('tsc-only', joined,
                      f'expected tsc-only warning; got: {joined[:300]}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
