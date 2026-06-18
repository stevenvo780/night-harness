#!/usr/bin/env python3
"""E2E pipeline edge cases — _e2e_task_worker behavior, error paths, sentinel,
queue interaction, and discover_zones contract."""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import harness  # noqa: E402


def _make_response(status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.__enter__ = lambda self_: self_
    resp.__exit__ = lambda self_, *a: False
    return resp


class _WTFixture(unittest.TestCase):
    """Isolated worktree with panel routes for the worker to map errors to."""

    PANEL_ROUTES = ('clientes', 'inventario', 'cobros')

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        panel = self.tmpdir / 'app' / '(panel)'
        for r in self.PANEL_ROUTES:
            (panel / r).mkdir(parents=True, exist_ok=True)
            (panel / r / 'page.tsx').write_text(f'// {r}\n')
        (self.tmpdir / 'app' / 'page.tsx').parent.mkdir(parents=True, exist_ok=True)
        (self.tmpdir / 'app' / 'page.tsx').write_text('// home\n')

        self._saved_wt = harness.WT
        self._saved_subdir = harness.SUBDIR
        self._saved_wt_subdir = harness.WT_SUBDIR
        self._saved_dev_url = harness.DEV_URL
        self._saved_storage = harness.STORAGE
        self._saved_shutdown = harness.shutdown.is_set()
        self._saved_skips = harness.STATS.get('e2e_devserver_skips', 0)
        self._saved_e2e_findings = harness.STATS.get('e2e_findings', 0)

        harness.WT = self.tmpdir
        harness.SUBDIR = '.'
        harness.WT_SUBDIR = self.tmpdir
        harness.DEV_URL = 'http://test-host:3011'
        harness.STORAGE = ''
        harness.shutdown.clear()

    def tearDown(self):
        harness.WT = self._saved_wt
        harness.SUBDIR = self._saved_subdir
        harness.WT_SUBDIR = self._saved_wt_subdir
        harness.DEV_URL = self._saved_dev_url
        harness.STORAGE = self._saved_storage
        if self._saved_shutdown:
            harness.shutdown.set()
        else:
            harness.shutdown.clear()
        harness.STATS['e2e_devserver_skips'] = self._saved_skips
        harness.STATS['e2e_findings'] = self._saved_e2e_findings
        # Drain queues so we don't leak between tests.
        for q in (harness.e2e_taskq, harness.suspectq):
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        self._tmp.cleanup()


class TestE2ETaskWorkerSentinel(_WTFixture):
    """_e2e_task_worker: sentinel None → exit. Task with dev-down → skip+counter."""

    def test_sentinel_none_exits_worker(self):
        """Pushing (None, *, *) makes the worker break out of the consume loop."""
        harness.e2e_taskq.put((None, 'sentinel', 0))
        t = threading.Thread(target=harness._e2e_task_worker, kwargs={'wid': 99}, daemon=True)
        t.start()
        t.join(timeout=3)
        self.assertFalse(t.is_alive(), 'worker should exit on None sentinel')

    def test_dev_server_down_increments_skip(self):
        """_dev_server_alive=False → e2e_devserver_skips counter incremented, no runner call."""
        runner_called = []

        def fake_run(*a, **kw):
            runner_called.append(True)
            return MagicMock()

        with patch('harness._dev_server_alive', return_value=False), \
             patch('harness.subprocess.run', side_effect=fake_run), \
             patch('harness.time.sleep'):  # avoid 2s sleep
            harness.e2e_taskq.put(('/clientes', 'mod', 1))
            harness.e2e_taskq.put((None, 'sentinel', 0))
            t = threading.Thread(target=harness._e2e_task_worker, kwargs={'wid': 0}, daemon=True)
            t.start()
            t.join(timeout=3)
        self.assertFalse(t.is_alive())
        self.assertEqual(runner_called, [], 'subprocess.run must NOT be called when dev is down')
        skips = harness.STATS.get('e2e_devserver_skips', 0) - self._saved_skips
        self.assertEqual(skips, 1, 'e2e_devserver_skips should increment exactly once')

    def test_dev_server_down_repeated_increments_each_skip(self):
        """Multiple down-skips stack on the counter (one per task)."""
        with patch('harness._dev_server_alive', return_value=False), \
             patch('harness.subprocess.run'), \
             patch('harness.time.sleep'):
            for _ in range(3):
                harness.e2e_taskq.put(('/clientes', 'mod', 1))
            harness.e2e_taskq.put((None, 'sentinel', 0))
            t = threading.Thread(target=harness._e2e_task_worker, kwargs={'wid': 0}, daemon=True)
            t.start()
            t.join(timeout=3)
        skips = harness.STATS.get('e2e_devserver_skips', 0) - self._saved_skips
        self.assertEqual(skips, 3)


class TestE2ETaskWorkerSubprocessPaths(_WTFixture):
    """_e2e_task_worker: TimeoutExpired, JSONDecodeError, fatal output."""

    def _run_worker(self, items):
        """Push items + None sentinel, run worker to completion, return."""
        for it in items:
            harness.e2e_taskq.put(it)
        harness.e2e_taskq.put((None, 'sentinel', 0))
        t = threading.Thread(target=harness._e2e_task_worker, kwargs={'wid': 0}, daemon=True)
        t.start()
        t.join(timeout=5)
        return t

    def test_subprocess_timeout_doesnt_crash_worker(self):
        """subprocess.run raises TimeoutExpired → worker continues, no Suspect emitted."""
        with patch('harness._dev_server_alive', return_value=True), \
             patch('harness.subprocess.run',
                   side_effect=subprocess.TimeoutExpired(cmd='node', timeout=180)):
            t = self._run_worker([('/clientes', 'mod', 1)])
        self.assertFalse(t.is_alive(), 'worker should exit cleanly on timeout')

    def test_json_decode_error_doesnt_crash_worker(self):
        """subprocess.run returns garbage that's not JSON → worker continues, no Suspect."""
        m = MagicMock()
        m.stdout = 'NOT JSON AT ALL'
        m.stderr = ''
        with patch('harness._dev_server_alive', return_value=True), \
             patch('harness.subprocess.run', return_value=m):
            t = self._run_worker([('/clientes', 'mod', 1)])
        self.assertFalse(t.is_alive(), 'worker should exit cleanly on bad JSON')

    def test_fatal_output_skips_emitting_suspect(self):
        """out.get('fatal') is set → worker logs and continues; no Suspect queued."""
        m = MagicMock()
        m.stdout = json.dumps({'fatal': 'browser context destroyed'})
        m.stderr = ''
        with patch('harness._dev_server_alive', return_value=True), \
             patch('harness.subprocess.run', return_value=m), \
             patch('harness.yield_suspect') as ys:
            t = self._run_worker([('/clientes', 'mod', 1)])
        self.assertFalse(t.is_alive())
        ys.assert_not_called()


class TestE2ETaskWorkerEmitsSuspects(_WTFixture):
    """_e2e_task_worker: error mapping, suspect emission, file resolution."""

    def _run_with_output(self, output: dict, task=('/clientes', 'mod', 1)) -> MagicMock:
        """Mock subprocess to return `output`; run worker; return the yield_suspect mock."""
        m = MagicMock()
        m.stdout = json.dumps(output)
        m.stderr = ''
        ys = MagicMock()
        with patch('harness._dev_server_alive', return_value=True), \
             patch('harness.subprocess.run', return_value=m), \
             patch('harness.yield_suspect', ys), \
             patch('harness.yield_suspect', ys):
            harness.e2e_taskq.put(task)
            harness.e2e_taskq.put((None, 'sentinel', 0))
            t = threading.Thread(target=harness._e2e_task_worker, kwargs={'wid': 0}, daemon=True)
            t.start()
            t.join(timeout=5)
        return ys

    def test_emits_suspect_for_each_error(self):
        """One error in output → exactly one yield_suspect call."""
        output = {
            'route': '/clientes',
            'errors': [{'kind': 'pageerror', 'detail': 'TypeError: x is undefined', 'line': 42}],
        }
        ys = self._run_with_output(output)
        ys.assert_called_once()
        sus = ys.call_args[0][0]
        # File should be the panel route mapping.
        self.assertEqual(sus.file, 'app/(panel)/clientes/page.tsx')
        self.assertEqual(sus.source, 'e2e')
        self.assertEqual(sus.line, 42)
        self.assertIn('pageerror', sus.why)
        self.assertIn('/clientes', sus.why)

    def test_maps_route_to_file_via_panel_candidate(self):
        """Each error's route is mapped via _map_e2e_route_to_file → panel path wins."""
        output = {
            'route': '/cobros',
            'errors': [{'kind': 'http', 'detail': '500'}],
        }
        ys = self._run_with_output(output)
        sus = ys.call_args[0][0]
        self.assertEqual(sus.file, 'app/(panel)/cobros/page.tsx')

    def test_severity_high_for_critical_kinds(self):
        """kind in {http500, crash, fatal} → severity='high'."""
        for kind in ('http500', 'crash', 'fatal'):
            output = {
                'route': '/clientes',
                'errors': [{'kind': kind, 'detail': 'x'}],
            }
            ys = self._run_with_output(output)
            sus = ys.call_args[0][0]
            self.assertEqual(sus.severity, 'high', f'kind={kind} should be high')

    def test_severity_medium_for_other_kinds(self):
        """Other kinds → severity='medium'."""
        output = {
            'route': '/clientes',
            'errors': [{'kind': 'console', 'detail': 'warn'}],
        }
        ys = self._run_with_output(output)
        sus = ys.call_args[0][0]
        self.assertEqual(sus.severity, 'medium')

    def test_error_with_non_digit_line_defaults_to_one(self):
        """'line' field not a digit string → line=1 (defensive)."""
        output = {
            'route': '/clientes',
            'errors': [{'kind': 'pageerror', 'detail': 'x', 'line': 'nope'}],
        }
        ys = self._run_with_output(output)
        sus = ys.call_args[0][0]
        self.assertEqual(sus.line, 1)

    def test_unmappable_route_skipped(self):
        """If _map_e2e_route_to_file returns None → error is logged, no Suspect emitted."""
        output = {
            'route': '/no-such-route',  # not in candidates
            'errors': [{'kind': 'pageerror', 'detail': 'x'}],
        }
        ys = self._run_with_output(output)
        ys.assert_not_called()

    def test_multiple_errors_each_become_suspect(self):
        """3 errors in output → 3 yield_suspect calls."""
        output = {
            'route': '/clientes',
            'errors': [
                {'kind': 'http', 'detail': 'a'},
                {'kind': 'pageerror', 'detail': 'b'},
                {'kind': 'console', 'detail': 'c'},
            ],
        }
        ys = self._run_with_output(output)
        self.assertEqual(ys.call_count, 3)
        # Increment e2e_findings by 3.
        findings_delta = harness.STATS.get('e2e_findings', 0) - self._saved_e2e_findings
        self.assertEqual(findings_delta, 3)

    def test_empty_errors_list_emits_nothing(self):
        """No errors → yield_suspect not called, e2e_findings unchanged."""
        output = {'route': '/clientes', 'errors': []}
        ys = self._run_with_output(output)
        ys.assert_not_called()
        findings_delta = harness.STATS.get('e2e_findings', 0) - self._saved_e2e_findings
        self.assertEqual(findings_delta, 0)


class TestDiscoverZonesContract(_WTFixture):
    """discover_zones: returns the canonical zone strings the e2e producer depends on."""

    def test_discovers_only_immediate_panel_subdirs(self):
        """Each immediate subdir of app/(panel) becomes a zone."""
        zones = harness.discover_zones()
        names = [z.split('/')[-2] for z in zones]
        self.assertIn('clientes', names)
        self.assertIn('inventario', names)
        self.assertIn('cobros', names)
        self.assertEqual(len(zones), 3)

    def test_zone_format_starts_with_app_panel(self):
        """Zones are strings prefixed with 'app/(panel)/' and end with '/'."""
        for z in harness.discover_zones():
            self.assertTrue(z.startswith('app/(panel)/'), z)
            self.assertTrue(z.endswith('/'), z)

    def test_excludes_nested_groups(self):
        """discover_zones is one level deep — not recursive into nested route groups."""
        nested = self.tmpdir / 'app' / '(panel)' / 'cobros' / '(nested)'
        nested.mkdir(parents=True, exist_ok=True)
        (nested / 'page.tsx').write_text('// nested\n')
        zones = harness.discover_zones()
        # Should not contain a path with another /(nested)/ segment.
        for z in zones:
            self.assertNotIn('(nested)', z)

    def test_empty_when_no_panel_dir(self):
        """If app/(panel) doesn't exist → returns []."""
        # Replace the panel dir we set up.
        import shutil
        shutil.rmtree(self.tmpdir / 'app' / '(panel)')
        zones = harness.discover_zones()
        self.assertEqual(zones, [])


class TestE2ETaskQueueCapacity(_WTFixture):
    """e2e_taskq: producer respects the queue capacity, drops when full."""

    def test_full_queue_does_not_block_producer(self):
        """Producer uses put_nowait and breaks on queue.Full — never blocks."""
        stop = threading.Event()
        # Saturate the queue (maxsize=400) with placeholder tasks.
        filler = [('/x', 'm', 1)] * 400
        for f in filler:
            harness.e2e_taskq.put(f)
        t = threading.Thread(target=harness.e2e_producer, args=(stop,), daemon=True)
        t.start()
        time.sleep(0.1)
        stop.set()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), 'producer must not block on full queue')


if __name__ == '__main__':
    unittest.main(verbosity=2)
